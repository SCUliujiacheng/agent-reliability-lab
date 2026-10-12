"""Release adapters must execute the frozen suite and preserve durable evidence."""

import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app as create_desktop_api
from agent_reliability_lab.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "scenarios" / "incident-response"


def load_adapter(name):
    path = ROOT / "distribution" / f"{name}.py"
    assert path.is_file(), f"The release must provide the {name} adapter"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def resources(tmp_path):
    root = tmp_path / "Read only installation 空间"
    load_adapter("prepare_android").prepare_resources(ROOT, root)
    return root


@pytest.fixture
def mobile(tmp_path, resources):
    return load_adapter("mobile_app").create_app(
        str(tmp_path / "User data 空间"), str(resources)
    )


def request(app, method, path, body=None, query=None):
    status, headers, raw = app.handle(method, path, query or {}, body, {})
    assert headers["Content-Type"].startswith("application/json")
    return status, json.loads(raw)


def start(app, scenario="normal-success", mode="resilient"):
    status, run = request(
        app, "POST", "/v1/runs", {"scenario_id": scenario, "mode": mode}
    )
    assert status == 201, run
    return run


def decision(run, allow=True):
    pending = run["pending_approval"]
    return {
        "actor": "release-test",
        "allow": allow,
        "action_step": pending["action_step"],
        "action_fingerprint": pending["action_fingerprint"],
        "reason": "Synthetic incident approval",
    }


def trace(app, run_id):
    status, page = request(app, "GET", f"/v1/runs/{run_id}/trace")
    assert status == 200
    return page["events"]


