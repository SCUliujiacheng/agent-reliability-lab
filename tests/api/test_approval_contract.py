"""Public approval-review identity and projection contracts."""

from uuid import UUID

from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.tools.gateway import action_fingerprint

from .conftest import assert_error, assert_safe_http_json, create_run


def _target(waiting: dict[str, object]) -> dict[str, object]:
    approval = waiting["pending_approval"]
    assert isinstance(approval, dict)
    return {
        "action_step": approval["action_step"],
        "action_fingerprint": approval["action_fingerprint"],
    }


def test_waiting_run_projects_only_the_exact_reviewable_action(client) -> None:
    """Leaking idempotency/internal fields or hiding the reviewed action fails this."""
    waiting = create_run(client, "approval-reconstruction")

    approval = waiting["pending_approval"]
    assert set(approval) == {
        "action_step",
        "action_fingerprint",
        "tool_name",
        "arguments",
    }
    assert approval["action_step"] == 1
    assert approval["tool_name"] == "prepare_rollback"
    assert approval["arguments"] == {"deployment_id": "deploy-2026-09-04-001"}
    assert len(approval["action_fingerprint"]) == 64
    assert set(approval["action_fingerprint"]) <= set("0123456789abcdef")
    assert_safe_http_json(waiting)
    assert "idempotency_key" not in str(waiting)


def test_pending_approval_arguments_are_recursively_sanitized(client) -> None:
    """A nested credential or configured secret crossing the DTO fails this test."""
    action = CallToolAction(
        tool_name="prepare_rollback",
        arguments={
            "deployment_id": "api-test-secret-deployment",
            "nested": {
                "access_token": "credential-value",
                "notes": ["before api-test-secret after"],
            },
        },
        idempotency_key="must-never-be-public",
    )
    run = Run.new("sanitized-approval", "resilient")
    run = run.transition(RunStatus.RUNNING).transition(RunStatus.WAITING_APPROVAL)
    run = run.model_copy(
        update={
            "pending_approval": True,
            "pending_action": action,
            "pending_action_fingerprint": action_fingerprint(action),
        }
    )
    client.app.state.container.store.save_run(run)

    response = client.get(f"/v1/runs/{run.id}")

    assert response.status_code == 200
    public = response.json()["pending_approval"]
    assert public["arguments"] == {
        "deployment_id": "[REDACTED]-deployment",
        "nested": {
            "access_token": "[REDACTED]",
            "notes": ["before [REDACTED] after"],
        },
    }
    assert "credential-value" not in response.text
    assert "must-never-be-public" not in response.text


def test_approval_requires_and_atomically_matches_the_reviewed_target(client) -> None:
    """Accepting omitted or fabricated action identity must fail this test."""
    waiting = create_run(client, "approval-reconstruction")
    path = f"/v1/runs/{waiting['id']}/approvals"

    assert_error(
        client.post(path, json={"actor": "reviewer", "allow": True}),
        422,
        "validation_error",
    )
    fabricated = {
        "actor": "reviewer",
        "allow": True,
        "reason": "reviewed",
        "action_step": waiting["pending_approval"]["action_step"],
        "action_fingerprint": "0" * 64,
    }
    assert_error(client.post(path, json=fabricated), 409, "approval_conflict")
    assert (
        client.app.state.container.store.get_approval(
            UUID(waiting["id"]), action_step=fabricated["action_step"]
        )
        is None
    )

    approved = client.post(
        path,
        json={
            "actor": "reviewer",
            "allow": True,
            "reason": "reviewed",
            **_target(waiting),
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "succeeded"
    assert "pending_approval" not in approved.json()
