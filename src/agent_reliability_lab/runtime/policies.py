"""Policies that choose the next schema-validated agent action."""

from typing import Protocol

from agent_reliability_lab.domain.actions import AgentAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.domain.scenarios import Scenario


class PolicyExhaustedError(RuntimeError):
    """Raised when a policy has no action at the durable cursor."""


class Policy(Protocol):
    """One action decision boundary used by the durable orchestrator."""

    async def next_action(self, run: Run, scenario: Scenario) -> AgentAction:
        """Return exactly one action for the run's persisted cursor."""
        ...


class ScriptedPolicy:
    """Replay a frozen scenario using the persisted run step as its cursor."""

    name = "scripted"

    async def next_action(self, run: Run, scenario: Scenario) -> AgentAction:
        try:
            return scenario.actions[run.current_step]
        except IndexError as error:
            raise PolicyExhaustedError(
                f"scenario {scenario.id} has no action at step {run.current_step}"
            ) from error


__all__ = ["Policy", "PolicyExhaustedError", "ScriptedPolicy"]
