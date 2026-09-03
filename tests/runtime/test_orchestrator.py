"""State-machine and checkpoint tests for the durable orchestrator."""

import pytest

from agent_reliability_lab.domain.runs import RunStatus
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.runtime.policies import ScriptedPolicy


@pytest.mark.asyncio
async def test_scripted_run_checkpoints_cursor_before_terminal_result(
    app_context: object,
) -> None:
    """Forgetting successful action checkpoints would replay tools after restart."""
    context = app_context
    run = await context.runs.start("rollback-approval", "resilient")

    assert run.status is RunStatus.WAITING_APPROVAL
    assert run.current_step == 1
    assert run.context["tool_results"] == {
        "0": {
            "deployment_id": "deploy-2026-09-04-001",
            "version": "2026.09.04.1",
        }
    }
    assert isinstance(context.runs.policy, ScriptedPolicy)


@pytest.mark.asyncio
async def test_permanent_tool_failure_becomes_a_terminal_run(
    app_context: object,
) -> None:
    """Leaving a permanent gateway failure running would permit unsafe resume."""
    scenario = Scenario.model_validate(
        {
            **app_context.runs.load_scenario("rollback-approval").model_dump(),
            "id": "bad-tool",
            "actions": [{"type": "call_tool", "tool_name": "missing", "arguments": {}}],
        }
    )
    app_context.scenarios[scenario.id] = scenario

    run = await app_context.runs.start("bad-tool", "resilient")

    assert run.status is RunStatus.FAILED
    assert run.result == {"code": "unknown_tool"}


@pytest.mark.asyncio
async def test_invalid_high_risk_input_fails_before_approval_pause(
    app_context: object,
) -> None:
    """Invalid input must not solicit approval for an action that cannot execute."""
    scenario = Scenario.model_validate(
        {
            **app_context.runs.load_scenario("rollback-approval").model_dump(),
            "id": "invalid-rollback",
            "actions": [
                {
                    "type": "call_tool",
                    "tool_name": "prepare_rollback",
                    "arguments": {"deployment_id": ""},
                    "idempotency_key": "invalid-rollback-v1",
                }
            ],
        }
    )
    app_context.scenarios[scenario.id] = scenario

    run = await app_context.runs.start("invalid-rollback", "resilient")

    assert run.status is RunStatus.FAILED
    assert run.result == {"code": "invalid_input"}
