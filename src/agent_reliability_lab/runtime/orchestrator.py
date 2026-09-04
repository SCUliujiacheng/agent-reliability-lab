"""Checkpointed agent state-machine orchestration."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import JsonValue

from agent_reliability_lab.config import (
    DEFAULT_MAX_ACTION_STEPS,
    validate_action_step_budget,
)
from agent_reliability_lab.domain.actions import AgentAction, FailAction, FinishAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import FaultType, Scenario
from agent_reliability_lab.runtime.policies import Policy
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.faults import FaultKind, FaultPlan, FaultRule
from agent_reliability_lab.tools.gateway import ToolGateway, action_fingerprint


class DurableOrchestrator:
    """Execute actions while making every externally visible step durable."""

    def __init__(
        self,
        store: SQLiteRunStore,
        recorder: TraceRecorder,
        gateway: ToolGateway,
        policy: Policy,
        *,
        max_action_steps: int = DEFAULT_MAX_ACTION_STEPS,
    ) -> None:
        self._store = store
        self._recorder = recorder
        self._gateway = gateway
        self._policy = policy
        self._max_action_steps = validate_action_step_budget(max_action_steps)

    async def execute(self, run: Run, scenario: Scenario, *, owner_token: str) -> Run:
        """Advance an owned run to its next durable stop."""
        if run.status in {RunStatus.QUEUED, RunStatus.WAITING_APPROVAL}:
            previous_status = run.status
            run = self._save(
                run.transition(RunStatus.RUNNING).model_copy(
                    update={"pending_approval": False}
                ),
                previous=run,
                owner_token=owner_token,
            )
            self._record(
                run,
                "run.running",
                {"from_status": previous_status},
            )
        elif run.status is RunStatus.RUNNING:
            self._record(
                run,
                "run.running",
                {"from_status": "running"},
            )

        faults = _scenario_fault_plan(scenario)
        while run.status is RunStatus.RUNNING:
            action: AgentAction
            if run.pending_action is not None:
                action = run.pending_action
            else:
                budget_used = max(run.current_step, run.policy_calls_started)
                if budget_used >= self._max_action_steps:
                    run = self._terminal_failure(
                        run,
                        "action_budget_exhausted",
                        owner_token=owner_token,
                    )
                    break
                run = self._save(
                    run.model_copy(
                        update={
                            "policy_calls_started": budget_used + 1,
                            "updated_at": datetime.now(UTC),
                        }
                    ),
                    previous=run,
                    owner_token=owner_token,
                )
                try:
                    action = await self._policy.next_action(run, scenario)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    run = self._terminal_failure(
                        run,
                        "policy_error",
                        type(error).__name__,
                        owner_token=owner_token,
                    )
                    break

            self._record(
                run,
                "policy.action",
                {**action.model_dump(mode="json"), "action_step": run.current_step},
            )
            if isinstance(action, FinishAction):
                terminal = run.transition(RunStatus.SUCCEEDED).model_copy(
                    update={
                        "current_step": run.current_step + 1,
                        "pending_action": None,
                        "pending_action_fingerprint": None,
                        "result": {
                            "outcome": action.outcome,
                            "summary": action.summary,
                            "evidence_refs": list(action.evidence_refs),
                        },
                    }
                )
                run = self._save(terminal, previous=run, owner_token=owner_token)
                self._record(run, "run.succeeded", cast(JsonValue, run.result or {}))
                break
            if isinstance(action, FailAction):
                run = self._terminal_failure(
                    run,
                    action.code,
                    action.explanation,
                    owner_token=owner_token,
                    next_step=True,
                )
                break

            action = self._gateway.stabilize_action(run, action)
            fingerprint = action_fingerprint(action)
            failure = self._gateway.preflight(
                run, action, faults=faults, check_approval=False
            )
            if failure is not None:
                self._record(
                    run,
                    "tool.preflight.failed",
                    {
                        "action_step": run.current_step,
                        "tool_name": action.tool_name,
                        "action_fingerprint": fingerprint,
                        "code": failure.error_code or "tool_preflight_failed",
                    },
                    status="error",
                )
                run = self._terminal_failure(
                    run,
                    failure.error_code or "tool_preflight_failed",
                    owner_token=owner_token,
                )
                break

            if self._gateway.requires_approval(action):
                approval = self._store.get_approval(
                    run.id, action_step=run.current_step
                )
                if approval is None:
                    paused = run.transition(RunStatus.WAITING_APPROVAL).model_copy(
                        update={
                            "pending_approval": True,
                            "pending_action": action,
                            "pending_action_fingerprint": fingerprint,
                        }
                    )
                    run = self._save(paused, previous=run, owner_token=owner_token)
                    self._record(
                        run,
                        "run.waiting_approval",
                        {
                            "tool_name": action.tool_name,
                            "step": run.current_step,
                            "action_fingerprint": fingerprint,
                        },
                    )
                    break
                if approval["action_fingerprint"] != fingerprint:
                    run = self._terminal_failure(
                        run, "approval_mismatch", owner_token=owner_token
                    )
                    break
                if approval["allow"] is not True:
                    run = self._terminal_failure(
                        run, "approval_denied", owner_token=owner_token
                    )
                    break

            result = await self._gateway.call(run, action, faults)
            if result.status == "failed":
                run = self._terminal_failure(
                    run,
                    result.error_code or "tool_execution_failed",
                    owner_token=owner_token,
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
                    "pending_approval": False,
                    "pending_action": None,
                    "pending_action_fingerprint": None,
                    "updated_at": datetime.now(UTC),
                }
            )
            run = self._save(checkpoint, previous=run, owner_token=owner_token)
            self._record(
                run,
                "run.checkpointed",
                {
                    "action_step": run.current_step - 1,
                    "attempt": result.attempts,
                    "current_step": run.current_step,
                    "cached": result.cached,
                    "output_digest": _canonical_json_digest(result.output),
                    "context_digest": _canonical_json_digest(run.context),
                },
            )
        return run

    def _terminal_failure(
        self,
        run: Run,
        code: str,
        explanation: str | None = None,
        *,
        owner_token: str,
        next_step: bool = False,
    ) -> Run:
        payload: dict[str, JsonValue] = {"code": code}
        if explanation is not None:
            payload["explanation"] = explanation
        terminal = run.transition(RunStatus.FAILED).model_copy(
            update={
                "current_step": run.current_step + int(next_step),
                "pending_approval": False,
                "pending_action": None,
                "pending_action_fingerprint": None,
                "result": payload,
            }
        )
        event = self._recorder.build_event(
            run.trace_id,
            "run.failed",
            payload,
            parent_span_id=run.trace_id,
            status="error",
        )
        saved, _ = self._store.save_run_owned_with_event(
            terminal,
            event,
            owner_token=owner_token,
            expected_version=run.version,
        )
        return saved

    def _save(self, changed: Run, *, previous: Run, owner_token: str) -> Run:
        return self._store.save_run_owned(
            changed,
            owner_token=owner_token,
            expected_version=previous.version,
        )

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


def _canonical_json_digest(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _scenario_fault_plan(scenario: Scenario) -> FaultPlan:
    kind_by_type = {
        FaultType.TIMEOUT: FaultKind.TIMEOUT,
        FaultType.RATE_LIMIT: FaultKind.RATE_LIMIT,
        FaultType.TOOL_ERROR: FaultKind.TOOL_ERROR,
        FaultType.MALFORMED_OUTPUT: FaultKind.MALFORMED_OUTPUT,
    }
    return FaultPlan(
        tuple(
            FaultRule(
                tool_name=rule.tool_name,
                attempt=rule.attempt,
                kind=kind_by_type[rule.type],
            )
            for rule in scenario.faults
        )
    )


__all__ = ["DurableOrchestrator"]
