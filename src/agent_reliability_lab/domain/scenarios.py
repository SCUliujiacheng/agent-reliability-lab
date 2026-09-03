"""Immutable, bounded scenario contracts for deterministic benchmarks."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent_reliability_lab.domain.actions import AgentAction


class FaultType(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TOOL_ERROR = "tool_error"
    MALFORMED_OUTPUT = "malformed_output"


class FaultRule(BaseModel):
    """A deterministic fault applied to a named tool attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1, le=20)
    type: FaultType


type ScenarioAction = AgentAction


class Scenario(BaseModel):
    """A versioned scripted workflow and its expected benchmark result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    initial_context: dict[str, object] = Field(default_factory=dict)
    actions: tuple[ScenarioAction, ...] = Field(min_length=1, max_length=100)
    expected_tool_sequence: tuple[str, ...] = Field(
        default_factory=tuple, max_length=100
    )
    expected_outcome: str = Field(min_length=1, max_length=128)
    faults: tuple[FaultRule, ...] = Field(default_factory=tuple, max_length=100)
    approval_supplied: bool = False
