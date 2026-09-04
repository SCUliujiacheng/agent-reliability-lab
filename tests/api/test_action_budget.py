"""HTTP adapter wiring for the server-configured action budget."""

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app

from .conftest import make_settings


def test_api_runtime_uses_the_server_configured_action_budget(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), max_action_steps=1)
    app = create_app(settings)

    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/v1/runs",
            json={"scenario_id": "normal-success", "mode": "resilient"},
        )
        assert response.status_code == 201
        run = response.json()
        trace = client.get(f"/v1/runs/{run['id']}/trace", params={"limit": 100})

    assert run["status"] == "failed"
    assert run["result"]["code"] == "action_budget_exhausted"
    events = trace.json()["events"]
    assert sum(event["event_type"] == "policy.action" for event in events) == 1
    assert sum(event["event_type"] == "run.failed" for event in events) == 1
