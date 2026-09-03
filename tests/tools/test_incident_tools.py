"""Tests for the deterministic incident-tool fixture set."""

from pathlib import Path

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway, action_fingerprint
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


def _gateway(tmp_path: Path) -> tuple[ToolGateway, Run, IncidentBackend]:
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "runs.db")
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    run = store.save_run(Run.new("incident-timeout", "resilient"))
    backend = IncidentBackend()
    return (
        ToolGateway(store, TraceRecorder(store), incident_registry(backend)),
        run,
        backend,
    )


def _call(name: str, arguments: dict[str, object] | None = None) -> CallToolAction:
    return CallToolAction(
        tool_name=name,
        arguments=arguments or {},
        idempotency_key="rollback-approval-test"
        if name == "prepare_rollback"
        else None,
    )


def test_incident_reads_return_schema_validated_deterministic_data(
    tmp_path: Path,
) -> None:
    """Replacing incident tools with nondeterministic external calls must fail."""
    gateway, run, _ = _gateway(tmp_path)

    health = gateway.call_sync(run, _call("get_service_health"))
    logs = gateway.call_sync(run, _call("search_recent_logs"))
    deployment = gateway.call_sync(run, _call("get_deployment"))

    assert health.status == "succeeded"
    assert health.output == {"service": "checkout", "status": "degraded"}
    assert logs.output is not None
    assert logs.output["entries"][0]["level"] == "ERROR"
    assert deployment.output == {
        "deployment_id": "deploy-2026-09-04-001",
        "version": "2026.09.04.1",
    }


def test_prepare_rollback_requires_approval_before_one_mutation(tmp_path: Path) -> None:
    """A high-risk incident action without a durable approval must fail."""
    gateway, run, backend = _gateway(tmp_path)
    action = _call(
        "prepare_rollback",
        {"deployment_id": "deploy-2026-09-04-001"},
    )

    denied = gateway.call_sync(run, action)
    gateway.store.record_approval(
        run.id,
        actor="reviewer",
        allow=True,
        action_step=run.current_step,
        action_fingerprint=action_fingerprint(action),
        reason="safe",
    )
    allowed = gateway.call_sync(run, action)

    assert denied.status == "failed"
    assert denied.error_code == "approval_required"
    assert allowed.status == "succeeded"
    assert allowed.output == {
        "deployment_id": "deploy-2026-09-04-001",
        "prepared": True,
    }
    assert backend.rollback_preparations == 1


def test_prepare_rollback_returns_explicit_denial_without_a_mutation(
    tmp_path: Path,
) -> None:
    """A recorded denial must be distinguishable from a missing review."""
    gateway, run, backend = _gateway(tmp_path)
    action = _call("prepare_rollback", {"deployment_id": "deploy-2026-09-04-001"})
    gateway.store.record_approval(
        run.id,
        actor="reviewer",
        allow=False,
        action_step=run.current_step,
        action_fingerprint=action_fingerprint(action),
        reason="scope",
    )
    result = gateway.call_sync(run, action)

    assert result.error_code == "approval_denied"
    assert backend.rollback_preparations == 0
