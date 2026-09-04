"""Cleanup exception precedence regressions for Task 4 review round 3."""

import asyncio
from collections.abc import Callable
from uuid import UUID

import pytest

from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.orchestrator import DurableOrchestrator


class PrimaryExecutionError(RuntimeError):
    """Distinct primary failure used to prove cleanup cannot replace it."""


class HeartbeatExecutionError(RuntimeError):
    """Distinct heartbeat failure used to prove cleanup ordering."""


def release_failure(_: UUID, *, owner_token: str) -> Run:
    del owner_token
    raise RuntimeError("release failed")


@pytest.mark.asyncio
async def test_arbitrary_release_failure_does_not_mask_cancellation(
    app_context: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def blocking_execute(
        _: DurableOrchestrator,
        run: Run,
        scenario: Scenario,
        *,
        owner_token: str,
    ) -> Run:
        del run, scenario, owner_token
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(DurableOrchestrator, "execute", blocking_execute)
    monkeypatch.setattr(
        app_context.runs.store, "release_run_execution", release_failure
    )
    task = asyncio.create_task(app_context.runs.start("rollback-approval", "resilient"))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_arbitrary_release_failure_does_not_mask_async_primary_error(
    app_context: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_execute(
        _: DurableOrchestrator,
        run: Run,
        scenario: Scenario,
        *,
        owner_token: str,
    ) -> Run:
        del run, scenario, owner_token
        raise PrimaryExecutionError("execution failed")

    monkeypatch.setattr(DurableOrchestrator, "execute", failing_execute)
    monkeypatch.setattr(
        app_context.runs.store, "release_run_execution", release_failure
    )

    with pytest.raises(PrimaryExecutionError, match="execution failed"):
        await app_context.runs.start("rollback-approval", "resilient")


@pytest.mark.asyncio
async def test_release_failure_is_surfaced_without_async_primary_error(
    app_context: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def successful_execute(
        _: DurableOrchestrator,
        run: Run,
        scenario: Scenario,
        *,
        owner_token: str,
    ) -> Run:
        del scenario, owner_token
        return run

    monkeypatch.setattr(DurableOrchestrator, "execute", successful_execute)
    monkeypatch.setattr(
        app_context.runs.store, "release_run_execution", release_failure
    )

    with pytest.raises(RuntimeError, match="release failed"):
        await app_context.runs.start("rollback-approval", "resilient")


@pytest.mark.asyncio
async def test_arbitrary_release_failure_does_not_mask_heartbeat_error(
    app_context: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    heartbeat_failed = asyncio.Event()

    async def failing_heartbeat_wait(_: float) -> None:
        heartbeat_failed.set()
        raise HeartbeatExecutionError("heartbeat failed")

    async def execute_after_heartbeat(
        _: DurableOrchestrator,
        run: Run,
        scenario: Scenario,
        *,
        owner_token: str,
    ) -> Run:
        del scenario, owner_token
        await heartbeat_failed.wait()
        return run

    monkeypatch.setattr(DurableOrchestrator, "execute", execute_after_heartbeat)
    monkeypatch.setattr(app_context.runs, "_heartbeat_sleeper", failing_heartbeat_wait)
    monkeypatch.setattr(
        app_context.runs.store, "release_run_execution", release_failure
    )

    with pytest.raises(HeartbeatExecutionError, match="heartbeat failed"):
        await app_context.runs.start("rollback-approval", "resilient")


@pytest.mark.asyncio
async def test_arbitrary_release_failure_does_not_mask_denial_primary_error(
    app_context: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = await app_context.runs.start("rollback-approval", "resilient")
    original_record: Callable[..., object] = app_context.runs._recorder.record

    def failing_denial_record(
        trace_id: UUID,
        event_type: str,
        payload: object,
        **kwargs: object,
    ) -> object:
        if event_type == "approval.denied":
            raise PrimaryExecutionError("denial audit failed")
        return original_record(trace_id, event_type, payload, **kwargs)

    monkeypatch.setattr(app_context.runs._recorder, "record", failing_denial_record)
    monkeypatch.setattr(
        app_context.runs.store, "release_run_execution", release_failure
    )

    with pytest.raises(PrimaryExecutionError, match="denial audit failed"):
        assert waiting.pending_action_fingerprint is not None
        await app_context.runs.approve(
            waiting.id,
            actor="reviewer",
            allow=False,
            expected_action_step=waiting.current_step,
            expected_action_fingerprint=waiting.pending_action_fingerprint,
        )
