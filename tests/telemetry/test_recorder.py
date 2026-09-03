"""Behavioural tests for sanitized trace recording."""

from pathlib import Path

from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder, sanitize_payload


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_recorder_redacts_nested_authorization_and_secret_values() -> None:
    """Removing recursive redaction must make this test fail."""
    payload = {"Authorization": "Bearer token", "nested": {"key": "s3cr3t"}}

    clean = sanitize_payload(payload, {"s3cr3t"})

    assert clean == {
        "Authorization": "[REDACTED]",
        "nested": {"key": "[REDACTED]"},
    }


def test_recorder_persists_only_sanitized_event_payloads(tmp_path: Path) -> None:
    """Persisting a secret in either event representation must fail this test."""
    store = SQLiteRunStore(sqlite_url(tmp_path / "traces.db"))
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    recorder = TraceRecorder(store, secret_values={"s3cr3t"})

    event = recorder.record(
        run.trace_id,
        "tool.request",
        {"api_key": "s3cr3t", "headers": {"authorization": "Bearer token"}},
    )

    assert event.sequence == 1
    assert store.list_events(run.trace_id)[0].payload == {
        "api_key": "[REDACTED]",
        "headers": {"authorization": "[REDACTED]"},
    }
