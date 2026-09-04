"""Approval, denial, idempotence, and race contracts."""

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app

from .conftest import assert_error, create_run


def _decision(client, waiting: dict, *, actor: str, allow: bool):
    approval = waiting["pending_approval"]
    return client.post(
        f"/v1/runs/{waiting['id']}/approvals",
        json={
            "actor": actor,
            "allow": allow,
            "reason": "reviewed",
            "action_step": approval["action_step"],
            "action_fingerprint": approval["action_fingerprint"],
        },
    )


def _prepare_successes(client, run_id: str) -> list[dict]:
    trace = client.get(f"/v1/runs/{run_id}/trace?limit=100").json()["events"]
    return [
        event
        for event in trace
        if event["event_type"] == "tool.attempt.succeeded"
        and event["payload"].get("tool_name") == "prepare_rollback"
    ]


def test_allow_resumes_and_identical_retry_is_idempotent(client) -> None:
    waiting = create_run(client, "approval-reconstruction")
    assert waiting["status"] == "waiting_approval"

    first = _decision(client, waiting, actor="reviewer", allow=True)
    repeated = _decision(client, waiting, actor="reviewer", allow=True)

    assert first.status_code == repeated.status_code == 200
    assert first.json()["status"] == repeated.json()["status"] == "succeeded"
    assert len(_prepare_successes(client, waiting["id"])) == 1


def test_deny_is_terminal_and_never_executes_write(client) -> None:
    waiting = create_run(client, "approval-reconstruction")
    denied = _decision(client, waiting, actor="reviewer", allow=False)

    assert denied.status_code == 200
    assert denied.json()["status"] == "failed"
    assert denied.json()["result"]["code"] == "approval_denied"
    assert _prepare_successes(client, waiting["id"]) == []


def test_conflicting_actor_or_decision_returns_stable_409(client) -> None:
    waiting = create_run(client, "approval-reconstruction")
    assert (
        _decision(client, waiting, actor="reviewer-a", allow=False).status_code == 200
    )

    assert_error(
        _decision(client, waiting, actor="reviewer-b", allow=False),
        409,
        "approval_conflict",
    )
    assert_error(
        _decision(client, waiting, actor="reviewer-a", allow=True),
        409,
        "approval_conflict",
    )


def test_approval_body_uses_strict_bool_and_forbids_internal_fields(client) -> None:
    waiting = create_run(client, "approval-reconstruction")
    assert_error(
        client.post(
            f"/v1/runs/{waiting['id']}/approvals",
            json={"actor": "reviewer", "allow": 1},
        ),
        422,
        "validation_error",
    )
    assert_error(
        client.post(
            f"/v1/runs/{waiting['id']}/approvals",
            json={
                "actor": "reviewer",
                "allow": True,
                "action_step": waiting["pending_approval"]["action_step"],
                "action_fingerprint": waiting["pending_approval"]["action_fingerprint"],
                "idempotency_key": "internal-field",
            },
        ),
        422,
        "validation_error",
    )


def test_two_simultaneous_allows_converge_and_write_once(client) -> None:
    waiting = create_run(client, "approval-reconstruction")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: _decision(client, waiting, actor="same-reviewer", allow=True),
                range(2),
            )
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert {response.json()["status"] for response in responses} == {"succeeded"}
    assert len(_prepare_successes(client, waiting["id"])) == 1


def test_allow_deny_race_has_one_winner_and_consistent_trace(client) -> None:
    waiting = create_run(client, "approval-reconstruction")

    with ThreadPoolExecutor(max_workers=2) as pool:
        allow_future = pool.submit(
            _decision, client, waiting, actor="allow-reviewer", allow=True
        )
        deny_future = pool.submit(
            _decision, client, waiting, actor="deny-reviewer", allow=False
        )
        responses = [allow_future.result(), deny_future.result()]

    assert sorted(response.status_code for response in responses) == [200, 409]
    final = client.get(f"/v1/runs/{waiting['id']}").json()
    trace = client.get(f"/v1/runs/{waiting['id']}/trace?limit=100").json()["events"]
    recorded = [event for event in trace if event["event_type"] == "approval.recorded"]
    denied = [event for event in trace if event["event_type"] == "approval.denied"]
    writes = _prepare_successes(client, waiting["id"])
    assert len(recorded) == 1
    if final["status"] == "succeeded":
        assert len(writes) == 1
        assert denied == []
    else:
        assert final["result"]["code"] == "approval_denied"
        assert writes == []
        assert len(denied) == 1


def test_approval_survives_app_reconstruction_exactly_once(settings) -> None:
    first_app = create_app(settings)
    with TestClient(first_app, base_url="http://localhost") as first:
        waiting = create_run(first, "approval-reconstruction")

    second_app = create_app(settings)
    with TestClient(second_app, base_url="http://localhost") as second:
        completed = _decision(second, waiting, actor="reviewer", allow=True)
        repeated = _decision(second, waiting, actor="reviewer", allow=True)
        assert completed.status_code == repeated.status_code == 200
        assert completed.json() == repeated.json()
        assert len(_prepare_successes(second, waiting["id"])) == 1


def test_two_apps_concurrent_identical_allow_converges_with_one_audit(settings) -> None:
    seed_app = create_app(settings)
    with TestClient(seed_app, base_url="http://localhost") as seed:
        waiting = create_run(seed, "approval-reconstruction")

    first_app = create_app(settings)
    second_app = create_app(settings)
    with (
        TestClient(first_app, base_url="http://localhost") as first,
        TestClient(second_app, base_url="http://localhost") as second,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                pool.submit(
                    _decision,
                    client,
                    waiting,
                    actor="shared-reviewer",
                    allow=True,
                )
                for client in (first, second)
            ]
            completed = [future.result() for future in responses]

        assert [response.status_code for response in completed] == [200, 200], [
            response.text for response in completed
        ]
        assert {response.json()["status"] for response in completed} == {"succeeded"}
        trace = first.get(f"/v1/runs/{waiting['id']}/trace?limit=100").json()["events"]
        assert sum(event["event_type"] == "approval.recorded" for event in trace) == 1
        assert len(_prepare_successes(first, waiting["id"])) == 1


def test_two_apps_allow_deny_race_has_one_durable_winner(settings) -> None:
    seed_app = create_app(settings)
    with TestClient(seed_app, base_url="http://localhost") as seed:
        waiting = create_run(seed, "approval-reconstruction")

    first_app = create_app(settings)
    second_app = create_app(settings)
    with (
        TestClient(first_app, base_url="http://localhost") as first,
        TestClient(second_app, base_url="http://localhost") as second,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            allow = pool.submit(
                _decision,
                first,
                waiting,
                actor="allow-reviewer",
                allow=True,
            )
            deny = pool.submit(
                _decision,
                second,
                waiting,
                actor="deny-reviewer",
                allow=False,
            )
            completed = [allow.result(), deny.result()]

        assert sorted(response.status_code for response in completed) == [200, 409]
        trace = first.get(f"/v1/runs/{waiting['id']}/trace?limit=100").json()["events"]
        assert sum(event["event_type"] == "approval.recorded" for event in trace) == 1
        final = first.get(f"/v1/runs/{waiting['id']}").json()
        writes = _prepare_successes(first, waiting["id"])
        if final["status"] == "succeeded":
            assert len(writes) == 1
            assert not any(event["event_type"] == "approval.denied" for event in trace)
        else:
            assert final["result"]["code"] == "approval_denied"
            assert writes == []
            assert sum(event["event_type"] == "approval.denied" for event in trace) == 1
