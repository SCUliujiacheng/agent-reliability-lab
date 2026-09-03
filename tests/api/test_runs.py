"""Run, scenario, list aggregation, and trace pagination contracts."""

from uuid import UUID

from agent_reliability_lab.storage.models import TraceEvent

from .conftest import assert_error, assert_safe_http_json, create_run


def test_scenarios_are_catalogued_without_filesystem_or_action_leakage(client) -> None:
    response = client.get("/v1/scenarios")

    assert response.status_code == 200
    scenarios = response.json()["items"]
    assert {item["id"] for item in scenarios} >= {
        "normal-success",
        "approval-reconstruction",
        "timeout-recovery",
    }
    assert all("path" not in item and "actions" not in item for item in scenarios)
    assert_safe_http_json(response.json())


def test_launcher_create_detail_list_and_trace_use_safe_dtos(client) -> None:
    created = create_run(client, "timeout-recovery")
    assert created["status"] == "succeeded"
    assert created["attempt_count"] == 4
    assert created["result"]["outcome"] == "diagnosed"

    detail = client.get(f"/v1/runs/{created['id']}")
    listing = client.get("/v1/runs?limit=10")
    trace = client.get(f"/v1/runs/{created['id']}/trace?limit=100")

    assert detail.status_code == listing.status_code == trace.status_code == 200
    assert listing.json()["items"][0]["id"] == created["id"]
    assert listing.json()["items"][0]["attempt_count"] == 4
    sequences = [event["sequence"] for event in trace.json()["events"]]
    assert sequences == sorted(sequences)
    assert any(
        event["event_type"] == "fault.injected"
        and event["payload"]["kind"] == "timeout"
        for event in trace.json()["events"]
    )
    for payload in (created, detail.json(), listing.json(), trace.json()):
        assert_safe_http_json(payload)


def test_recent_runs_uses_batch_attempt_counts_not_per_run_trace_reads(
    client, app, monkeypatch
) -> None:
    create_run(client)
    create_run(client, "timeout-recovery")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("run listing must not issue per-run trace reads")

    monkeypatch.setattr(app.state.container.store, "list_events", forbidden)
    response = client.get("/v1/runs")

    assert response.status_code == 200
    assert [item["attempt_count"] for item in response.json()["items"]] == [4, 3]


def test_trace_uses_database_cursor_and_can_continue_after_append(client, app) -> None:
    run = create_run(client)
    first = client.get(f"/v1/runs/{run['id']}/trace?limit=2&after_sequence=0")

    assert first.status_code == 200
    assert len(first.json()["events"]) == 2
    assert first.json()["has_more"] is True
    cursor = first.json()["next_after_sequence"]
    second = client.get(f"/v1/runs/{run['id']}/trace?limit=100&after_sequence={cursor}")
    previous_last = second.json()["next_after_sequence"]

    stored = app.state.container.store.get_run(UUID(run["id"]))
    assert stored is not None
    app.state.container.store.append_event(
        TraceEvent.new(stored.trace_id, "test.appended", {"tool_name": "future_tool"})
    )
    appended = client.get(
        f"/v1/runs/{run['id']}/trace?limit=10&after_sequence={previous_last}"
    )

    assert [event["event_type"] for event in appended.json()["events"]] == [
        "test.appended"
    ]
    assert appended.json()["events"][0]["payload"]["tool_name"] == "future_tool"


def test_unknown_run_and_trace_are_404_not_empty(client) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert_error(client.get(f"/v1/runs/{missing}"), 404, "run_not_found")
    assert_error(client.get(f"/v1/runs/{missing}/trace"), 404, "run_not_found")


def test_terminal_run_cannot_resume(client) -> None:
    run = create_run(client)
    assert_error(client.post(f"/v1/runs/{run['id']}/resume"), 409, "invalid_transition")


def test_run_inputs_and_list_bounds_are_strict(client) -> None:
    assert_error(
        client.post(
            "/v1/runs",
            json={"scenario_id": "normal-success", "mode": "resilient", "extra": 1},
        ),
        422,
        "validation_error",
    )
    assert_error(
        client.post("/v1/runs", json={"scenario_id": "normal-success", "mode": True}),
        422,
        "validation_error",
    )
    assert_error(client.get("/v1/runs?limit=0"), 422, "validation_error")
    assert_error(client.get("/v1/runs?limit=101"), 422, "validation_error")
    assert_error(client.get("/v1/runs/not-a-uuid"), 422, "validation_error")
    assert_error(
        client.get(
            "/v1/runs/00000000-0000-0000-0000-000000000000/trace?after_sequence=-1"
        ),
        422,
        "validation_error",
    )
