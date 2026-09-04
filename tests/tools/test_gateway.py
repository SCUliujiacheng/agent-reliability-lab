"""Behavioural tests for the schema-first tool execution boundary."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)
from agent_reliability_lab.tools.faults import (
    FaultKind,
    FaultPlan,
    FaultRule,
    no_faults,
    timeout_on_attempt,
)
from agent_reliability_lab.tools.gateway import (
    PermanentToolError,
    ToolGateway,
    action_fingerprint,
)
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


def _call(name: str, *, key: str | None = None, **arguments: object) -> CallToolAction:
    return CallToolAction(tool_name=name, arguments=arguments, idempotency_key=key)


def _gateway(
    tmp_path: Path,
    *,
    sleeper: Any | None = None,
    clock: Any | None = None,
) -> tuple[ToolGateway, Run]:
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
            clock=clock,
            incident_backend=backend,
        ),
        run,
    )


def _approve(
    gateway: ToolGateway, run: Run, action: CallToolAction, *, allow: bool = True
) -> None:
    stable = gateway.stabilize_action(run, action)
    gateway.store.record_approval(
        run.id,
        actor="reviewer",
        allow=allow,
        action_step=run.current_step,
        action_fingerprint=action_fingerprint(stable),
        reason="ok",
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
    assert gateway.events[-1].payload["output"] == result.output


def test_malformed_fault_traverses_authoritative_output_validation(
    tmp_path: Path,
) -> None:
    """An injector-manufactured error code cannot prove schema rejection."""
    gateway, run = _gateway(tmp_path)
    faults = FaultPlan(
        (
            FaultRule(
                tool_name="get_deployment",
                attempt=1,
                kind=FaultKind.MALFORMED_OUTPUT,
            ),
        )
    )

    result = gateway.call_sync(run, _call("get_deployment"), faults)

    assert result.error_code == "invalid_output"
    assert [event.event_type for event in gateway.events] == [
        "tool.attempt.started",
        "fault.injected",
        "tool.output.validation_failed",
        "tool.attempt.failed",
    ]
    validation = gateway.events[2]
    assert validation.payload == {
        "tool_name": "get_deployment",
        "attempt": 1,
        "action_step": 0,
        "code": "invalid_output",
        "source": "output_model",
    }
    assert [event.event_type for event in gateway.store.list_events(run.trace_id)] == [
        event.event_type for event in gateway.events
    ]


def test_duplicate_idempotency_key_returns_cached_write_once(tmp_path: Path) -> None:
    """Dropping durable claim/cache protection around a write must fail."""
    gateway, run = _gateway(tmp_path)
    action = _call(
        "prepare_rollback", key="rollback-42", deployment_id="deploy-2026-09-04-001"
    )
    _approve(gateway, run, action)

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

    async def permanent(_: Input, __: ToolExecutionContext) -> Output:
        nonlocal calls
        calls += 1
        raise PermanentToolError("bad_request", "never retry")

    async def malformed(_: Input, __: ToolExecutionContext) -> dict[str, str]:
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

    async def slow(_: Input, __: ToolExecutionContext) -> Output:
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


def test_high_risk_write_derives_a_stable_idempotency_key(tmp_path: Path) -> None:
    """A provider-style write without a key must still execute at most once."""
    gateway, run = _gateway(tmp_path)
    action = _call("prepare_rollback", deployment_id="deploy-2026-09-04-001")
    _approve(gateway, run, action)

    first = gateway.call_sync(run, action)
    second = gateway.call_sync(run, action)

    assert first.status == "succeeded"
    assert second.cached is True
    assert gateway.incident_backend.rollback_preparations == 1


def test_write_and_high_risk_definitions_must_declare_idempotency() -> None:
    """Registry contracts must not permit an accidentally replayable write."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def handler(_: Input, __: ToolExecutionContext) -> Output:
        return Output()

    with pytest.raises(ValueError, match="idempotent"):
        ToolDefinition("write", Input, Output, handler, is_write=True)
    with pytest.raises(ValueError, match="idempotent"):
        ToolDefinition("high", Input, Output, handler, requires_approval=True)


