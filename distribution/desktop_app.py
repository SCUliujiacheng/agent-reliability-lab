"""Run the complete desktop engine with explicit immutable/writable paths."""

import json
import urllib.request
from pathlib import Path

from starlette.staticfiles import StaticFiles

from agent_reliability_lab.api.app import create_app as create_api
from agent_reliability_lab.config import Settings


def create_app(data_dir: str, resource_dir: str):
    data = Path(data_dir).resolve()
    resources = Path(resource_dir).resolve()
    scenarios = resources / "data" / "scenarios"
    metadata = json.loads((resources / "build.json").read_text(encoding="utf-8"))
    app = create_api(
        Settings(
            data_dir=data,
            database_path=data / "agent-reliability-lab.db",
            scenario_dir=scenarios,
            evaluation_suites=(("incident-response", scenarios),),
            trusted_hosts=("127.0.0.1", "localhost"),
            frozen_git_provenance=(metadata["git_revision"], metadata["git_dirty"]),
        )
    )
    app.mount(
        "/", StaticFiles(directory=resources / "web", html=True), name="dashboard"
    )
    return app


def smoke_test(origin: str, cookie: str) -> None:
    """Exercise real work through the frozen executable's authenticated HTTP API."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        headers = {"Cookie": cookie}
        if body is not None:
            headers["Content-Type"] = "application/json"
        call = urllib.request.Request(
            origin + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        with opener.open(call, timeout=60) as response:
            assert response.status in {200, 201}
            return json.loads(response.read())

    scenarios = request("/v1/scenarios")["items"]
    assert {item["id"] for item in scenarios} == {
        "normal-success",
        "timeout-recovery",
        "rate-limit-recovery",
        "permanent-invalid-input",
        "malformed-output-rejected",
        "approval-reconstruction",
    }
    recovered = request(
        "/v1/runs", {"scenario_id": "timeout-recovery", "mode": "resilient"}
    )
    assert recovered["status"] == "succeeded" and recovered["attempt_count"] == 4
    events = request(f"/v1/runs/{recovered['id']}/trace")["events"]
    assert sum(event["event_type"] == "tool.attempt.failed" for event in events) == 1
    waiting = request(
        "/v1/runs", {"scenario_id": "approval-reconstruction", "mode": "resilient"}
    )
    assert waiting["status"] == "waiting_approval"
    pending = waiting["pending_approval"]
    decision = {
        "actor": "packaged-smoke",
        "allow": True,
        "action_step": pending["action_step"],
        "action_fingerprint": pending["action_fingerprint"],
        "reason": "Verify the packaged synthetic workflow",
    }
    for _ in range(2):
        completed = request(f"/v1/runs/{waiting['id']}/approvals", decision)
        assert completed["status"] == "succeeded"
    events = request(f"/v1/runs/{waiting['id']}/trace")["events"]
    assert sum(event["event_type"] == "approval.recorded" for event in events) == 1
    assert (
        sum(
            event["event_type"] == "tool.attempt.succeeded"
            and event["payload"].get("tool_name") == "prepare_rollback"
            for event in events
        )
        == 1
    )
    report = request("/v1/evaluations", {"suite": "incident-response"})
    assert len(report["provenance"]["git_revision"]) == 40
    assert report["provenance"]["package_version"] == "0.2.0"
    for mode, correct, recovered_faults in (("fragile", 4, 0), ("resilient", 6, 2)):
        metrics = report["modes"][mode]["metrics"]
        assert metrics["case_count"] == 6 and metrics["case_correct_count"] == correct
        assert metrics["recovered_transient_fault_count"] == recovered_faults
        assert metrics["invalid_output_accepted_count"] == 0
