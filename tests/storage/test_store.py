"""Behavioural tests for the durable SQLite run store."""

from pathlib import Path

from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_run_survives_store_reconstruction(tmp_path: Path) -> None:
    """Replacing durable storage with process-local state must fail this test."""
    database_url = sqlite_url(tmp_path / "runs.db")
    first = SQLiteRunStore(database_url)
    first.create_schema()
    run = Run.new("incident-timeout", "resilient")
    first.save_run(run)

    second = SQLiteRunStore(database_url)

    assert second.get_run(run.id) == run


def test_events_receive_monotonic_sequences_per_trace(tmp_path: Path) -> None:
    """Assigning sequences outside the insert transaction must fail this test."""
    store = SQLiteRunStore(sqlite_url(tmp_path / "events.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    first = store.append_event(
        TraceEvent.new(run.trace_id, "run.started", {"attempt": 1})
    )
    second = store.append_event(
        TraceEvent.new(run.trace_id, "tool.completed", {"attempt": 2})
    )

    assert (first.sequence, second.sequence) == (1, 2)
    assert store.list_events(run.trace_id) == [first, second]


def test_store_persists_tool_results_approvals_and_evaluations(tmp_path: Path) -> None:
    """Dropping an auxiliary audit record must make this test fail."""
    store = SQLiteRunStore(sqlite_url(tmp_path / "audit.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    store.save_tool_result(run.id, "lookup-1", {"status": "ok"})
    store.record_approval(run.id, "approval-1", "approved", {"by": "operator"})
    store.save_evaluation(run.id, "recovery", {"score": 1.0})

    assert store.get_tool_result(run.id, "lookup-1") == {"status": "ok"}
    assert store.get_approval(run.id, "approval-1") == {
        "decision": "approved",
        "details": {"by": "operator"},
    }
    assert store.get_evaluation(run.id, "recovery") == {"score": 1.0}
