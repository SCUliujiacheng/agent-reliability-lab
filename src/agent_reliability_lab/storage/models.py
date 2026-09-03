"""Pydantic contracts for durable trace events."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class TraceEvent(BaseModel):
    """One ordered, serializable event emitted while a run is executing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    trace_id: UUID
    event_type: str = Field(min_length=1, max_length=128)
    payload: JsonValue
    created_at: datetime
    sequence: int | None = Field(default=None, ge=1)

    @classmethod
    def new(cls, trace_id: UUID, event_type: str, payload: JsonValue) -> "TraceEvent":
        """Create an unsequenced event with an auditable UTC timestamp."""
        return cls(
            id=uuid4(),
            trace_id=trace_id,
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(UTC),
        )
