"""Immutable run state and explicit state-machine transitions."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ALLOWED_TRANSITIONS = {
    RunStatus.QUEUED: {RunStatus.RUNNING},
    RunStatus.RUNNING: {
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
    },
    RunStatus.WAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.FAILED},
}


class InvalidTransition(ValueError):
    """Raised when a state-machine transition is not allowed."""


class Run(BaseModel):
    """A durable, immutable record of an agent workflow execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    scenario_id: str = Field(min_length=1, max_length=128)
    mode: Literal["fragile", "resilient"]
    status: RunStatus = RunStatus.QUEUED
    current_step: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    trace_id: UUID
    policy_name: str = Field(default="scripted", min_length=1, max_length=128)
    context: dict[str, object] = Field(default_factory=dict)
    pending_approval: bool = False
    result: dict[str, object] | None = None
    version: int = Field(default=0, ge=0)

    @classmethod
    def new(cls, scenario_id: str, mode: Literal["fragile", "resilient"]) -> "Run":
        """Create a queued run with stable identifiers and UTC timestamps."""
        now = datetime.now(UTC)
        return cls(
            id=uuid4(),
            scenario_id=scenario_id,
            mode=mode,
            created_at=now,
            updated_at=now,
            trace_id=uuid4(),
        )

    def transition(self, status: RunStatus) -> "Run":
        """Return a new run after validating one state-machine transition."""
        try:
            next_status = RunStatus(status)
        except (TypeError, ValueError) as error:
            raise InvalidTransition(f"unknown run status: {status}") from error
        if next_status not in ALLOWED_TRANSITIONS.get(self.status, set()):
            raise InvalidTransition(
                f"cannot transition from {self.status} to {next_status}"
            )
        return self.model_copy(
            update={"status": next_status, "updated_at": datetime.now(UTC)}
        )
