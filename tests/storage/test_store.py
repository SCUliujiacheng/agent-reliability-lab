"""Behavioural tests for the durable SQLite run store."""

from concurrent.futures import ThreadPoolExecutor
from math import nan
from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import ConcurrentUpdateError, SQLiteRunStore


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_from_settings_rejects_database_outside_data_dir(tmp_path: Path) -> None:
    """Allowing a store path to escape its data root must fail this test."""
    settings = Settings(data_dir=tmp_path / "data", database_path=tmp_path / "runs.db")

    with pytest.raises(ValueError, match="data directory"):
        SQLiteRunStore.from_settings(settings)


def test_run_survives_store_reconstruction(tmp_path: Path) -> None:
    """Replacing durable storage with process-local state must fail this test."""
    database_url = sqlite_url(tmp_path / "runs.db")
    first = SQLiteRunStore.for_testing(database_url)
    first.create_schema()
    run = Run.new("incident-timeout", "resilient")
    first.save_run(run)

    second = SQLiteRunStore.for_testing(database_url)

    assert second.get_run(run.id) == run


def test_events_receive_unique_stable_sequences_under_concurrency(
    tmp_path: Path,
) -> None:
    """A racing MAX+1 allocator must fail this test."""
    store = SQLiteRunStore.for_testing(sqlite_url(tmp_path / "events.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    def append(index: int) -> TraceEvent:
        return store.append_event(
            TraceEvent.new(run.trace_id, "tool.completed", {"attempt": index})
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        inserted = list(pool.map(append, range(100)))

    stored = store.list_events(run.trace_id)
    assert sorted(event.sequence for event in inserted) == list(range(1, 101))
    assert [event.sequence for event in stored] == list(range(1, 101))
    assert [event.id for event in stored] == [
        event.id for event in sorted(stored, key=lambda e: (e.sequence, str(e.id)))
    ]


def test_store_persists_tool_results_approvals_and_evaluations(tmp_path: Path) -> None:
    """Dropping an auxiliary audit record must make this test fail."""
    store = SQLiteRunStore.for_testing(sqlite_url(tmp_path / "audit.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    assert store.claim_tool_execution(run.id, "lookup-1")
    assert not store.claim_tool_execution(run.id, "lookup-1")
    store.complete_tool_result(run.id, "lookup-1", {"status": "ok"})
    store.record_approval(
        run.id, actor="operator", allow=True, reason="validated incident scope"
    )
    store.save_evaluation(run.id, "recovery", {"score": 1.0})

    assert store.get_tool_result(run.id, "lookup-1") == {"status": "ok"}
    approval = store.get_approval(run.id)
    assert approval is not None
    assert approval["actor"] == "operator"
    assert approval["allow"] is True
    assert approval["reason"] == "validated incident scope"
    assert store.get_evaluation(run.id, "recovery") == {"score": 1.0}

    with pytest.raises(ValueError, match="already recorded"):
        store.record_approval(run.id, actor="other", allow=False)


def test_tool_claim_allows_only_one_competing_worker_to_execute(tmp_path: Path) -> None:
    """Letting both workers claim an idempotency key must fail this test."""
    store = SQLiteRunStore.for_testing(sqlite_url(tmp_path / "claims.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(lambda _: store.claim_tool_execution(run.id, "key-1"), range(8))
        )

    assert claims.count(True) == 1
    store.complete_tool_result(run.id, "key-1", {"ok": True})
    assert store.get_tool_result(run.id, "key-1") == {"ok": True}


def test_run_save_uses_compare_and_swap_versioning(tmp_path: Path) -> None:
    """Overwriting a stale run snapshot must fail this test."""
    store = SQLiteRunStore.for_testing(sqlite_url(tmp_path / "runs.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    version = store.save_run(run)
    changed = run.transition("running")

    assert store.save_run(changed, expected_version=version) == version + 1
    with pytest.raises(ConcurrentUpdateError):
        store.save_run(run, expected_version=version)


def test_store_rejects_nan_json_values(tmp_path: Path) -> None:
    """Serializing non-standard JSON must fail this test."""
    store = SQLiteRunStore.for_testing(sqlite_url(tmp_path / "json.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    with pytest.raises(ValueError):
        store.save_tool_result(run.id, "nan", {"value": nan})
