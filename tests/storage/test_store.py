"""Behavioural tests for the durable SQLite run store."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from math import nan
from pathlib import Path
from uuid import UUID

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import (
    ToolClaimState,
    ToolFailureDisposition,
    TraceEvent,
)
from agent_reliability_lab.storage.store import (
    ConcurrentUpdateError,
    SQLiteRunStore,
    ToolResultRow,
)


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
        store.claim_tool_execution(
            run.id,
            "lookup-1",
            owner_token="worker-a",
            request_fingerprint="lookup-request",
        ).state
        is ToolClaimState.CLAIMED
    )
    assert (
        store.claim_tool_execution(
            run.id,
            "lookup-1",
            owner_token="worker-b",
            request_fingerprint="lookup-request",
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
                    store.claim_tool_execution(
                        run.id,
                        "key-1",
                        owner_token=str(index),
                        request_fingerprint="key-request",
                    ),
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
    persisted = store.save_run(run)
    changed = persisted.transition("running")

    assert store.save_run(changed, expected_version=persisted.version).version == 2
    with pytest.raises(ConcurrentUpdateError):
        store.save_run(run, expected_version=persisted.version)


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
    persisted = store.save_run(original)
    loaded = store.get_run(original.id)

    assert loaded is not None
    assert loaded.version == persisted.version
    updated = loaded.transition("running")
    assert (
        store.save_run(updated, expected_version=loaded.version).version
        == loaded.version + 1
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
    first = store.claim_tool_execution(
        run.id, "key", owner_token="one", request_fingerprint="key-request"
    )
    assert first.state is ToolClaimState.CLAIMED
    assert first.owner_token == "one"
    assert (
        store.claim_tool_execution(
            run.id, "key", owner_token="two", request_fingerprint="key-request"
        ).owner_token
        == "one"
    )
    store.fail_tool_execution(run.id, "key", owner_token="one", error="network")
    assert store.get_tool_claim(run.id, "key").state is ToolClaimState.FAILED
    second = store.claim_tool_execution(
        run.id, "key", owner_token="two", request_fingerprint="key-request"
    )
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
        store.claim_tool_execution(
            run.id, "key", owner_token="one", request_fingerprint="key-request"
        ).owner_token
        == "one"
    )
    with pytest.raises(ValueError):
        store.complete_tool_result(run.id, "key", {"value": nan}, owner_token="one")
    assert store.get_tool_claim(run.id, "key").state is ToolClaimState.FAILED
    assert (
        store.claim_tool_execution(
            run.id, "key", owner_token="two", request_fingerprint="key-request"
        ).owner_token
        == "two"
    )


def test_save_run_returns_canonical_versioned_run_for_immediate_next_save(
    tmp_path: Path,
) -> None:
    """Returning only an integer version leaves the immutable run stale."""
    store = store_at(tmp_path / "canonical.db")
    store.create_schema()

    saved = store.save_run(Run.new("incident-timeout", "resilient"))

    assert isinstance(saved, Run)
    running = saved.transition("running")
    persisted = store.save_run(running, expected_version=saved.version)
    assert persisted.version == saved.version + 1


def test_reconstructed_run_can_perform_two_consecutive_compare_and_swap_saves(
    tmp_path: Path,
) -> None:
    """Returning a stale snapshot after one save must fail this test."""
    store = store_at(tmp_path / "two-cas.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    loaded = store.get_run(run.id)

    assert loaded is not None
    first = store.save_run(
        loaded.transition("running"), expected_version=loaded.version
    )
    assert isinstance(first, Run)
    second = store.save_run(
        first.transition("waiting_approval"), expected_version=first.version
    )

    assert second.version == first.version + 1


def test_compatibility_save_tool_result_is_contention_safe(tmp_path: Path) -> None:
    """A shared compatibility owner token lets losers complete work and must fail."""
    store = store_at(tmp_path / "compatibility.db")
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)

    def save(index: int) -> bool:
        return store.save_tool_result(run.id, "same-key", {"winner": index})

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(save, range(12)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 11
    assert store.get_tool_result(run.id, "same-key") is not None


def test_tool_claim_rejects_a_different_request_fingerprint(tmp_path: Path) -> None:
    """A key reused for a different canonical request must report conflict."""
    store = store_at(tmp_path / "fingerprint.db")
    store.create_schema()
    run = store.save_run(Run.new("incident-timeout", "resilient"))

    first = store.claim_tool_execution(
        run.id, "bound", owner_token="one", request_fingerprint="request-a"
    )
    conflict = store.claim_tool_execution(
        run.id, "bound", owner_token="two", request_fingerprint="request-b"
    )

    assert first.state is ToolClaimState.CLAIMED
    assert conflict.state is ToolClaimState.CONFLICT


def test_cached_tool_result_is_sanitized_before_it_reaches_sqlite(
    tmp_path: Path,
) -> None:
    """A cached secret must be redacted in both retrieval and raw SQLite storage."""
    store = store_at(tmp_path / "sanitized-result.db", secret_values={"s3cr3t"})
    store.create_schema()
    run = store.save_run(Run.new("incident-timeout", "resilient"))
    store.claim_tool_execution(
        run.id, "secret", owner_token="worker", request_fingerprint="request"
    )
    store.complete_tool_result(
        run.id, "secret", {"token": "s3cr3t"}, owner_token="worker"
    )

    assert store.get_tool_result(run.id, "secret") == {"token": "[REDACTED]"}
    with store._session() as session:
        row = session.get(ToolResultRow, (str(run.id), "secret"))
    assert row is not None
    assert "s3cr3t" not in (row.payload or "")


def test_expired_claim_is_reclaimed_with_injected_clock(tmp_path: Path) -> None:
    """A process crash must not strand safe work behind a permanent claim."""
    now = datetime(2026, 9, 4, tzinfo=UTC)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "lease.db"),
        clock=lambda: now,
        claim_lease_seconds=30,
    )
    store.create_schema()
    run = store.save_run(Run.new("lease", "resilient"))
    first = store.claim_tool_execution(
        run.id, "key", owner_token="crashed", request_fingerprint="request"
    )
    assert first.lease_expires_at == now + timedelta(seconds=30)

    now += timedelta(seconds=31)
    reclaimed = store.claim_tool_execution(
        run.id, "key", owner_token="recovery", request_fingerprint="request"
    )

    assert reclaimed.state is ToolClaimState.CLAIMED
    assert reclaimed.owner_token == "recovery"
    assert reclaimed.lease_expires_at == now + timedelta(seconds=30)


def test_expired_unsafe_claim_becomes_indeterminate(tmp_path: Path) -> None:
    """An abandoned write without replay safety must require human handling."""
    now = datetime(2026, 9, 4, tzinfo=UTC)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "unsafe-lease.db"),
        clock=lambda: now,
        claim_lease_seconds=10,
    )
    store.create_schema()
    run = store.save_run(Run.new("lease", "resilient"))
    store.claim_tool_execution(
        run.id, "key", owner_token="crashed", request_fingerprint="request"
    )

    now += timedelta(seconds=11)
    claim = store.claim_tool_execution(
        run.id,
        "key",
        owner_token="recovery",
        request_fingerprint="request",
        allow_reclaim=False,
    )

    assert claim.state is ToolClaimState.FAILED
    assert claim.failure_disposition is ToolFailureDisposition.INDETERMINATE
    assert claim.owner_token == "crashed"


def test_list_runs_applies_limit_after_newest_first_ordering(tmp_path: Path) -> None:
    """Limiting by random UUID order must not discard the newest durable run."""
    store = store_at(tmp_path / "list.db")
    store.create_schema()
    base = datetime(2026, 9, 4, tzinfo=UTC)
    oldest = Run.new("oldest", "resilient").model_copy(
        update={"id": UUID(int=3), "created_at": base, "updated_at": base}
    )
    middle = Run.new("middle", "resilient").model_copy(
        update={
            "id": UUID(int=2),
            "created_at": base + timedelta(seconds=1),
            "updated_at": base + timedelta(seconds=1),
        }
    )
    newest = Run.new("newest", "resilient").model_copy(
        update={
            "id": UUID(int=1),
            "created_at": base + timedelta(seconds=2),
            "updated_at": base + timedelta(seconds=2),
        }
    )
    for run in (oldest, middle, newest):
        store.save_run(run)

    assert [run.scenario_id for run in store.list_runs(limit=2)] == [
        "newest",
        "middle",
    ]