@pytest.mark.parametrize(
    "scenario", [path.stem for path in sorted(SCENARIOS.glob("*.yaml"))]
)
@pytest.mark.parametrize("mode", ["fragile", "resilient"])
def test_mobile_matches_real_desktop_outcome_attempts_and_order(
    mobile, tmp_path, scenario, mode
):
    """A shortcut returning the expected outcome cannot pass retry/trace parity."""
    settings = Settings(
        data_dir=tmp_path / "desktop",
        database_path=tmp_path / "desktop" / "runs.db",
        scenario_dir=SCENARIOS,
        evaluation_suites=(("incident-response", SCENARIOS),),
    )
    run = start(mobile, scenario, mode)
    with TestClient(create_desktop_api(settings), base_url="http://localhost") as api:
        desktop = api.post(
            "/v1/runs", json={"scenario_id": scenario, "mode": mode}
        ).json()
        if run["status"] == "waiting_approval":
            status, run = request(
                mobile, "POST", f"/v1/runs/{run['id']}/approvals", decision(run)
            )
            assert status == 200
            desktop = api.post(
                f"/v1/runs/{desktop['id']}/approvals", json=decision(desktop)
            ).json()
        desktop_events = api.get(f"/v1/runs/{desktop['id']}/trace").json()["events"]
    assert run["status"] == desktop["status"]
    assert run["result"].get("outcome", run["result"].get("code")) == desktop[
        "result"
    ].get("outcome", desktop["result"].get("code"))
    events = trace(mobile, run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    for event_type in (
        "tool.attempt.started",
        "tool.attempt.failed",
        "tool.attempt.succeeded",
    ):
        projection = lambda items, kind=event_type: [
            (event["payload"]["tool_name"], event["payload"]["attempt"])
            for event in items
            if event["event_type"] == kind
        ]
        assert projection(events) == projection(desktop_events)
    assert run["attempt_count"] == desktop["attempt_count"]

    def logical_order(items):
        # Desktop records the resumed policy action again at the same step.
        by_step = {
            event["payload"]["action_step"]: event["payload"]["tool_name"]
            for event in items
            if event["event_type"] == "policy.action"
            and "tool_name" in event["payload"]
        }
        return [by_step[step] for step in sorted(by_step)]

    assert logical_order(events) == logical_order(desktop_events)


def test_approval_survives_restart_and_duplicate_decision_has_one_effect(
    mobile, resources, tmp_path
):
    run = start(mobile, "approval-reconstruction")
    assert run["status"] == "waiting_approval"
    payload = decision(run)
    app = load_adapter("mobile_app").create_app(
        str(tmp_path / "User data 空间"), str(resources)
    )
    route = f"/v1/runs/{run['id']}/approvals"
    for _ in range(2):
        status, completed = request(app, "POST", route, payload)
        assert status == 200
        assert completed["status"] == "succeeded"
    events = trace(app, run["id"])
    assert sum(event["event_type"] == "approval.recorded" for event in events) == 1
    assert (
        sum(
            event["event_type"] == "tool.attempt.succeeded"
            and event["payload"].get("tool_name") == "prepare_rollback"
            for event in events
        )
        == 1
    )
    with sqlite3.connect(tmp_path / "User data 空间" / "agentlab-mobile.db") as db:
        assert db.execute("SELECT COUNT(*) FROM effects").fetchone()[0] == 1
    status, _ = request(app, "POST", route, {**payload, "allow": False})
    assert status == 409


@pytest.mark.parametrize(
    "change", [{"action_step": 0}, {"action_fingerprint": "0" * 64}]
)
def test_stale_approval_cannot_execute_write(mobile, change):
    run = start(mobile, "approval-reconstruction")
    status, _ = request(
        mobile, "POST", f"/v1/runs/{run['id']}/approvals", {**decision(run), **change}
    )
    assert status == 409
    status, current = request(mobile, "GET", f"/v1/runs/{run['id']}")
    assert current["status"] == "waiting_approval"
    assert not any(
        event["payload"].get("tool_name") == "prepare_rollback"
        and event["event_type"] == "tool.attempt.started"
        for event in trace(mobile, run["id"])
    )


def test_denied_approval_is_terminal_and_idempotent(mobile):
    run = start(mobile, "approval-reconstruction")
    payload = decision(run, False)
    for _ in range(2):
        status, result = request(
            mobile, "POST", f"/v1/runs/{run['id']}/approvals", payload
        )
        assert status == 200
        assert result["status"] == "failed"
        assert result["result"]["code"] == "approval_denied"
    assert request(mobile, "POST", f"/v1/runs/{run['id']}/resume")[0] == 409


def test_two_app_instances_converge_on_one_approval_and_write(
    mobile, resources, tmp_path
):
    run = start(mobile, "approval-reconstruction")
    other = load_adapter("mobile_app").create_app(
        str(tmp_path / "User data 空间"), str(resources)
    )
    payload = decision(run)
    route = f"/v1/runs/{run['id']}/approvals"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda app: request(app, "POST", route, payload), [mobile, other])
        )
    assert all(
        status == 200 and value["status"] == "succeeded" for status, value in results
    )
    events = trace(other, run["id"])
    assert sum(event["event_type"] == "approval.recorded" for event in events) == 1
    assert (
        sum(
            event["event_type"] == "tool.attempt.succeeded"
            and event["payload"]["tool_name"] == "prepare_rollback"
            for event in events
        )
        == 1
    )


def test_report_metrics_come_from_new_runs_and_survive_restart(
    mobile, resources, tmp_path
):
    status, report = request(
        mobile, "POST", "/v1/evaluations", {"suite": "incident-response"}
    )
    assert status == 201, report
    assert report["provenance"]["report_version"] == "mobile/v1"
    assert report["provenance"]["grader_version"] == "mobile-exact/v1"
    for mode, correct, attempts, accepted in [
        ("fragile", 4, 0, 8),
        ("resilient", 6, 2, 11),
    ]:
        metrics = report["modes"][mode]["metrics"]
        assert metrics["case_correct_count"] == correct
        assert metrics["retry_attempt_count"] == attempts
        assert metrics["accepted_output_count"] == accepted
        assert metrics["invalid_output_accepted_count"] == 0
        assert metrics["invalid_output_rejected_count"] == 1
        for case in report["modes"][mode]["cases"]:
            events = trace(mobile, case["run_id"])
            assert case["attempt_count"] == sum(
                event["event_type"] == "tool.attempt.started" for event in events
            )
    assert report["modes"]["fragile"]["metrics"][
        "tool_sequence_accuracy"
    ] == pytest.approx(17 / 18)
    app = load_adapter("mobile_app").create_app(
        str(tmp_path / "User data 空间"), str(resources)
    )
    assert (
        request(app, "GET", f"/v1/evaluations/{report['evaluation_id']}")[1] == report
    )


