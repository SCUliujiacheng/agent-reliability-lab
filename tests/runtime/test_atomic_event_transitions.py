"""State transitions and their audit events must share one SQLite transaction."""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from agent_reliability_lab.domain.runs import RunStatus
from agent_reliability_lab.storage.models import TraceEvent


def _duplicate_event_id_on(
    app_context: Any,
    monkeypatch: pytest.MonkeyPatch,
    target_event_type: str,
) -> None:
    original: Callable[..., TraceEvent] = app_context.recorder.build_event

    def build_event(*args: object, **kwargs: object) -> TraceEvent:
        event = original(*args, **kwargs)
        if event.event_type != target_event_type:
            return event
        existing = app_context.runs.store.list_events(event.trace_id)
        assert existing, "fault injection requires an earlier audit event"
        return event.model_copy(update={"id": existing[0].id})

    monkeypatch.setattr(app_context.recorder, "build_event", build_event)


def test_running_transition_rolls_back_when_event_build_fails(
    app_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original: Callable[..., TraceEvent] = app_context.recorder.build_event

    def build_event(*args: object, **kwargs: object) -> TraceEvent:
        event = original(*args, **kwargs)
        if event.event_type == "run.running":
            raise RuntimeError("injected event build failure")
        return event

    monkeypatch.setattr(app_context.recorder, "build_event", build_event)

    with pytest.raises(RuntimeError, match="injected event build failure"):
        asyncio.run(app_context.runs.start("rollback-approval", "resilient"))

    [stored] = app_context.runs.list()
    assert stored.status is RunStatus.QUEUED
    assert app_context.runs.store.list_events(stored.trace_id) == []


def test_checkpoint_rolls_back_when_event_insert_fails(
    app_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _duplicate_event_id_on(app_context, monkeypatch, "run.checkpointed")

    with pytest.raises(IntegrityError):
        asyncio.run(app_context.runs.start("rollback-approval", "resilient"))

    [stored] = app_context.runs.list()
    assert stored.status is RunStatus.RUNNING
    assert stored.current_step == 0
    assert "tool_results" not in stored.context
    assert not any(
        event.event_type == "run.checkpointed"
        for event in app_context.runs.store.list_events(stored.trace_id)
    )


def test_approval_pause_rolls_back_when_event_insert_fails(
    app_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _duplicate_event_id_on(app_context, monkeypatch, "run.waiting_approval")

    with pytest.raises(IntegrityError):
        asyncio.run(app_context.runs.start("rollback-approval", "resilient"))

    [stored] = app_context.runs.list()
    assert stored.status is RunStatus.RUNNING
    assert stored.current_step == 1
    assert stored.pending_approval is False
    assert stored.pending_action is None
    assert not any(
        event.event_type == "run.waiting_approval"
        for event in app_context.runs.store.list_events(stored.trace_id)
    )


def test_success_rolls_back_when_event_insert_fails(
    app_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.run(app_context.runs.start("rollback-approval", "resilient"))
    assert waiting.pending_action_fingerprint is not None
    _duplicate_event_id_on(app_context, monkeypatch, "run.succeeded")

    with pytest.raises(IntegrityError):
        asyncio.run(
            app_context.runs.approve(
                waiting.id,
                actor="reviewer",
                allow=True,
                expected_action_step=waiting.current_step,
                expected_action_fingerprint=waiting.pending_action_fingerprint,
                reason="reviewed",
            )
        )

    stored = app_context.runs.get(waiting.id)
    assert stored.status is RunStatus.RUNNING
    assert stored.current_step == 2
    assert stored.result is None
    assert not any(
        event.event_type == "run.succeeded"
        for event in app_context.runs.store.list_events(stored.trace_id)
    )


def test_approval_denial_rolls_back_state_and_first_event_when_second_event_fails(
    app_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = asyncio.run(app_context.runs.start("rollback-approval", "resilient"))
    assert waiting.pending_action_fingerprint is not None
    events_before = app_context.runs.store.list_events(waiting.trace_id)
    _duplicate_event_id_on(app_context, monkeypatch, "run.failed")

    with pytest.raises(IntegrityError):
        asyncio.run(
            app_context.runs.approve(
                waiting.id,
                actor="reviewer",
                allow=False,
                expected_action_step=waiting.current_step,
                expected_action_fingerprint=waiting.pending_action_fingerprint,
                reason="unsafe",
            )
        )

    stored = app_context.runs.get(waiting.id)
    assert stored.status is RunStatus.WAITING_APPROVAL
    assert stored.pending_approval is True
    assert stored.pending_action is not None
    assert stored.result is None
    events_after = app_context.runs.store.list_events(waiting.trace_id)
    assert events_after[:-1] == events_before
    assert events_after[-1].event_type == "approval.recorded"
    assert events_after[-1].payload["allow"] is False
    assert not any(
        event.event_type in {"approval.denied", "run.failed"}
        for event in events_after[len(events_before) :]
    )
