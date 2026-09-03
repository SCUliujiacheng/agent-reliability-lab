"""Behavioural tests for the durable SQLite run store."""

from concurrent.futures import ThreadPoolExecutor
from math import nan
from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import ToolClaimState, TraceEvent
from agent_reliability_lab.storage.store import ConcurrentUpdateError, SQLiteRunStore


def store_at(path: Path, secret_values: set[str] | None = None) -> SQLiteRunStore:
    return SQLiteRunStore.from_settings(
        Settings(data_dir=path.parent, database_path=path), secret_values=secret_values
    )


def test_from_settings_rejects_database_outside_data_dir(tmp_path: Path) -> None:
    """Allowing a store path to escape its data root must fail this test."""
    settings = Settings(data_dir=tmp_path / "data", database_path=tmp_path / "runs.db")

    with pytest.raises(ValueError, match="data directory"):
        SQLiteRunStore.from_settings(settings)


def test_run_survives_store_reconstruction(tmp_path: Path) -> None:
    """Replacing durable storage with process-local state must fail this test."""
    path = tmp_path / "runs.db"
    first = store_at(path)
    first.create_schema()
    run = Run.new("incident-timeout", "resilient")
    first.save_run(run)

    second = store_at(path)

    assert second.get_run(run.id) == run.model_copy(update={"version": 1})


def test_events_receive_unique_stable_sequences_under_concurrency(
    tmp_path: Path,
) -> None:
    """A racing MAX+1 allocator must fail this test."""
    store = store_at(tmp_path / "events.db")
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
    store = store_at(tmp_path / "audit.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    assert (
        store.claim_tool_execution(run.id, "lookup-1", owner_token="worker-a").state
        is ToolClaimState.CLAIMED
    )
    assert (
        store.claim_tool_execution(
            run.id, "lookup-1", owner_token="worker-b"
        ).owner_token
        == "worker-a"
    )
    store.complete_tool_result(
        run.id, "lookup-1", {"status": "ok"}, owner_token="worker-a"
    )
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
    store = store_at(tmp_path / "claims.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda index: (
                    str(index),
                    store.claim_tool_execution(run.id, "key-1", owner_token=str(index)),
                ),
                range(8),
            )
        )

    winners = [token for token, claim in claims if claim.owner_token == token]
    assert len(winners) == 1
    store.complete_tool_result(run.id, "key-1", {"ok": True}, owner_token=winners[0])
    assert store.get_tool_result(run.id, "key-1") == {"ok": True}


def test_run_save_uses_compare_and_swap_versioning(tmp_path: Path) -> None:
    """Overwriting a stale run snapshot must fail this test."""
    store = store_at(tmp_path / "runs.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    version = store.save_run(run)
    changed = run.transition("running")

    assert store.save_run(changed, expected_version=version) == version + 1
    with pytest.raises(ConcurrentUpdateError):
        store.save_run(run, expected_version=version)


def test_store_rejects_nan_json_values(tmp_path: Path) -> None:
    """Serializing non-standard JSON must fail this test."""
    store = store_at(tmp_path / "json.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    with pytest.raises(ValueError):
        store.save_tool_result(run.id, "nan", {"value": nan})


def test_reloaded_run_exposes_version_for_a_following_compare_and_swap(
    tmp_path: Path,
) -> None:
    """A reconstructed run without its durable version must fail this test."""
    store = store_at(tmp_path / "version.db")
    store.create_schema()
    original = Run.new("incident-timeout", "resilient")
    persisted_version = store.save_run(original)
    loaded = store.get_run(original.id)

    assert loaded is not None
    assert loaded.version == persisted_version
    updated = loaded.transition("running")
    assert (
        store.save_run(updated, expected_version=loaded.version) == loaded.version + 1
    )
    with pytest.raises(ConcurrentUpdateError):
        store.save_run(loaded, expected_version=loaded.version)


def test_tool_claim_status_distinguishes_absent_claimed_completed_and_failed(
    tmp_path: Path,
) -> None:
    """Collapsing absent and claimed work into None must fail this test."""
    store = store_at(tmp_path / "states.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    absent = store.get_tool_claim(run.id, "key")
    assert absent.state is ToolClaimState.ABSENT
    first = store.claim_tool_execution(run.id, "key", owner_token="one")
    assert first.state is ToolClaimState.CLAIMED
    assert first.owner_token == "one"
    assert (
        store.claim_tool_execution(run.id, "key", owner_token="two").owner_token
        == "one"
    )
    store.fail_tool_execution(run.id, "key", owner_token="one", error="network")
    assert store.get_tool_claim(run.id, "key").state is ToolClaimState.FAILED
    second = store.claim_tool_execution(run.id, "key", owner_token="two")
    store.complete_tool_result(run.id, "key", {"ok": True}, owner_token="two")
    assert second.owner_token == "two"
    assert store.get_tool_claim(run.id, "key").state is ToolClaimState.COMPLETED


def test_public_constructor_rejects_arbitrary_sqlite_url(tmp_path: Path) -> None:
    """A public constructor that can escape Settings.data_dir must fail."""
    with pytest.raises((TypeError, ValueError)):
        SQLiteRunStore(f"sqlite:///{(tmp_path.parent / 'escaped.db').as_posix()}")  # type: ignore[arg-type]


def test_failed_result_serialization_releases_owned_claim(tmp_path: Path) -> None:
    """A NaN completion must leave a later owner able to reclaim the work."""
    store = store_at(tmp_path / "recovery.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    assert (
        store.claim_tool_execution(run.id, "key", owner_token="one").owner_token
        == "one"
    )
    with pytest.raises(ValueError):
        store.complete_tool_result(run.id, "key", {"value": nan}, owner_token="one")
    assert store.get_tool_claim(run.id, "key").state is ToolClaimState.FAILED
    assert (
        store.claim_tool_execution(run.id, "key", owner_token="two").owner_token
        == "two"
    )
