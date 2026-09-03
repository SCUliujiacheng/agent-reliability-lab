"""Schema-validated actions emitted by agent policies."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class CallToolAction(BaseModel):
    """Request execution of one registered tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["call_tool"] = "call_tool"
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class FinishAction(BaseModel):
    """Finish a run with an auditable conclusion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["finish"] = "finish"
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    outcome: str = Field(min_length=1, max_length=128)


class FailAction(BaseModel):
    """Terminate a run with a stable failure code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["fail"] = "fail"
    code: str = Field(min_length=1, max_length=128)
    explanation: str = Field(min_length=1, max_length=4_000)


type AgentAction = Annotated[
    CallToolAction | FinishAction | FailAction, Field(discriminator="type")
]
