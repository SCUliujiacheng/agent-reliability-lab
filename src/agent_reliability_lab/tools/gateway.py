"""Auditable schema-first gateway for all registered tool execution."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import ToolClaimState, TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.contracts import ToolDefinition, ToolRegistry
from agent_reliability_lab.tools.faults import (
    FaultPlan,
    InjectedFault,
    PermanentToolError,
    ToolExecutionError,
    TransientToolError,
    no_faults,
)

Sleeper = Callable[[float], Awaitable[None]]


class ToolCallResult(BaseModel):
    """Typed terminal result returned by every gateway invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["succeeded", "failed"]
    output: JsonValue | None = None
    error_code: str | None = None
    attempts: int = 0
    cached: bool = False

    @classmethod
    def succeeded(
        cls, output: JsonValue, *, attempts: int, cached: bool = False
    ) -> "ToolCallResult":
        return cls(status="succeeded", output=output, attempts=attempts, cached=cached)

    @classmethod
    def failed(cls, code: str, *, attempts: int) -> "ToolCallResult":
        return cls(status="failed", error_code=code, attempts=attempts)


class ToolGateway:
    """The only execution path: validate, approve, claim, invoke, and trace."""

    def __init__(
        self,
        store: SQLiteRunStore,
        recorder: TraceRecorder,
        registry: ToolRegistry,
        *,
        sleeper: Sleeper | None = None,
        incident_backend: object | None = None,
    ) -> None:
        self.store = store
        self._recorder = recorder
        self._registry = registry
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self.incident_backend = incident_backend
        self.events: list[TraceEvent] = []

    def call_sync(
        self, run: Run, action: CallToolAction, faults: FaultPlan | None = None
    ) -> ToolCallResult:
        """Run the async API from synchronous scenario tests and scripts."""
        return asyncio.run(self.call(run, action, faults))

    async def call(
        self, run: Run, action: CallToolAction, faults: FaultPlan | None = None
    ) -> ToolCallResult:
        """Execute one known action through every durable safety boundary."""
        definition = self._registry.get(action.tool_name)
        if definition is None:
            return ToolCallResult.failed("unknown_tool", attempts=0)
        try:
            validated_input = definition.input_model.model_validate(action.arguments)
        except ValidationError:
            return ToolCallResult.failed("invalid_input", attempts=0)
        if definition.requires_approval and not self._approved(run):
            return ToolCallResult.failed("approval_required", attempts=0)

        owner_token: str | None = None
        if action.idempotency_key is not None:
            owner_token = uuid4().hex
            claim = self.store.claim_tool_execution(
                run.id, action.idempotency_key, owner_token=owner_token
            )
            if claim.state is ToolClaimState.COMPLETED:
                try:
                    output = self._validated_output(definition, claim.result)
                except PermanentToolError:
                    return ToolCallResult.failed("invalid_cached_output", attempts=0)
                return ToolCallResult.succeeded(output, attempts=0, cached=True)
            if (
                claim.state is not ToolClaimState.CLAIMED
                or claim.owner_token != owner_token
            ):
                return ToolCallResult.failed("idempotency_in_progress", attempts=0)

        plan = faults or no_faults()
        result = await self._attempts(run, action, definition, validated_input, plan)
        if owner_token is not None and action.idempotency_key is not None:
            self._persist_claim_result(run, action.idempotency_key, owner_token, result)
        return result

    async def _attempts(
        self,
        run: Run,
        action: CallToolAction,
        definition: ToolDefinition[Any, Any],
        validated_input: BaseModel,
        faults: FaultPlan,
    ) -> ToolCallResult:
        for attempt in range(1, definition.max_attempts + 1):
            self._record(
                run,
                "tool.attempt.started",
                {"tool_name": action.tool_name, "attempt": attempt},
            )
            error: ToolExecutionError
            try:
                injected = faults.fault_for(action.tool_name, attempt)
                if injected is not None:
                    self._record(
                        run, "fault.injected", injected.model_dump(mode="json")
                    )
                    raise InjectedFault(injected.kind, tool_name=action.tool_name)
                raw = await self._invoke_with_timeout(definition, validated_input)
                output = self._validated_output(definition, raw)
            except TimeoutError:
                error = TransientToolError("tool_timeout", "tool handler timed out")
            except ToolExecutionError as caught:
                error = caught
            except ValidationError:
                error = PermanentToolError(
                    "invalid_output", "tool output did not match schema"
                )
            except Exception:  # noqa: BLE001 - handlers are an untrusted boundary.
                error = PermanentToolError(
                    "tool_execution_failed", "tool handler failed"
                )
            else:
                self._record(
                    run,
                    "tool.attempt.succeeded",
                    {"tool_name": action.tool_name, "attempt": attempt},
                )
                return ToolCallResult.succeeded(output, attempts=attempt)

            retry_delay = (
                definition.delay_seconds(attempt)
                if error.transient and attempt < definition.max_attempts
                else None
            )
            payload: dict[str, JsonValue] = {
                "tool_name": action.tool_name,
                "attempt": attempt,
                "code": error.code,
                "transient": error.transient,
            }
            if retry_delay is not None:
                payload["retry_delay_seconds"] = retry_delay
            self._record(run, "tool.attempt.failed", payload)
            if retry_delay is None:
                return ToolCallResult.failed(error.code, attempts=attempt)
            await self._sleep(retry_delay)
        raise AssertionError("retry loop must return a terminal tool result")

    async def _invoke_with_timeout(
        self, definition: ToolDefinition[Any, Any], value: BaseModel
    ) -> Any:
        async with asyncio.timeout(definition.timeout_seconds):
            return await definition.handler(value)

    def _validated_output(
        self, definition: ToolDefinition[Any, Any], raw: object
    ) -> JsonValue:
        try:
            output = definition.output_model.model_validate(raw)
        except ValidationError as error:
            raise PermanentToolError(
                "invalid_output", "tool output did not match schema"
            ) from error
        return cast(JsonValue, output.model_dump(mode="json"))

    def _approved(self, run: Run) -> bool:
        approval = self.store.get_approval(run.id)
        return approval is not None and approval["allow"] is True

    def _persist_claim_result(
        self,
        run: Run,
        idempotency_key: str,
        owner_token: str,
        result: ToolCallResult,
    ) -> None:
        if result.status == "succeeded":
            self.store.complete_tool_result(
                run.id,
                idempotency_key,
                result.output,
                owner_token=owner_token,
            )
        else:
            self.store.fail_tool_execution(
                run.id,
                idempotency_key,
                owner_token=owner_token,
                error=result.error_code or "tool_execution_failed",
            )

    def _record(self, run: Run, event_type: str, payload: JsonValue) -> None:
        self.events.append(self._recorder.record(run.trace_id, event_type, payload))


__all__ = [
    "PermanentToolError",
    "ToolCallResult",
    "ToolGateway",
    "TransientToolError",
]
