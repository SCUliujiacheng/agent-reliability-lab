"""Approval pause and process-reconstruction behavior."""

import pytest

from agent_reliability_lab.domain.runs import RunStatus


@pytest.mark.asyncio
async def test_approval_run_reconstructs_and_resumes_exactly_once(
    app_context: object,
) -> None:
    """Losing the cursor or idempotency key would duplicate the durable write."""
    run = await app_context.runs.start("rollback-approval", "resilient")
    assert run.status is RunStatus.WAITING_APPROVAL

    reconstructed = app_context.reconstruct()
    completed = await reconstructed.runs.approve(run.id, actor="reviewer", allow=True)
    assert completed.status is RunStatus.SUCCEEDED
    assert reconstructed.backend.rollback_preparations == 1

    repeated = await reconstructed.runs.approve(run.id, actor="reviewer", allow=True)
    assert repeated == completed
    recorded = [
        event
        for event in reconstructed.runs.store.list_events(completed.trace_id)
        if event.event_type == "approval.recorded"
    ]
    assert len(recorded) == 1

    with pytest.raises(ValueError, match="conflict"):
        await reconstructed.runs.approve(run.id, actor="other", allow=True)
    assert reconstructed.backend.rollback_preparations == 1


@pytest.mark.asyncio
async def test_denied_approval_terminates_without_executing_write(
    app_context: object,
) -> None:
    """Treating denial as a pause would leave a forbidden mutation resumable."""
    waiting = await app_context.runs.start("rollback-approval", "resilient")

    denied = await app_context.runs.approve(
        waiting.id, actor="reviewer", allow=False, reason="unsafe"
    )

    assert denied.status is RunStatus.FAILED
    assert denied.pending_approval is False
    assert denied.result == {"code": "approval_denied", "reason": "unsafe"}
    assert app_context.backend.rollback_preparations == 0
    events = app_context.runs.store.list_events(denied.trace_id)
    denied_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "approval.denied"
    )
    assert events[denied_index].payload == {
        "actor": "reviewer",
        "reason": "unsafe",
        "action_step": events[denied_index - 1].payload["action_step"],
        "action_fingerprint": events[denied_index - 1].payload["action_fingerprint"],
    }
    assert events[denied_index + 1].event_type == "run.failed"
    assert events[denied_index + 1].payload == {
        "code": "approval_denied",
        "reason": "unsafe",
    }
    assert events[-1] == events[denied_index + 1]
    assert not any(
        event.event_type.startswith("tool.attempt.")
        for event in events[denied_index + 1 :]
    )
