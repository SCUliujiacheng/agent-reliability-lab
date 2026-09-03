"""Application service for starting, approving, and resuming durable runs."""

from collections.abc import Callable, Mapping
from typing import Literal
from uuid import UUID

from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.orchestrator import DurableOrchestrator
from agent_reliability_lab.runtime.policies import Policy, ScriptedPolicy
from agent_reliability_lab.storage.store import SQLiteRunStore
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
        """Expose the default policy for runtime composition inspection."""
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
        return await self._orchestrator(policy).execute(canonical, scenario)

    async def resume(self, run_id: UUID) -> Run:
        run = self._required(run_id)
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
            raise RunConflictError("terminal runs cannot be resumed")
        if run.status is RunStatus.RUNNING:
            raise RunConflictError("currently running runs cannot be resumed")
        scenario = self.load_scenario(run.scenario_id)
        return await self._orchestrator(self._policy(run.policy_name)).execute(
            run, scenario
        )

    async def approve(
        self,
        run_id: UUID,
        *,
        actor: str,
        allow: bool,
        reason: str | None = None,
    ) -> Run:
        run = self._required(run_id)
        if self.store.get_approval(run_id) is not None:
            self.store.record_approval(run_id, actor=actor, allow=allow, reason=reason)
        if run.status is not RunStatus.WAITING_APPROVAL:
            raise RunConflictError("run is not waiting for approval")
        self.store.record_approval(run_id, actor=actor, allow=allow, reason=reason)
        if allow:
            return await self.resume(run_id)
        terminal = run.transition(RunStatus.FAILED).model_copy(
            update={
                "pending_approval": False,
                "result": {"code": "approval_denied", "reason": reason},
            }
        )
        saved = self.store.save_run(terminal, expected_version=run.version)
        self._recorder.record(
            saved.trace_id,
            "approval.denied",
            {"actor": actor, "reason": reason},
            parent_span_id=saved.trace_id,
            status="error",
        )
        return saved

    def get(self, run_id: UUID) -> Run:
        return self._required(run_id)

    def list(self, *, limit: int = 100) -> list[Run]:
        return self.store.list_runs(limit=limit)

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

    def _orchestrator(self, policy: Policy) -> DurableOrchestrator:
        return DurableOrchestrator(self.store, self._recorder, self._gateway, policy)


__all__ = ["RunConflictError", "RunNotFoundError", "RunService"]
