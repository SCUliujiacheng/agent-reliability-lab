"""Behavioural tests for the schema-first tool execution boundary."""

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.contracts import ToolDefinition, ToolRegistry
from agent_reliability_lab.tools.faults import no_faults, timeout_on_attempt
from agent_reliability_lab.tools.gateway import PermanentToolError, ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


def _call(name: str, *, key: str | None = None, **arguments: object) -> CallToolAction:
    return CallToolAction(tool_name=name, arguments=arguments, idempotency_key=key)


def _gateway(tmp_path: Path, *, sleeper: Any | None = None) -> tuple[ToolGateway, Run]:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "runs.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("incident-timeout", "resilient"))
    backend = IncidentBackend()
    return (
        ToolGateway(
            store,
            TraceRecorder(store),
            incident_registry(backend),
            sleeper=sleeper,
            incident_backend=backend,
        ),
        run,
    )


def test_gateway_retries_a_transient_timeout_and_records_attempts(
    tmp_path: Path,
) -> None:
    """Removing transient retry or its event audit trail must fail."""
    gateway, run = _gateway(tmp_path)

    result = gateway.call_sync(
        run,
        _call("search_recent_logs"),
        timeout_on_attempt(1, tool_name="search_recent_logs"),
    )

    assert result.status == "succeeded"
    assert result.attempts == 2
    assert [event.event_type for event in gateway.events] == [
        "tool.attempt.started",
        "fault.injected",
        "tool.attempt.failed",
        "tool.attempt.started",
        "tool.attempt.succeeded",
    ]
    assert [event.event_type for event in gateway.store.list_events(run.trace_id)] == [
        event.event_type for event in gateway.events
    ]


def test_duplicate_idempotency_key_returns_cached_write_once(tmp_path: Path) -> None:
    """Dropping durable claim/cache protection around a write must fail."""
    gateway, run = _gateway(tmp_path)
    gateway.store.record_approval(run.id, actor="reviewer", allow=True, reason="ok")
    action = _call(
        "prepare_rollback", key="rollback-42", deployment_id="deploy-2026-09-04-001"
    )

    first = gateway.call_sync(run, action, no_faults())
    second = gateway.call_sync(run, action, no_faults())

    assert first.output == second.output
    assert second.cached is True
    assert gateway.incident_backend.rollback_preparations == 1


def test_unknown_tool_and_invalid_input_fail_closed_before_an_attempt(
    tmp_path: Path,
) -> None:
    """Calling unknown handlers or validating after execution must fail."""
    gateway, run = _gateway(tmp_path)

    unknown = gateway.call_sync(run, _call("not_registered"))
    invalid = gateway.call_sync(
        run,
        _call("prepare_rollback", deployment_id=""),
    )

    assert unknown.status == "failed"
    assert unknown.error_code == "unknown_tool"
    assert invalid.status == "failed"
    assert invalid.error_code == "invalid_input"
    assert gateway.events == []


def test_permanent_error_is_not_retried_and_output_is_validated(tmp_path: Path) -> None:
    """Retrying permanent errors or accepting malformed outputs must fail."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str = Field(min_length=1)

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str = Field(min_length=1)

    calls = 0

    async def permanent(_: Input) -> Output:
        nonlocal calls
        calls += 1
        raise PermanentToolError("bad_request", "never retry")

    async def malformed(_: Input) -> dict[str, str]:
        return {"wrong": "shape"}

    registry = ToolRegistry()
    registry.register(ToolDefinition("permanent", Input, Output, permanent))
    registry.register(ToolDefinition("malformed", Input, Output, malformed))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "custom.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("custom", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)

    permanent_result = gateway.call_sync(run, _call("permanent", value="x"))
    malformed_result = gateway.call_sync(run, _call("malformed", value="x"))

    assert permanent_result.error_code == "bad_request"
    assert permanent_result.attempts == 1
    assert calls == 1
    assert malformed_result.error_code == "invalid_output"
    assert malformed_result.attempts == 1


def test_retry_backoff_is_deterministic_and_uses_injected_sleeper(
    tmp_path: Path,
) -> None:
    """Real-time sleeps or jitter make benchmark attempts non-reproducible."""
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    gateway, run = _gateway(tmp_path, sleeper=sleeper)
    result = gateway.call_sync(
        run,
        _call("search_recent_logs"),
        timeout_on_attempt(1, tool_name="search_recent_logs"),
    )

    assert result.status == "succeeded"
    assert delays == [0.1]
    assert gateway.events[2].payload["retry_delay_seconds"] == 0.1


def test_call_is_async_and_enforces_handler_timeout(tmp_path: Path) -> None:
    """A handler able to run indefinitely must fail with a transient timeout."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    async def slow(_: Input) -> Output:
        await asyncio.sleep(0.02)
        return Output(answer="late")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "slow", Input, Output, slow, timeout_seconds=0.001, max_attempts=1
        )
    )
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "timeout.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("timeout", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)

    result = gateway.call_sync(run, _call("slow", value="x"))

    assert result.status == "failed"
    assert result.error_code == "tool_timeout"
    assert result.attempts == 1
