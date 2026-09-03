"""Behavioral regressions required by Task 4 review round 2."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import AgentAction, FinishAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.service import RunService
from agent_reliability_lab.storage.store import (
    RunExecutionConflictError,
    SQLiteRunStore,
)
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


class ManualClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class HeartbeatBarrier:
    """Let a test deterministically release individual heartbeat waits."""

    def __init__(self) -> None:
        self.entered = [asyncio.Event() for _ in range(3)]
        self.release = [asyncio.Event() for _ in range(3)]
        self.calls = 0

    async def __call__(self, _: float) -> None:
        call = self.calls
        self.calls += 1
        self.entered[call].set()
        await self.release[call].wait()


class BlockingFinishPolicy:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def next_action(self, _: Run, __: Scenario) -> AgentAction:
        self.started.set()
        await self.finish.wait()
        return FinishAction(summary="done", outcome="done")


def heartbeat_service(
    path: Path,
    *,
    clock: ManualClock,
    sleeper: HeartbeatBarrier,
    policy: BlockingFinishPolicy,
) -> RunService:
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=path.parent, database_path=path),
        clock=clock,
        run_lease_seconds=10,
    )
    store.create_schema()
    recorder = TraceRecorder(store)
    gateway = ToolGateway(store, recorder, incident_registry(IncidentBackend()))
    scenario = Scenario(
        id="heartbeat",
        version=1,
        actions=(FinishAction(summary="done", outcome="done"),),
        expected_outcome="done",
    )
    return RunService(
        store,
        recorder,
        gateway,
        lambda _: scenario,
        policies={"blocking": policy},
        heartbeat_interval_seconds=1,
        heartbeat_sleeper=sleeper,
    )


@pytest.mark.asyncio
async def test_live_heartbeat_prevents_reclaim_until_owner_is_abandoned(
    tmp_path: Path,
) -> None:
    """A long provider call remains owned, then fails closed after actual reclaim."""
    clock = ManualClock()
    barrier = HeartbeatBarrier()
    policy = BlockingFinishPolicy()
    service = heartbeat_service(
        tmp_path / "heartbeat.db", clock=clock, sleeper=barrier, policy=policy
    )

    worker_a = asyncio.create_task(
        service.start("heartbeat", "resilient", policy_name="blocking")
    )
    await policy.started.wait()
    await barrier.entered[0].wait()
    run = service.list()[0]

    clock.advance(11)
    barrier.release[0].set()
    await barrier.entered[1].wait()
    assert service.get(run.id).version == run.version
    with pytest.raises(RunExecutionConflictError, match="live owner"):
        service.store.claim_run_execution(run.id, owner_token="worker-b")

    clock.advance(11)
    reclaimed = service.store.claim_run_execution(run.id, owner_token="worker-b")
    assert reclaimed.execution_owner == "worker-b"

    policy.finish.set()
    with pytest.raises(RunExecutionConflictError, match="checkpoint"):
        await worker_a

    unchanged = service.get(run.id)
    assert unchanged.status is RunStatus.RUNNING
    assert unchanged.current_step == 0
    assert unchanged.execution_owner == "worker-b"
    service.store.release_run_execution(run.id, owner_token="worker-b")


@pytest.mark.asyncio
async def test_release_ownership_loss_does_not_mask_cancellation(
    tmp_path: Path,
) -> None:
    """Cleanup conflict must not replace the CancelledError from the active call."""
    clock = ManualClock()
    barrier = HeartbeatBarrier()
    policy = BlockingFinishPolicy()
    service = heartbeat_service(
        tmp_path / "cancel-heartbeat.db",
        clock=clock,
        sleeper=barrier,
        policy=policy,
    )

    worker_a = asyncio.create_task(
        service.start("heartbeat", "resilient", policy_name="blocking")
    )
    await policy.started.wait()
    await barrier.entered[0].wait()
    run = service.list()[0]
    clock.advance(11)
    service.store.claim_run_execution(run.id, owner_token="worker-b")

    worker_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_a

    unchanged = service.get(run.id)
    assert unchanged.status is RunStatus.RUNNING
    assert unchanged.current_step == 0
    assert unchanged.execution_owner == "worker-b"
    service.store.release_run_execution(run.id, owner_token="worker-b")
