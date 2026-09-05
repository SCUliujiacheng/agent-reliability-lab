from pathlib import Path

import pytest

from agent_reliability_lab.scenarios.loader import (
    ScenarioValidationError,
    load_scenario,
    scenario_sha256,
)


def write_scenario(tmp_path: Path, *, fault_type: str = "timeout") -> Path:
    path = tmp_path / "incident.yaml"
    content = (
        """id: api-latency
version: 1
initial_context:
  service: payments
actions:
  - type: call_tool
    tool_name: get_service_health
    arguments:
      service: payments
  - type: finish
    summary: Service is healthy.
    evidence_refs:
      - health-check
    outcome: succeeded
expected_tool_sequence:
  - get_service_health
expected_outcome: succeeded
faults:
  - tool_name: get_service_health
    attempt: 1
    type: """
        + fault_type
        + """
approval_supplied: false
"""
    )
    path.write_bytes(content.encode("utf-8"))
    return path


def test_loader_rejects_unknown_fault_type(tmp_path: Path) -> None:
    """Accepting an unrecognised injected fault must make this test fail."""
    path = write_scenario(tmp_path, fault_type="surprise")

    with pytest.raises(ScenarioValidationError):
        load_scenario(path)


def test_loader_builds_actions_and_hashes_exact_file_bytes(tmp_path: Path) -> None:
    """Ignoring YAML actions or normalising bytes before hashing must fail."""
    path = write_scenario(tmp_path)

    scenario = load_scenario(path)

    assert scenario.id == "api-latency"
    assert scenario.actions[0].type == "call_tool"
    assert scenario.faults[0].type == "timeout"
    assert (
        scenario_sha256(path)
        == "1fb6ba86037c98588cbf4684c838cfab6c05e75131f8d401a415c43b29fbac59"
    )
