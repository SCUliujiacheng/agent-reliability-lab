"""Behavioral regressions required by Task 4 review round 1."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction, FinishAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import FaultRule, FaultType, Scenario
from agent_reliability_lab.providers.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatiblePolicy,
)
from agent_reliability_lab.runtime.policies import Policy
from agent_reliability_lab.runtime.service import RunConflictError, RunService
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


class BranchingPolicy:
    """Return a chosen write at step zero and finish thereafter."""

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id

    async def next_action(self, run: Run, _: Scenario) -> object:
        if run.current_step == 0:
            return CallToolAction(
                tool_name="prepare_rollback",
                arguments={"deployment_id": self.deployment_id},
            )
        return FinishAction(summary="prepared", outcome="done")


class BlockingPolicy:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def next_action(self, _: Run, __: Scenario) -> object:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def one_step_scenario(scenario_id: str = "dynamic") -> Scenario:
    return Scenario(
        id=scenario_id,
        version=1,
        actions=(FinishAction(summary="done", outcome="done"),),
        expected_outcome="done",
    )


def service_at(
    path: Path,
    scenarios: dict[str, Scenario],
    *,
    policies: dict[str, Policy] | None = None,
    clock: Callable[[], datetime] | None = None,
    run_lease_seconds: float = 30,
) -> tuple[RunService, IncidentBackend]:
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=path.parent, database_path=path),
        clock=clock,
        run_lease_seconds=run_lease_seconds,
    )
    store.create_schema()
    recorder = TraceRecorder(store)
    backend = IncidentBackend()
    gateway = ToolGateway(store, recorder, incident_registry(backend))
    return (
        RunService(
            store,
            recorder,
            gateway,
            scenarios.__getitem__,
            policies=policies,
        ),
        backend,
    )


@pytest.mark.asyncio
async def test_approval_executes_persisted_action_not_requeried_policy(
    tmp_path: Path,
) -> None:
    """A reconstructed non-deterministic policy must not swap the approved write."""
    path = tmp_path / "pending-action.db"
    scenarios = {"dynamic": one_step_scenario()}
    first, _ = service_at(
        path, scenarios, policies={"branch": BranchingPolicy("deploy-A")}
    )
    waiting = await first.start("dynamic", "resilient", policy_name="branch")

    assert waiting.pending_action is not None
    assert waiting.pending_action.arguments == {"deployment_id": "deploy-A"}
    assert waiting.pending_action_fingerprint

    reconstructed, backend = service_at(
        path, scenarios, policies={"branch": BranchingPolicy("deploy-B")}
    )
    completed = await reconstructed.approve(waiting.id, actor="reviewer", allow=True)

    assert completed.status is RunStatus.SUCCEEDED
    assert completed.context["tool_results"]["0"]["deployment_id"] == "deploy-A"
    assert completed.pending_action is None
    assert completed.pending_action_fingerprint is None
    assert backend.rollback_preparations == 1


@pytest.mark.asyncio
async def test_same_approval_retries_after_record_before_resume_crash(
    tmp_path: Path,
) -> None:
    """A crash after decision insert must not make the approved run unrecoverable."""
    path = tmp_path / "approval-crash.db"
    scenarios = {"dynamic": one_step_scenario()}
    service, _ = service_at(
        path, scenarios, policies={"branch": BranchingPolicy("deploy-A")}
    )
    waiting = await service.start("dynamic", "resilient", policy_name="branch")
    assert waiting.pending_action_fingerprint is not None
    service.store.record_approval(
        waiting.id,
        actor="reviewer",
        allow=True,
        action_step=waiting.current_step,
        action_fingerprint=waiting.pending_action_fingerprint,
    )

    reconstructed, backend = service_at(
        path, scenarios, policies={"branch": BranchingPolicy("deploy-B")}
    )
    completed = await reconstructed.approve(waiting.id, actor="reviewer", allow=True)

    assert completed.status is RunStatus.SUCCEEDED
    assert backend.rollback_preparations == 1


def test_run_execution_lease_has_one_live_owner_and_expiry_reclaim(
    tmp_path: Path,
) -> None:
    """A live run owner must exclude peers while an expired owner is reclaimable."""
    now = datetime(2026, 9, 4, tzinfo=UTC)
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "run-lease.db"),
        clock=lambda: now,
        run_lease_seconds=10,
    )
    store.create_schema()
    run = store.save_run(Run.new("lease", "resilient"))

    first = store.claim_run_execution(run.id, owner_token="worker-a")
    assert first.execution_owner == "worker-a"
    assert first.execution_lease_expires_at == now + timedelta(seconds=10)
    with pytest.raises(RuntimeError, match="live owner"):
        store.claim_run_execution(run.id, owner_token="worker-b")

    now += timedelta(seconds=11)
    reclaimed = store.claim_run_execution(run.id, owner_token="worker-b")
    assert reclaimed.execution_owner == "worker-b"


@pytest.mark.asyncio
async def test_cancellation_releases_run_execution_owner(tmp_path: Path) -> None:
    """Cancellation must leave a running run immediately reclaimable."""
    scenario = one_step_scenario("blocking")
    blocker = BlockingPolicy()
    service, _ = service_at(
        tmp_path / "cancel-run.db",
        {scenario.id: scenario},
        policies={"blocking": blocker},
    )

    task = asyncio.create_task(
        service.start(scenario.id, "resilient", policy_name="blocking")
    )
    await blocker.started.wait()
    running = service.list()[0]
    with pytest.raises(RunConflictError, match="live owner"):
        await service.resume(running.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    released = service.get(running.id)
    assert released.status is RunStatus.RUNNING
    assert released.execution_owner is None


@pytest.mark.asyncio
async def test_expired_running_execution_is_resumed(tmp_path: Path) -> None:
    """Process death must not strand a running run after its owner lease expires."""
    now = datetime(2026, 9, 4, tzinfo=UTC)
    scenario = one_step_scenario("expired")
    service, _ = service_at(
        tmp_path / "expired-run.db",
        {scenario.id: scenario},
        clock=lambda: now,
        run_lease_seconds=10,
    )
    queued = service.store.save_run(service.new_run(scenario.id, "resilient"))
    owned = service.store.claim_run_execution(queued.id, owner_token="crashed")
    running = service.store.save_run(
        owned.transition(RunStatus.RUNNING), expected_version=owned.version
    )

    now += timedelta(seconds=11)
    completed = await service.resume(running.id)

    assert completed.status is RunStatus.SUCCEEDED
    assert completed.execution_owner is None


@pytest.mark.asyncio
async def test_standard_function_write_gets_stable_key_and_passes_preflight(
    tmp_path: Path,
) -> None:
    """A standard function call for a high-risk tool must be approvable and executable."""
    responses = iter(
        [
            {
                "id": "chatcmpl-write",
                "object": "chat.completion",
                "created": 1788500000,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-write",
                                    "type": "function",
                                    "function": {
                                        "name": "prepare_rollback",
                                        "arguments": '{"deployment_id":"deploy-A"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
            {
                "id": "chatcmpl-finish",
                "object": "chat.completion",
                "created": 1788500001,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"type":"finish","summary":"done","outcome":"done"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        ]
    )
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses)))
    scenario = Scenario(
        id="provider-write",
        version=1,
        actions=(
            CallToolAction(
                tool_name="prepare_rollback",
                arguments={"deployment_id": "deploy-A"},
            ),
        ),
        expected_outcome="done",
    )
    async with httpx.AsyncClient(transport=transport) as client:
        policy = OpenAICompatiblePolicy(
            OpenAICompatibleConfig(
                base_url="https://provider.example/v1",
                model="model",
                api_key_env="MISSING_TEST_KEY",
            ),
            client=client,
        )
        service, backend = service_at(
            tmp_path / "provider-write.db",
            {scenario.id: scenario},
            policies={"provider": policy},
        )
        waiting = await service.start(scenario.id, "resilient", policy_name="provider")
        assert waiting.status is RunStatus.WAITING_APPROVAL
        assert waiting.pending_action is not None
        assert waiting.pending_action.idempotency_key

        completed = await service.approve(waiting.id, actor="reviewer", allow=True)

    assert completed.status is RunStatus.SUCCEEDED
    assert backend.rollback_preparations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_type", "terminal", "code"),
    [
        (FaultType.TIMEOUT, RunStatus.SUCCEEDED, "tool_timeout"),
        (FaultType.RATE_LIMIT, RunStatus.SUCCEEDED, "rate_limit"),
        (FaultType.TOOL_ERROR, RunStatus.SUCCEEDED, "tool_error"),
        (FaultType.MALFORMED_OUTPUT, RunStatus.FAILED, "invalid_output"),
    ],
)
async def test_scenario_faults_reach_gateway(
    app_context: object,
    fault_type: FaultType,
    terminal: RunStatus,
    code: str,
) -> None:
    """Ignoring scenario faults would make deterministic benchmarks fictitious."""
    scenario = Scenario(
        id=f"fault-{fault_type}",
        version=1,
        actions=(
            CallToolAction(tool_name="get_deployment"),
            FinishAction(summary="done", outcome="done"),
        ),
        expected_outcome="done",
        faults=(FaultRule(tool_name="get_deployment", attempt=1, type=fault_type),),
    )
    app_context.scenarios[scenario.id] = scenario

    run = await app_context.runs.start(scenario.id, "resilient")

    assert run.status is terminal
    events = app_context.runs.store.list_events(run.trace_id)
    injected = [event for event in events if event.event_type == "fault.injected"]
    assert len(injected) == 1
    assert injected[0].payload["code"] == code


@pytest.mark.asyncio
async def test_fragile_mode_does_not_recover_transient_scenario_fault(
    app_context: object,
) -> None:
    """A fragile baseline that retries like resilient mode invalidates comparison."""
    scenario = Scenario(
        id="fragile-timeout",
        version=1,
        actions=(
            CallToolAction(tool_name="get_deployment"),
            FinishAction(summary="done", outcome="done"),
        ),
        expected_outcome="done",
        faults=(
            FaultRule(tool_name="get_deployment", attempt=1, type=FaultType.TIMEOUT),
        ),
    )
    app_context.scenarios[scenario.id] = scenario

    fragile = await app_context.runs.start(scenario.id, "fragile")
    resilient = await app_context.runs.start(scenario.id, "resilient")

    assert fragile.status is RunStatus.FAILED
    assert fragile.result == {"code": "tool_timeout"}
    assert resilient.status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_run_transitions_and_rejected_operations_are_traced(
    app_context: object,
) -> None:
    """Missing accepted/rejected state events prevents operational audit."""
    waiting = await app_context.runs.start("rollback-approval", "resilient")
    completed = await app_context.runs.approve(waiting.id, actor="reviewer", allow=True)

    with pytest.raises(RunConflictError):
        await app_context.runs.resume(completed.id)
    with pytest.raises(ValueError, match="conflict"):
        await app_context.runs.approve(completed.id, actor="reviewer", allow=False)

    events = app_context.runs.store.list_events(completed.trace_id)
    event_types = [event.event_type for event in events]
    assert event_types.count("run.running") >= 2
    running_events = [event for event in events if event.event_type == "run.running"]
    assert all("owner_token" not in event.payload for event in running_events)
    assert "run.resume.rejected" in event_types
    assert "approval.rejected" in event_types