def test_concurrent_high_risk_write_executes_once(tmp_path: Path) -> None:
    """A contender must observe the claim while the owner handler is blocked."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        prepared: bool

    handler_started = asyncio.Event()
    release_owner = asyncio.Event()
    invocations = 0

    async def blocked_write(_: Input, __: ToolExecutionContext) -> Output:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            handler_started.set()
            await release_owner.wait()
        return Output(prepared=True)

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "blocked_write",
            Input,
            Output,
            blocked_write,
            is_write=True,
            idempotent=True,
        )
    )
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "overlap.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("overlap", "resilient"))
    first = ToolGateway(store, TraceRecorder(store), registry)
    second = ToolGateway(store, TraceRecorder(store), registry)
    action = _call("blocked_write", key="concurrent-write")

    async def invoke() -> tuple[object, object]:
        owner_task = asyncio.create_task(first.call(run, action))
        await handler_started.wait()
        contender = await second.call(run, action)
        release_owner.set()
        return await owner_task, contender

    owner, contender = asyncio.run(invoke())

    assert owner.status == "succeeded"
    assert contender.error_code == "idempotency_in_progress"
    assert invocations == 1


def test_side_effect_before_invalid_output_is_not_replayed(tmp_path: Path) -> None:
    """An uncertain write result must be terminal under its original key."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    mutations: list[str] = []

    async def unsafe_write(_: Input, context: ToolExecutionContext) -> dict[str, str]:
        mutations.append(context.idempotency_token)
        return {"wrong": "output"}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "unsafe_write", Input, Output, unsafe_write, is_write=True, idempotent=True
        )
    )
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "unsafe.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("unsafe", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)
    action = _call("unsafe_write", key="once", value="x")

    first = gateway.call_sync(run, action)
    second = gateway.call_sync(run, action)

    assert first.error_code == "invalid_output"
    assert second.error_code == "idempotency_indeterminate"
    assert mutations == ["once"]


def test_cancelled_read_claim_is_released_and_can_recover(tmp_path: Path) -> None:
    """Cancellation must not strand an owned read claim forever."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    started = asyncio.Event()
    calls = 0

    async def cancellable(_: Input, __: ToolExecutionContext) -> Output:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return Output(answer="recovered")

    registry = ToolRegistry()
    registry.register(ToolDefinition("cancellable", Input, Output, cancellable))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "cancel.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("cancel", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)
    action = _call("cancellable", key="recover", value="x")

    async def cancel_then_retry() -> object:
        task = asyncio.create_task(gateway.call(run, action))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await gateway.call(run, action)

    recovered = asyncio.run(cancel_then_retry())

    assert recovered.status == "succeeded"
    assert calls == 2
    cancelled = [
        event
        for event in gateway.events
        if event.event_type == "tool.attempt.cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0].status == "error"


def test_idempotency_key_conflicts_on_changed_tool_or_arguments(tmp_path: Path) -> None:
    """One key must never return a cache entry for a different request."""
    gateway, run = _gateway(tmp_path)
    approved = _call(
        "prepare_rollback",
        key="bound",
        deployment_id="deploy-2026-09-04-001",
    )
    _approve(gateway, run, approved)

    first = gateway.call_sync(run, approved)
    tool_mismatch = gateway.call_sync(run, _call("get_service_health", key="bound"))
    args_mismatch = gateway.call_sync(
        run,
        _call(
            "prepare_rollback",
            key="bound",
            deployment_id="deploy-2026-09-04-002",
        ),
    )

    assert first.status == "succeeded"
    assert tool_mismatch.error_code == "idempotency_conflict"
    assert args_mismatch.error_code == "approval_mismatch"


def test_attempt_events_share_span_parent_and_terminal_status(tmp_path: Path) -> None:
    """A trace that splits one attempt across spans cannot be causally audited."""
    gateway, run = _gateway(tmp_path)
    gateway.call_sync(
        run,
        _call("search_recent_logs"),
        timeout_on_attempt(1, tool_name="search_recent_logs"),
    )

    first_attempt = gateway.events[:3]
    second_attempt = gateway.events[3:]
    assert {event.span_id for event in first_attempt} == {first_attempt[0].span_id}
    assert {event.span_id for event in second_attempt} == {second_attempt[0].span_id}
    assert first_attempt[0].parent_span_id == run.trace_id
    assert first_attempt[-1].status == "error"
    assert second_attempt[-1].status == "ok"


def test_successful_attempt_records_controlled_monotonic_duration(
    tmp_path: Path,
) -> None:
    """Removing handler latency from terminal events must fail this test."""
    readings = iter((10.0, 10.125))
    gateway, run = _gateway(tmp_path, clock=lambda: next(readings))

    result = gateway.call_sync(run, _call("get_deployment"))

    assert result.status == "succeeded"
    assert gateway.events[0].event_type == "tool.attempt.started"
    assert gateway.events[0].duration_ms is None
    terminal = gateway.events[-1]
    assert terminal.event_type == "tool.attempt.succeeded"
    assert terminal.duration_ms == pytest.approx(125.0)
    stored = gateway.store.list_events(run.trace_id)
    assert stored[-1].duration_ms == pytest.approx(125.0)


def test_unknown_fault_tool_is_rejected_before_execution(tmp_path: Path) -> None:
    """A typo in a deterministic fault plan must not be silently ignored."""
    gateway, run = _gateway(tmp_path)
    faults = FaultPlan(
        (FaultRule(tool_name="does_not_exist", attempt=1, kind="timeout"),)
    )

    result = gateway.call_sync(run, _call("search_recent_logs"), faults)

    assert result.error_code == "invalid_fault_plan"
    assert gateway.events == []


def test_nan_write_completion_is_indeterminate_and_never_replayed(
    tmp_path: Path,
) -> None:
    """A write whose schema-valid output cannot be stored must not be reclaimed."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        measurement: float

    side_effects = 0

    async def nan_write(_: Input, __: ToolExecutionContext) -> Output:
        nonlocal side_effects
        side_effects += 1
        return Output(measurement=float("nan"))

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "nan_write", Input, Output, nan_write, is_write=True, idempotent=True
        )
    )
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "nan-write.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("nan-write", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)
    action = _call("nan_write", key="nan-once")

    first = gateway.call_sync(run, action)
    second = gateway.call_sync(run, action)

    assert first.error_code == "idempotency_indeterminate"
    assert second.error_code == "idempotency_indeterminate"
    assert side_effects == 1