def test_corrupt_trace_fails_report_closed(mobile, tmp_path):
    _, report = request(
        mobile, "POST", "/v1/evaluations", {"suite": "incident-response"}
    )
    run_id = report["modes"]["resilient"]["cases"][0]["run_id"]
    with sqlite3.connect(tmp_path / "User data 空间" / "agentlab-mobile.db") as db:
        db.execute("DELETE FROM events WHERE run_id = ? AND sequence = 2", (run_id,))
    status, result = request(
        mobile, "GET", f"/v1/evaluations/{report['evaluation_id']}"
    )
    assert status == 409
    assert result["error"]["code"] == "evidence_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario_id": "normal-success", "mode": "resilient", "extra": True},
        {"scenario_id": "../../private", "mode": "resilient"},
        {"scenario_id": "normal-success", "mode": "anything"},
    ],
)
def test_invalid_public_inputs_fail_without_creating_runs(mobile, payload):
    assert request(mobile, "POST", "/v1/runs", payload)[0] == 422
    assert request(mobile, "GET", "/v1/runs")[1] == {"items": []}
    assert request(mobile, "GET", "/v1/unknown")[0] == 404


def test_trace_paging_does_not_drop_events(mobile):
    run = start(mobile, "timeout-recovery")
    accumulated = []
    cursor = 0
    while True:
        status, page = request(
            mobile,
            "GET",
            f"/v1/runs/{run['id']}/trace",
            query={"after_sequence": [str(cursor)], "limit": ["3"]},
        )
        assert status == 200
        accumulated += page["events"]
        cursor = page["next_after_sequence"]
        if not page["has_more"]:
            break
    assert accumulated == trace(mobile, run["id"])


def test_mobile_export_is_a_complete_same_origin_json_download(mobile):
    run = start(mobile, "timeout-recovery")
    status, headers, raw = mobile.handle(
        "GET", "/v1/export", {"run_id": [run["id"]]}, None, {}
    )
    assert status == 200
    assert (
        headers["Content-Disposition"]
        == f'attachment; filename="trace-{run["id"]}.json"'
    )
    downloaded = json.loads(raw)
    assert downloaded["run"] == run
    assert downloaded["events"] == trace(mobile, run["id"])
    assert (
        request(mobile, "GET", "/v1/export", query={"run_id": ["../../other"]})[0]
        == 422
    )


def test_android_resource_preparation_marks_mobile_without_changing_desktop(tmp_path):
    preparation = load_adapter("prepare_android")
    preparation.prepare_mobile_resources(ROOT, tmp_path / "mobile")
    preparation.prepare_resources(ROOT, tmp_path / "desktop")
    assert 'name="arl-runtime" content="mobile"' in (
        tmp_path / "mobile/web/index.html"
    ).read_text(encoding="utf-8")
    assert 'name="arl-runtime"' not in (tmp_path / "desktop/web/index.html").read_text(
        encoding="utf-8"
    )


