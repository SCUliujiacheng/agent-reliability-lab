"""Shared durable runtime fixtures."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction, FinishAction
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.service import RunService
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


@dataclass
class AppContext:
    """A process-shaped runtime around one durable SQLite database."""

    database_path: Path
    runs: RunService
    backend: IncidentBackend
    scenarios: dict[str, Scenario]

    def reconstruct(self) -> "AppContext":
        return build_context(self.database_path)


def rollback_scenario() -> Scenario:
    return Scenario(
        id="rollback-approval",
        version=1,
        initial_context={"incident": "checkout"},
        actions=(
            CallToolAction(tool_name="get_deployment"),
            CallToolAction(
                tool_name="prepare_rollback",
                arguments={"deployment_id": "deploy-2026-09-04-001"},
                idempotency_key="rollback-approval-v1",
            ),
            FinishAction(summary="Rollback prepared", outcome="prepared"),
        ),
        expected_tool_sequence=("get_deployment", "prepare_rollback"),
        expected_outcome="prepared",
    )


def build_context(database_path: Path) -> AppContext:
    settings = Settings(data_dir=database_path.parent, database_path=database_path)
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    recorder = TraceRecorder(store)
    backend = IncidentBackend()
    gateway = ToolGateway(
        store,
        recorder,
        incident_registry(backend),
        incident_backend=backend,
    )
    scenarios = {"rollback-approval": rollback_scenario()}
    loader: Callable[[str], Scenario] = scenarios.__getitem__
    return AppContext(
        database_path=database_path,
        runs=RunService(store, recorder, gateway, loader),
        backend=backend,
        scenarios=scenarios,
    )


@pytest.fixture
def app_context(tmp_path: Path) -> AppContext:
    return build_context(tmp_path / "runs.db")
