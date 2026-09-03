"""Pydantic contracts for durable trace events."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class TraceEvent(BaseModel):
    """One ordered event with trace/span context and a safe JSON payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None = None
    event_type: str = Field(min_length=1, max_length=128)
    payload: JsonValue
    attributes: JsonValue = Field(default_factory=dict)
    duration_ms: float | None = Field(default=None, ge=0)
    status: str = Field(default="ok", min_length=1, max_length=64)
    created_at: datetime
    sequence: int | None = Field(default=None, ge=1)

    @classmethod
    def new(
        cls,
        trace_id: UUID,
        event_type: str,
        payload: JsonValue,
        *,
        span_id: UUID | None = None,
        parent_span_id: UUID | None = None,
        duration_ms: float | None = None,
        status: str = "ok",
        attributes: JsonValue | None = None,
    ) -> "TraceEvent":
        """Create an unsequenced event with required trace/span identifiers."""
        return cls(
            id=uuid4(),
            trace_id=trace_id,
            span_id=span_id or uuid4(),
            parent_span_id=parent_span_id,
            event_type=event_type,
            payload=payload,
            attributes=attributes if attributes is not None else {},
            duration_ms=duration_ms,
            status=status,
            created_at=datetime.now(UTC),
        )
