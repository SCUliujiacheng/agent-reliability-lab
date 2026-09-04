"""Resume and durable cursor behavior."""

import pytest

from agent_reliability_lab.domain.runs import RunStatus
from agent_reliability_lab.runtime.service import RunConflictError


@pytest.mark.asyncio
async def test_resume_rejects_terminal_and_currently_running_runs(
    app_context: object,
) -> None:
    """Permitting terminal or live-run resume would fork state-machine ownership."""
    waiting = await app_context.runs.start("rollback-approval", "resilient")
    assert waiting.pending_action_fingerprint is not None
    completed = await app_context.runs.approve(
        waiting.id,
        actor="reviewer",
        allow=True,
        expected_action_step=waiting.current_step,
        expected_action_fingerprint=waiting.pending_action_fingerprint,
    )
    assert completed.status is RunStatus.SUCCEEDED

    with pytest.raises(RunConflictError, match="terminal"):
        await app_context.runs.resume(completed.id)

    queued = app_context.runs.store.save_run(
        app_context.runs.new_run("rollback-approval", "resilient")
    )
    owned = app_context.runs.store.claim_run_execution(
        queued.id, owner_token="active-worker"
    )
    stored = app_context.runs.store.save_run(
        owned.transition(RunStatus.RUNNING), expected_version=owned.version
    )
    with pytest.raises(RunConflictError, match="live owner"):
        await app_context.runs.resume(stored.id)


@pytest.mark.asyncio
async def test_service_lists_and_gets_canonical_persisted_runs(
    app_context: object,
) -> None:
    """Serving stale pre-save snapshots would expose the wrong durable version."""
    created = await app_context.runs.start("rollback-approval", "resilient")

    fetched = app_context.runs.get(created.id)
    listed = app_context.runs.list()

    assert fetched == created
    assert listed == [created]
    assert created.version > 1
