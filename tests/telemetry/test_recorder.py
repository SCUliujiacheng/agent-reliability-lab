"""Behavioural tests for sanitized trace recording."""

from pathlib import Path

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder, sanitize_payload


def store_at(path: Path, secret_values: set[str] | None = None) -> SQLiteRunStore:
    return SQLiteRunStore.from_settings(
        Settings(data_dir=path.parent, database_path=path), secret_values=secret_values
    )


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
    store = store_at(tmp_path / "traces.db")
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


def test_redaction_replaces_configured_secret_substrings_and_key_variants() -> None:
    """Leaving embedded secrets or credential key variants visible must fail."""
    clean = sanitize_payload(
        {
            "X-API-KEY": "visible",
            "x_api_key": "visible",
            "message": "upstream rejected Bearer s3cr3t while retrying",
            "nested": {"ToKeN": "visible"},
        },
        {"s3cr3t"},
    )

    assert clean == {
        "X-API-KEY": "[REDACTED]",
        "x_api_key": "[REDACTED]",
        "message": "upstream rejected Bearer [REDACTED] while retrying",
        "nested": {"ToKeN": "[REDACTED]"},
    }


def test_store_sanitizes_direct_event_writes_and_round_trips_span_fields(
    tmp_path: Path,
) -> None:
    """Bypassing TraceRecorder must not allow a secret into storage."""
    store = store_at(tmp_path / "boundary.db", secret_values={"s3cr3t"})
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    event = TraceEvent.new(
        run.trace_id,
        "tool.request",
        {"error": "failed with s3cr3t"},
        duration_ms=12.5,
        status="error",
        attributes={"http.method": "POST"},
    )

    stored = store.append_event(event)

    assert stored.span_id == event.span_id
    assert stored.parent_span_id is None
    assert stored.duration_ms == 12.5
    assert stored.status == "error"
    assert stored.attributes == {"http.method": "POST"}
    assert stored.payload == {"error": "failed with [REDACTED]"}


def test_redaction_ignores_empty_secrets_and_replaces_overlap_longest_first() -> None:
    """An empty secret or shortest-first overlap must not corrupt payload text."""
    clean = sanitize_payload({"message": "abcabc"}, {"", "abc", "abcabc"})

    assert clean == {"message": "[REDACTED]"}


def test_redaction_covers_compound_credentials_without_hiding_metrics() -> None:
    """Missing compound credentials or broad substring matching must fail."""
    clean = sanitize_payload(
        {
            "access_token": "access-value",
            "refresh-token": "refresh-value",
            "oauthClientSecret": "client-value",
            "database_password": "password-value",
            "signing_private_key": "private-key-value",
            "metrics": {
                "access_token_count": 3,
                "refresh_token_latency_ms": 12.5,
                "prompt_tokens": 144,
                "secret_scan_count": 2,
            },
        },
        set(),
    )

    assert clean == {
        "access_token": "[REDACTED]",
        "refresh-token": "[REDACTED]",
        "oauthClientSecret": "[REDACTED]",
        "database_password": "[REDACTED]",
        "signing_private_key": "[REDACTED]",
        "metrics": {
            "access_token_count": 3,
            "refresh_token_latency_ms": 12.5,
            "prompt_tokens": 144,
            "secret_scan_count": 2,
        },
    }
