"""Application service for starting, approving, and resuming durable runs."""

from collections.abc import Callable, Mapping
from typing import Literal, cast
from uuid import UUID, uuid4

from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.orchestrator import DurableOrchestrator
from agent_reliability_lab.runtime.policies import Policy, ScriptedPolicy
from agent_reliability_lab.storage.store import (
    RunExecutionConflictError,
    SQLiteRunStore,
)
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway

ScenarioLoader = Callable[[str], Scenario]


class RunNotFoundError(LookupError):
    """Raised when a requested durable run does not exist."""


class RunConflictError(RuntimeError):
    """Raised when an operation conflicts with the run state machine."""


class RunService:
    """Stable runtime facade shared by API, CLI, and evaluation callers."""

    def __init__(
        self,
        store: SQLiteRunStore,
        recorder: TraceRecorder,
        gateway: ToolGateway,
        scenario_loader: ScenarioLoader,
        *,
        policies: Mapping[str, Policy] | None = None,
    ) -> None:
        self.store = store
        self._recorder = recorder
        self._gateway = gateway
        self._scenario_loader = scenario_loader
        configured = dict(policies or {})
        configured.setdefault("scripted", ScriptedPolicy())
        self._policies = configured

    @property
    def policy(self) -> Policy:
        return self._policies["scripted"]

    def load_scenario(self, scenario_id: str) -> Scenario:
        return self._scenario_loader(scenario_id)

    def new_run(self, scenario_id: str, mode: Literal["fragile", "resilient"]) -> Run:
        scenario = self.load_scenario(scenario_id)
        return Run.new(scenario_id, mode).model_copy(
            update={"context": scenario.initial_context}
        )

    async def start(
        self,
        scenario_id: str,
        mode: Literal["fragile", "resilient"],
        *,
        policy_name: str = "scripted",
    ) -> Run:
        scenario = self.load_scenario(scenario_id)
        policy = self._policy(policy_name)
        run = Run.new(scenario_id, mode).model_copy(
            update={
                "context": scenario.initial_context,
                "policy_name": policy_name,
            }
        )
        canonical = self.store.save_run(run)
        return await self._execute_owned(canonical, scenario, policy)

    async def resume(self, run_id: UUID) -> Run:
        run = self._required(run_id)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            self._rejected(run, "run.resume.rejected", "terminal")
            raise RunConflictError("terminal runs cannot be resumed")
        scenario = self.load_scenario(run.scenario_id)
        try:
            return await self._execute_owned(
                run, scenario, self._policy(run.policy_name)
            )
        except RunExecutionConflictError as error:
            current = self._required(run_id)
            self._rejected(current, "run.resume.rejected", "live_owner")
            raise RunConflictError("run has a live owner") from error

    async def approve(
        self,
        run_id: UUID,
        *,
        actor: str,
        allow: bool,
        reason: str | None = None,
    ) -> Run:
        run = self._required(run_id)
        latest = self.store.get_latest_approval(run_id)
        if run.pending_action_fingerprint is not None:
            action_step = run.current_step
            fingerprint = run.pending_action_fingerprint
        elif latest is not None:
            action_step = cast(int, latest["action_step"])
            fingerprint = cast(str, latest["action_fingerprint"])
        else:
            self._rejected(run, "approval.rejected", "not_waiting")
            raise RunConflictError("run is not waiting for approval")

        try:
            decision = self.store.record_approval(
                run_id,
                actor=actor,
                allow=allow,
                action_step=action_step,
                action_fingerprint=fingerprint,
                reason=reason,
            )
        except ValueError:
            self._rejected(run, "approval.rejected", "decision_conflict")
            raise
        self._recorder.record(
            run.trace_id,
            "approval.recorded",
            {
                "actor": actor,
                "allow": allow,
                "action_step": action_step,
                "action_fingerprint": fingerprint,
            },
            parent_span_id=run.trace_id,
        )

        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            return run
        if run.status not in {RunStatus.WAITING_APPROVAL, RunStatus.RUNNING}:
            self._rejected(run, "approval.rejected", "not_waiting")
            raise RunConflictError("run is not waiting for approval")
        if decision["allow"] is not True:
            return self._deny(run, actor=actor, reason=reason)
        return await self.resume(run_id)

    def get(self, run_id: UUID) -> Run:
        return self._required(run_id)

    def list(self, *, limit: int = 100) -> list[Run]:
        return self.store.list_runs(limit=limit)

    async def _execute_owned(self, run: Run, scenario: Scenario, policy: Policy) -> Run:
        owner_token = uuid4().hex
        claimed = self.store.claim_run_execution(run.id, owner_token=owner_token)
        try:
            await DurableOrchestrator(
                self.store, self._recorder, self._gateway, policy
            ).execute(claimed, scenario, owner_token=owner_token)
        finally:
            released = self.store.release_run_execution(run.id, owner_token=owner_token)
        return released

    def _deny(self, run: Run, *, actor: str, reason: str | None) -> Run:
        owner_token = uuid4().hex
        owned = self.store.claim_run_execution(run.id, owner_token=owner_token)
        if owned.status is RunStatus.WAITING_APPROVAL:
            owned = self.store.save_run(
                owned.transition(RunStatus.FAILED).model_copy(
                    update={
                        "pending_approval": False,
                        "pending_action": None,
                        "pending_action_fingerprint": None,
                        "result": {"code": "approval_denied", "reason": reason},
                    }
                ),
                expected_version=owned.version,
            )
        self._recorder.record(
            owned.trace_id,
            "approval.denied",
            {"actor": actor, "reason": reason},
            parent_span_id=owned.trace_id,
            status="error",
        )
        return self.store.release_run_execution(run.id, owner_token=owner_token)

    def _required(self, run_id: UUID) -> Run:
        run = self.store.get_run(run_id)
        if run is None:
            raise RunNotFoundError(f"run not found: {run_id}")
        return run

    def _policy(self, name: str) -> Policy:
        try:
            return self._policies[name]
        except KeyError as error:
            raise ValueError(f"unknown policy: {name}") from error

    def _rejected(self, run: Run, event_type: str, reason: str) -> None:
        self._recorder.record(
            run.trace_id,
            event_type,
            {"reason": reason, "status": run.status},
            parent_span_id=run.trace_id,
            status="error",
        )


__all__ = ["RunConflictError", "RunNotFoundError", "RunService"]
