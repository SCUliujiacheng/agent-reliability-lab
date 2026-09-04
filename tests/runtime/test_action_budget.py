"""Durable per-run logical action budget security contracts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event as sqlalchemy_event

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import (
    AgentAction,
    CallToolAction,
    FinishAction,
)
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.service import RunService
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


class CursorPolicy:
    """Return a safe read forever unless one durable cursor should finish."""

    def __init__(self, *, finish_at: int | None = None) -> None:
        self.finish_at = finish_at
        self.calls: list[int] = []

    async def next_action(self, run: Run, _: Scenario) -> AgentAction:
        self.calls.append(run.current_step)
        if run.current_step == self.finish_at:
            return FinishAction(summary="budget complete", outcome="complete")
        return CallToolAction(tool_name="get_service_health")


class ApprovalThenFinishPolicy:
    """Require one approval at cursor zero, then finish at cursor one."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def next_action(self, run: Run, _: Scenario) -> AgentAction:
        self.calls.append(run.current_step)
        if run.current_step == 0:
            return CallToolAction(
                tool_name="prepare_rollback",
                arguments={"deployment_id": "deploy-2026-09-04-001"},
                idempotency_key="budget-approval-v1",
            )
        return FinishAction(summary="approved", outcome="approved")


class CancelOncePolicy:
    """Cancel one provider call, then expose any unsafe retry as a success."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def next_action(self, run: Run, _: Scenario) -> AgentAction:
        self.calls.append(run.current_step)
        if len(self.calls) == 1:
            raise asyncio.CancelledError
        return FinishAction(summary="unsafe retry", outcome="retried")


@dataclass
class BudgetHarness:
    service: RunService
    store: SQLiteRunStore
    backend: IncidentBackend
    scenario: Scenario


def _service(
    path: Path,
    policy: Any,
    *,
    max_action_steps: int,
) -> BudgetHarness:
    settings = Settings(data_dir=path.parent, database_path=path)
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    recorder = TraceRecorder(store)
    backend = IncidentBackend()
    gateway = ToolGateway(
        store,
        recorder,
        incident_registry(backend),
        incident_backend=backend,
    )
    scenario = Scenario(
        id="budget-boundary",
        version=1,
        actions=(FinishAction(summary="unused", outcome="unused"),),
        expected_outcome="complete",
    )
    try:
        service = RunService(
            store,
            recorder,
            gateway,
            lambda _: scenario,
            policies={"budget": policy},
            max_action_steps=max_action_steps,
        )
    except BaseException:
        store.close()
        raise
    return BudgetHarness(service, store, backend, scenario)


def _events(harness: BudgetHarness, run: Run, event_type: str) -> list[TraceEvent]:
    return [
        event
        for event in harness.store.list_events(run.trace_id)
        if event.event_type == event_type
    ]


@pytest.mark.asyncio
async def test_infinite_policy_fails_at_exact_budget_without_an_extra_call(
    tmp_path: Path,
) -> None:
    policy = CursorPolicy()
    harness = _service(tmp_path / "infinite.db", policy, max_action_steps=3)
    try:
        run = await harness.service.start(
            harness.scenario.id, "resilient", policy_name="budget"
        )

        assert run.status is RunStatus.FAILED
        assert run.current_step == 3
        assert run.result == {"code": "action_budget_exhausted"}
        assert policy.calls == [0, 1, 2]
        assert len(_events(harness, run, "policy.action")) == 3
        assert len(_events(harness, run, "run.checkpointed")) == 3
        failures = _events(harness, run, "run.failed")
        assert len(failures) == 1
        assert failures[0].payload == {"code": "action_budget_exhausted"}
        assert failures[0].status == "error"
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_finish_is_allowed_as_the_last_budgeted_action(tmp_path: Path) -> None:
    policy = CursorPolicy(finish_at=2)
    harness = _service(tmp_path / "finish.db", policy, max_action_steps=3)
    try:
        run = await harness.service.start(
            harness.scenario.id, "resilient", policy_name="budget"
        )

        assert run.status is RunStatus.SUCCEEDED
        assert run.current_step == 3
        assert run.result == {
            "outcome": "complete",
            "summary": "budget complete",
            "evidence_refs": [],
        }
        assert policy.calls == [0, 1, 2]
        assert not _events(harness, run, "run.failed")
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_reconstruction_enforces_the_persisted_cursor_before_policy_call(
    tmp_path: Path,
) -> None:
    policy = CursorPolicy(finish_at=3)
    harness = _service(tmp_path / "reconstructed.db", policy, max_action_steps=3)
    try:
        running = Run.new(harness.scenario.id, "resilient").transition(
            RunStatus.RUNNING
        )
        running = running.model_copy(
            update={"current_step": 3, "policy_name": "budget"}
        )
        persisted = harness.store.save_run(running)

        run = await harness.service.resume(persisted.id)

        assert run.status is RunStatus.FAILED
        assert run.current_step == 3
        assert run.result == {"code": "action_budget_exhausted"}
        assert policy.calls == []
        assert not _events(harness, run, "policy.action")
        assert len(_events(harness, run, "run.failed")) == 1
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_cancelled_policy_call_consumes_one_durable_budget_slot(
    tmp_path: Path,
) -> None:
    policy = CancelOncePolicy()
    harness = _service(tmp_path / "cancelled.db", policy, max_action_steps=1)
    try:
        with pytest.raises(asyncio.CancelledError):
            await harness.service.start(
                harness.scenario.id, "resilient", policy_name="budget"
            )

        interrupted = harness.store.list_runs(limit=1)[0]
        assert interrupted.status is RunStatus.RUNNING
        assert interrupted.current_step == 0

        recovered = await harness.service.resume(interrupted.id)

        assert recovered.status is RunStatus.FAILED
        assert recovered.current_step == 0
        assert recovered.result == {"code": "action_budget_exhausted"}
        assert policy.calls == [0]
        assert len(_events(harness, recovered, "run.failed")) == 1
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_exhaustion_state_and_audit_roll_back_together_then_recover(
    tmp_path: Path,
) -> None:
    policy = CursorPolicy()
    harness = _service(tmp_path / "atomic-exhaustion.db", policy, max_action_steps=1)

    def fail_run_failed_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO EVENTS") and (
            "run.failed" in str(parameters)
        ):
            raise RuntimeError("audit insert failed")

    sqlalchemy_event.listen(
        harness.store._engine, "before_cursor_execute", fail_run_failed_insert
    )
    try:
        with pytest.raises(RuntimeError, match="audit insert failed"):
            await harness.service.start(
                harness.scenario.id, "resilient", policy_name="budget"
            )
    finally:
        sqlalchemy_event.remove(
            harness.store._engine, "before_cursor_execute", fail_run_failed_insert
        )

    try:
        interrupted = harness.store.list_runs(limit=1)[0]
        assert interrupted.status is RunStatus.RUNNING
        assert interrupted.current_step == 1
        assert interrupted.result is None
        assert not _events(harness, interrupted, "run.failed")

        recovered = await harness.service.resume(interrupted.id)

        assert recovered.status is RunStatus.FAILED
        assert recovered.current_step == 1
        assert recovered.result == {"code": "action_budget_exhausted"}
        assert policy.calls == [0]
        assert len(_events(harness, recovered, "run.failed")) == 1
    finally:
        harness.store.close()


@pytest.mark.asyncio
async def test_pending_approval_reconstruction_does_not_consume_budget_twice(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval.db"
    first_policy = ApprovalThenFinishPolicy()
    first = _service(database, first_policy, max_action_steps=2)
    waiting = await first.service.start(
        first.scenario.id, "resilient", policy_name="budget"
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert waiting.current_step == 0
    assert waiting.pending_action_fingerprint is not None
    assert first_policy.calls == [0]
    first.store.close()

    reconstructed_policy = ApprovalThenFinishPolicy()
    reconstructed = _service(database, reconstructed_policy, max_action_steps=2)
    try:
        completed = await reconstructed.service.approve(
            waiting.id,
            actor="reviewer",
            allow=True,
            expected_action_step=waiting.current_step,
            expected_action_fingerprint=waiting.pending_action_fingerprint,
        )

        assert completed.status is RunStatus.SUCCEEDED
        assert completed.current_step == 2
        assert reconstructed_policy.calls == [1]
        assert reconstructed.backend.rollback_preparations == 1
    finally:
        reconstructed.store.close()


@pytest.mark.parametrize("value", [0, -1, 1025, True, 1.5])
def test_direct_runtime_rejects_invalid_action_budgets(
    tmp_path: Path, value: object
) -> None:
    policy = CursorPolicy()

    with pytest.raises(ValueError, match="action step budget"):
        harness = _service(
            tmp_path / "invalid.db",
            policy,
            max_action_steps=value,  # type: ignore[arg-type]
        )
        harness.store.close()


def test_action_budget_settings_default_and_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARL_MAX_ACTION_STEPS", raising=False)
    assert Settings.from_env(tmp_path).max_action_steps == 64

    monkeypatch.setenv("ARL_MAX_ACTION_STEPS", "7")
    assert Settings.from_env(tmp_path).max_action_steps == 7


@pytest.mark.parametrize("value", ["0", "1025", "not-an-integer"])
def test_action_budget_environment_rejects_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ARL_MAX_ACTION_STEPS", value)

    with pytest.raises(ValueError, match="action step budget"):
        Settings.from_env(tmp_path)
