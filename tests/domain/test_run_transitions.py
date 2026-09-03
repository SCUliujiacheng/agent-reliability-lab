from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import InvalidTransition, Run, RunStatus


def test_terminal_run_rejects_a_new_transition() -> None:
    """Removing terminal-state protection must make this test fail."""
    run = Run.new("api-latency", mode="resilient").transition(RunStatus.RUNNING)
    run = run.transition(RunStatus.SUCCEEDED)

    with pytest.raises(InvalidTransition):
        run.transition(RunStatus.RUNNING)


def test_transition_returns_an_immutable_run_with_the_requested_status() -> None:
    """Returning a mutable or unchanged run must make this test fail."""
    queued = Run.new("api-latency", mode="fragile")

    running = queued.transition(RunStatus.RUNNING)

    assert queued.status is RunStatus.QUEUED
    assert running.status is RunStatus.RUNNING
    assert running.id == queued.id


def test_transition_normalizes_a_runtime_string_status_to_the_enum() -> None:
    """Returning a raw string status must make this test fail."""
    queued = Run.new("api-latency", mode="resilient")

    running = queued.transition("running")  # type: ignore[arg-type]

    assert type(running.status) is RunStatus
    assert running.status is RunStatus.RUNNING


def test_settings_rejects_a_database_outside_its_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allowing a database path to escape the data directory must fail."""
    monkeypatch.setenv("ARL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARL_DATABASE_PATH", str(tmp_path / "outside.db"))

    with pytest.raises(ValueError, match="data directory"):
        Settings.from_env(tmp_path)