def test_backoff_cancellation_keeps_one_terminal_event_per_attempt(
    tmp_path: Path,
) -> None:
    """Cancelling retry sleep must not add a second terminal event to attempt one."""
    retry_sleep_started = asyncio.Event()

    async def blocked_sleeper(_: float) -> None:
        retry_sleep_started.set()
        await asyncio.Event().wait()

    gateway, run = _gateway(tmp_path, sleeper=blocked_sleeper)

    async def cancel_during_backoff() -> None:
        task = asyncio.create_task(
            gateway.call(
                run,
                _call("search_recent_logs", key="backoff-cancel"),
                timeout_on_attempt(1, tool_name="search_recent_logs"),
            )
        )
        await retry_sleep_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_backoff())

    terminal_types = {
        "tool.attempt.failed",
        "tool.attempt.succeeded",
        "tool.attempt.cancelled",
    }
    terminal_events = [
        event for event in gateway.events if event.event_type in terminal_types
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].event_type == "tool.attempt.failed"
    retry_cancelled = [
        event for event in gateway.events if event.event_type == "tool.retry.cancelled"
    ]
    assert len(retry_cancelled) == 1
    assert retry_cancelled[0].status == "error"
    assert retry_cancelled[0].span_id != terminal_events[0].span_id


def test_cancelled_idempotent_write_reenters_with_stable_context_once(
    tmp_path: Path,
) -> None:
    """Cancelling a replay-safe write must not force duplicate or manual recovery."""

    class Input(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid")
        answer: str

    started = asyncio.Event()
    results_by_token: dict[str, Output] = {}
    side_effects = 0

    async def replay_safe_write(value: Input, context: ToolExecutionContext) -> Output:
        nonlocal side_effects
        existing = results_by_token.get(context.idempotency_token)
        if existing is not None:
            return existing
        side_effects += 1
        result = Output(answer=value.value)
        results_by_token[context.idempotency_token] = result
        started.set()
        await asyncio.Event().wait()
        return result

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "replay_safe_write",
            Input,
            Output,
            replay_safe_write,
            is_write=True,
            idempotent=True,
        )
    )
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "cancel-write.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("cancel-write", "resilient"))
    gateway = ToolGateway(store, TraceRecorder(store), registry)
    action = _call("replay_safe_write", key="stable-write", value="done")

    async def cancel_then_resume() -> object:
        task = asyncio.create_task(gateway.call(run, action))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return await gateway.call(run, action)

    recovered = asyncio.run(cancel_then_resume())

    assert recovered.status == "succeeded"
    assert recovered.output == {"answer": "done"}
    assert side_effects == 1