def test_interrupted_tool_attempt_resumes_last_committed_checkpoint(
    mobile, resources, tmp_path, monkeypatch
):
    original = mobile._attempt

    def crash_at_second_action(db, run, scenario, action):
        if run["current_step"] == 1:
            raise SystemExit("process interrupted after first durable checkpoint")
        return original(db, run, scenario, action)

    monkeypatch.setattr(mobile, "_attempt", crash_at_second_action)
    with pytest.raises(SystemExit):
        start(mobile, "normal-success")
    app = load_adapter("mobile_app").create_app(
        str(tmp_path / "User data 空间"), str(resources)
    )
    _, runs = request(app, "GET", "/v1/runs")
    run = runs["items"][0]
    assert run["status"] == "running"
    assert run["attempt_count"] == 1
    status, result = request(app, "POST", f"/v1/runs/{run['id']}/resume")
    assert status == 200
    assert result["status"] == "succeeded"
    assert result["attempt_count"] == 3
    assert (
        sum(
            event["event_type"] == "tool.attempt.started"
            and event["payload"]["tool_name"] == "get_service_health"
            for event in trace(app, run["id"])
        )
        == 1
    )


def test_report_preserves_build_dirty_provenance(resources, tmp_path):
    metadata = json.loads((resources / "build.json").read_text(encoding="utf-8"))
    metadata["git_dirty"] = True
    (resources / "build.json").write_text(json.dumps(metadata), encoding="utf-8")
    app = load_adapter("mobile_app").create_app(
        str(tmp_path / "dirty-build"), str(resources)
    )
    _, report = request(app, "POST", "/v1/evaluations", {"suite": "incident-response"})
    assert report["provenance"]["git_dirty"] is True


def test_mobile_has_no_third_party_runtime_dependencies(resources, tmp_path):
    code = (
        "import importlib.util,json; "
        f"s=importlib.util.spec_from_file_location('mobile_app',{str(ROOT / 'distribution/mobile_app.py')!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"a=m.create_app({str(tmp_path / 'isolated')!r},{str(resources)!r}); "
        "r=a.handle('POST','/v1/runs',{}, {'scenario_id':'timeout-recovery','mode':'resilient'},{}); "
        "assert r[0]==201 and json.loads(r[2])['status']=='succeeded'"
    )
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)


def test_desktop_uses_bundled_scenarios_and_writable_user_data(
    resources, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "desktop-user"
    app = load_adapter("desktop_app").create_app(str(data), str(resources))
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200
        assert len(client.get("/v1/scenarios").json()["items"]) == 6
        assert client.get("/").status_code == 200
        assert client.get("/v1/not-found").status_code == 404
        assert (
            client.post(
                "/v1/runs", json={"scenario_id": "normal-success", "mode": "resilient"}
            ).json()["status"]
            == "succeeded"
        )
    assert (data / "agent-reliability-lab.db").is_file()
    assert not (resources / "agent-reliability-lab.db").exists()


def test_frozen_smoke_hook_executes_scenarios_approval_and_evaluation(
    resources, tmp_path
):
    adapter = load_adapter("desktop_app")
    hook = getattr(adapter, "smoke_test", None)
    assert callable(hook), "The frozen smoke must exercise the packaged engine"
    data = tmp_path / "smoke-data"
    server = uvicorn.Server(
        uvicorn.Config(
            adapter.create_app(str(data), str(resources)),
            access_log=False,
            log_config=None,
            ws="none",
        )
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    worker = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        hook(f"http://127.0.0.1:{listener.getsockname()[1]}", "smoke_token=local-test")
        with sqlite3.connect(data / "agent-reliability-lab.db") as db:
            assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
            assert (
                db.execute("SELECT COUNT(*) FROM evaluation_reports").fetchone()[0] == 1
            )
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        listener.close()


def test_resource_bundle_contains_only_curated_data_and_source_hashes(tmp_path):
    resources = tmp_path / "curated"
    load_adapter("prepare_android").prepare_resources(ROOT, resources)
    catalog = json.loads(
        (resources / "data/scenarios.json").read_text(encoding="utf-8")
    )
    assert len(catalog["scenarios"]) == 6
    assert all(len(item["source_sha256"]) == 64 for item in catalog["scenarios"])
    assert not list(resources.rglob("*.db"))
    assert (resources / "web/index.html").is_file()
    assert (
        json.loads((resources / "app.json").read_text(encoding="utf-8"))["version"]
        == "0.2.0"
    )
