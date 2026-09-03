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

    with pytest.raises(ValueError, match="already recorded"):
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
