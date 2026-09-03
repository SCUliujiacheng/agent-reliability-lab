"""Auditable schema-first gateway for all registered tool execution."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import (
    ToolClaimState,
    ToolFailureDisposition,
    TraceEvent,
)
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)
from agent_reliability_lab.tools.faults import (
    FaultKind,
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
    retryable: bool = False

    @classmethod
    def succeeded(
        cls, output: JsonValue, *, attempts: int, cached: bool = False
    ) -> "ToolCallResult":
        return cls(status="succeeded", output=output, attempts=attempts, cached=cached)

    @classmethod
    def failed(
        cls, code: str, *, attempts: int, retryable: bool = False
    ) -> "ToolCallResult":
        return cls(
            status="failed",
            error_code=code,
            attempts=attempts,
            retryable=retryable,
        )


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
        action = self.stabilize_action(run, action)
        failure = self.preflight(run, action, faults=faults, check_approval=True)
        if failure is not None:
            return failure
        definition = self._registry.get(action.tool_name)
        if definition is None:  # pragma: no cover - guaranteed by preflight.
            raise AssertionError("preflight accepted an unknown tool")
        validated_input = definition.input_model.model_validate(action.arguments)
        plan = faults or no_faults()

        owner_token: str | None = None
        request_fingerprint = _request_fingerprint(action.tool_name, validated_input)
        if action.idempotency_key is not None:
            owner_token = uuid4().hex
            claim = self.store.claim_tool_execution(
                run.id,
                action.idempotency_key,
                owner_token=owner_token,
                request_fingerprint=request_fingerprint,
                allow_reclaim=not definition.is_write or definition.idempotent,
            )
            if claim.state is ToolClaimState.CONFLICT:
                return ToolCallResult.failed("idempotency_conflict", attempts=0)
            if claim.state is ToolClaimState.COMPLETED:
                try:
                    output = self._validated_output(definition, claim.result)
                except PermanentToolError:
                    return ToolCallResult.failed("invalid_cached_output", attempts=0)
                return ToolCallResult.succeeded(output, attempts=0, cached=True)
            if claim.state is ToolClaimState.FAILED:
                code = (
                    "idempotency_indeterminate"
                    if claim.failure_disposition is ToolFailureDisposition.INDETERMINATE
                    else "idempotency_terminal_failure"
                )
                return ToolCallResult.failed(code, attempts=0)
            if (
                claim.state is not ToolClaimState.CLAIMED
                or claim.owner_token != owner_token
            ):
                return ToolCallResult.failed("idempotency_in_progress", attempts=0)

        context = ToolExecutionContext(
            run_id=run.id,
            tool_name=action.tool_name,
            idempotency_token=action.idempotency_key or owner_token or uuid4().hex,
            request_fingerprint=request_fingerprint,
        )
        try:
            result = await self._attempts(
                run, action, definition, validated_input, context, plan
            )
        except asyncio.CancelledError:
            if owner_token is not None and action.idempotency_key is not None:
                self.store.fail_tool_execution(
                    run.id,
                    action.idempotency_key,
                    owner_token=owner_token,
                    error="tool_execution_cancelled",
                    disposition=(
                        ToolFailureDisposition.RETRYABLE
                        if not definition.is_write or definition.idempotent
                        else ToolFailureDisposition.INDETERMINATE
                    ),
                )
            raise
        if owner_token is not None and action.idempotency_key is not None:
            result = self._persist_claim_result(
                run, action.idempotency_key, owner_token, result, definition
            )
        return result

    def requires_approval(self, action: CallToolAction) -> bool:
        """Report whether a registered action needs approval."""
        definition = self._registry.get(action.tool_name)
        return definition is not None and definition.requires_approval

    def stabilize_action(self, run: Run, action: CallToolAction) -> CallToolAction:
        """Attach a deterministic key when the registered tool requires one."""
        definition = self._registry.get(action.tool_name)
        if (
            definition is None
            or action.idempotency_key is not None
            or not (definition.is_write or definition.requires_approval)
        ):
            return action
        canonical = json.dumps(
            {
                "run_id": str(run.id),
                "step": run.current_step,
                "tool_name": action.tool_name,
                "arguments": action.arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return action.model_copy(update={"idempotency_key": f"runtime-{digest}"})

    def preflight(
        self,
        run: Run,
        action: CallToolAction,
        *,
        faults: FaultPlan | None = None,
        check_approval: bool,
    ) -> ToolCallResult | None:
        """Validate every non-executing gateway requirement in stable order."""
        definition = self._registry.get(action.tool_name)
        if definition is None:
            return ToolCallResult.failed("unknown_tool", attempts=0)
        try:
            definition.input_model.model_validate(action.arguments)
        except ValidationError:
            return ToolCallResult.failed("invalid_input", attempts=0)
        plan = faults or no_faults()
        if any(self._registry.get(rule.tool_name) is None for rule in plan.rules):
            return ToolCallResult.failed("invalid_fault_plan", attempts=0)
        if (definition.is_write or definition.requires_approval) and (
            action.idempotency_key is None
        ):
            return ToolCallResult.failed("idempotency_key_required", attempts=0)
        if definition.requires_approval and check_approval:
            fingerprint = action_fingerprint(action)
            approval = self.store.get_approval(run.id, action_step=run.current_step)
            if approval is None:
                return ToolCallResult.failed("approval_required", attempts=0)
            if approval["action_fingerprint"] != fingerprint:
                return ToolCallResult.failed("approval_mismatch", attempts=0)
            if approval["allow"] is not True:
                return ToolCallResult.failed("approval_denied", attempts=0)
        return None

    async def _attempts(
        self,
        run: Run,
        action: CallToolAction,
        definition: ToolDefinition[Any, Any],
        validated_input: BaseModel,
        context: ToolExecutionContext,
        faults: FaultPlan,
    ) -> ToolCallResult:
        max_attempts = 1 if run.mode == "fragile" else definition.max_attempts
        for attempt in range(1, max_attempts + 1):
            span_id = uuid4()
            self._record(
                run,
                "tool.attempt.started",
                {
                    "tool_name": action.tool_name,
                    "attempt": attempt,
                    "action_step": run.current_step,
                },
                span_id=span_id,
            )
            error: ToolExecutionError
            try:
                injected = faults.fault_for(action.tool_name, attempt)
                if injected is not None:
                    self._record(
                        run,
                        "fault.injected",
                        {
                            **injected.model_dump(mode="json"),
                            "action_step": run.current_step,
                        },
                        span_id=span_id,
                        status="error",
                    )
                    if injected.kind is FaultKind.MALFORMED_OUTPUT:
                        raw = {"__arl_malformed_output__": True}
                    else:
                        raise InjectedFault(injected.kind, tool_name=action.tool_name)
                else:
                    raw = await self._invoke_with_timeout(
                        definition, validated_input, context
                    )
                try:
                    output = self._validated_output(definition, raw)
                except PermanentToolError as validation_error:
                    if validation_error.code == "invalid_output":
                        self._record(
                            run,
                            "tool.output.validation_failed",
                            {
                                "tool_name": action.tool_name,
                                "attempt": attempt,
                                "action_step": run.current_step,
                                "code": "invalid_output",
                                "source": "output_model",
                            },
                            span_id=span_id,
                            status="error",
                        )
                    raise
            except asyncio.CancelledError:
                self._record(
                    run,
                    "tool.attempt.cancelled",
                    {
                        "tool_name": action.tool_name,
                        "attempt": attempt,
                        "action_step": run.current_step,
                    },
                    span_id=span_id,
                    status="error",
                )
                raise
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
                    {
                        "tool_name": action.tool_name,
                        "attempt": attempt,
                        "action_step": run.current_step,
                        "output": _traceable_output(output),
                    },
                    span_id=span_id,
                )
                return ToolCallResult.succeeded(output, attempts=attempt)

            retry_delay = (
                definition.delay_seconds(attempt)
                if error.transient and attempt < max_attempts
                else None
            )
            payload: dict[str, JsonValue] = {
                "tool_name": action.tool_name,
                "attempt": attempt,
                "action_step": run.current_step,
                "code": error.code,
                "transient": error.transient,
            }
            if retry_delay is not None:
                payload["retry_delay_seconds"] = retry_delay
            self._record(
                run,
                "tool.attempt.failed",
                payload,
                span_id=span_id,
                status="error",
            )
            if retry_delay is None:
                return ToolCallResult.failed(
                    error.code, attempts=attempt, retryable=error.transient
                )
            try:
                await self._sleep(retry_delay)
            except asyncio.CancelledError:
                self._record(
                    run,
                    "tool.retry.cancelled",
                    {
                        "tool_name": action.tool_name,
                        "after_attempt": attempt,
                    },
                    span_id=uuid4(),
                    status="error",
                )
                raise
        raise AssertionError("retry loop must return a terminal tool result")

    async def _invoke_with_timeout(
        self,
        definition: ToolDefinition[Any, Any],
        value: BaseModel,
        context: ToolExecutionContext,
    ) -> Any:
        async with asyncio.timeout(definition.timeout_seconds):
            return await definition.handler(value, context)

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

    def _persist_claim_result(
        self,
        run: Run,
        idempotency_key: str,
        owner_token: str,
        result: ToolCallResult,
        definition: ToolDefinition[Any, Any],
    ) -> ToolCallResult:
        if result.status == "succeeded":
            disposition = (
                ToolFailureDisposition.INDETERMINATE
                if definition.is_write
                else ToolFailureDisposition.RETRYABLE
            )
            try:
                self.store.complete_tool_result(
                    run.id,
                    idempotency_key,
                    result.output,
                    owner_token=owner_token,
                    failure_disposition=disposition,
                )
            except ValueError:
                return ToolCallResult.failed(
                    "idempotency_indeterminate"
                    if definition.is_write
                    else "result_serialization_failed",
                    attempts=result.attempts,
                    retryable=not definition.is_write,
                )
            return result
        self.store.fail_tool_execution(
            run.id,
            idempotency_key,
            owner_token=owner_token,
            error=result.error_code or "tool_execution_failed",
            disposition=(
                ToolFailureDisposition.INDETERMINATE
                if definition.is_write
                else (
                    ToolFailureDisposition.RETRYABLE
                    if result.retryable
                    else ToolFailureDisposition.TERMINAL
                )
            ),
        )
        return result

    def _record(
        self,
        run: Run,
        event_type: str,
        payload: JsonValue,
        *,
        span_id: UUID,
        status: str = "ok",
    ) -> None:
        self.events.append(
            self._recorder.record(
                run.trace_id,
                event_type,
                payload,
                span_id=span_id,
                parent_span_id=run.trace_id,
                status=status,
            )
        )


def _request_fingerprint(tool_name: str, validated_input: BaseModel) -> str:
    """Hash the exact tool name and canonical validated input for claim binding."""
    canonical = json.dumps(
        {"tool_name": tool_name, "arguments": validated_input.model_dump(mode="json")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _traceable_output(output: JsonValue) -> JsonValue:
    """Keep trace persistence from changing non-JSON result disposition."""
    try:
        json.dumps(output, allow_nan=False)
    except (TypeError, ValueError):
        return {"__arl_unserializable_output__": True}
    return output


def action_fingerprint(action: CallToolAction) -> str:
    """Hash the complete normalized action approved at one durable cursor."""
    canonical = json.dumps(
        action.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


__all__ = [
    "PermanentToolError",
    "ToolCallResult",
    "ToolGateway",
    "TransientToolError",
    "action_fingerprint",
]
