"""Checkpointed agent state-machine orchestration."""

import asyncio
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from agent_reliability_lab.domain.actions import FailAction, FinishAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.policies import Policy
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway


class DurableOrchestrator:
    """Execute actions while making every externally visible step durable."""

    def __init__(
        self,
        store: SQLiteRunStore,
        recorder: TraceRecorder,
        gateway: ToolGateway,
        policy: Policy,
    ) -> None:
        self._store = store
        self._recorder = recorder
        self._gateway = gateway
        self._policy = policy

    async def execute(self, run: Run, scenario: Scenario) -> Run:
        """Advance a queued or approval-paused run to its next durable stop."""
        if run.status in {RunStatus.QUEUED, RunStatus.WAITING_APPROVAL}:
            run = self._save(
                run.transition(RunStatus.RUNNING).model_copy(
                    update={"pending_approval": False}
                ),
                previous=run,
            )

        while run.status is RunStatus.RUNNING:
            try:
                action = await self._policy.next_action(run, scenario)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - policy is an external boundary.
                run = self._terminal_failure(run, "policy_error", type(error).__name__)
                break

            self._record(run, "policy.action", action.model_dump(mode="json"))
            if isinstance(action, FinishAction):
                terminal = run.transition(RunStatus.SUCCEEDED).model_copy(
                    update={
                        "current_step": run.current_step + 1,
                        "result": {
                            "outcome": action.outcome,
                            "summary": action.summary,
                            "evidence_refs": list(action.evidence_refs),
                        },
                    }
                )
                run = self._save(terminal, previous=run)
                self._record(run, "run.succeeded", cast(JsonValue, run.result or {}))
                break
            if isinstance(action, FailAction):
                run = self._terminal_failure(
                    run,
                    action.code,
                    action.explanation,
                    next_step=True,
                )
                break
            if self._gateway.requires_approval(action):
                approval = self._store.get_approval(run.id)
                if approval is None:
                    paused = run.transition(RunStatus.WAITING_APPROVAL).model_copy(
                        update={"pending_approval": True}
                    )
                    run = self._save(paused, previous=run)
                    self._record(
                        run,
                        "run.waiting_approval",
                        {"tool_name": action.tool_name, "step": run.current_step},
                    )
                    break

            result = await self._gateway.call(run, action)
            if result.status == "failed":
                run = self._terminal_failure(
                    run, result.error_code or "tool_execution_failed"
                )
                break
            prior_results = run.context.get("tool_results", {})
            tool_results = (
                dict(prior_results) if isinstance(prior_results, dict) else {}
            )
            tool_results[str(run.current_step)] = result.output
            checkpoint = run.model_copy(
                update={
                    "current_step": run.current_step + 1,
                    "context": {**run.context, "tool_results": tool_results},
                    "updated_at": datetime.now(UTC),
                }
            )
            run = self._save(checkpoint, previous=run)
            self._record(
                run,
                "run.checkpointed",
                {"current_step": run.current_step, "cached": result.cached},
            )
        return run

    def _terminal_failure(
        self,
        run: Run,
        code: str,
        explanation: str | None = None,
        *,
        next_step: bool = False,
    ) -> Run:
        payload: dict[str, JsonValue] = {"code": code}
        if explanation is not None:
            payload["explanation"] = explanation
        terminal = run.transition(RunStatus.FAILED).model_copy(
            update={
                "current_step": run.current_step + int(next_step),
                "pending_approval": False,
                "result": payload,
            }
        )
        saved = self._save(terminal, previous=run)
        self._record(saved, "run.failed", payload, status="error")
        return saved

    def _save(self, changed: Run, *, previous: Run) -> Run:
        return self._store.save_run(changed, expected_version=previous.version)

    def _record(
        self,
        run: Run,
        event_type: str,
        payload: JsonValue,
        *,
        status: str = "ok",
    ) -> None:
        self._recorder.record(
            run.trace_id,
            event_type,
            payload,
            parent_span_id=run.trace_id,
            status=status,
        )


__all__ = ["DurableOrchestrator"]
