"""Privacy-preserving recording of trace events."""

from uuid import UUID

from pydantic import JsonValue

from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.sanitization import sanitize_payload
from agent_reliability_lab.storage.store import SQLiteRunStore


class TraceRecorder:
    """Records ordered trace events after sanitizing untrusted payloads."""

    def __init__(
        self, store: SQLiteRunStore, secret_values: set[str] | None = None
    ) -> None:
        self._store = store
        self._secret_values = secret_values or set()

    def record(
        self,
        trace_id: UUID,
        event_type: str,
        payload: JsonValue,
        *,
        span_id: UUID | None = None,
        parent_span_id: UUID | None = None,
        duration_ms: float | None = None,
        status: str = "ok",
        extra_secret_values: set[str] | None = None,
    ) -> TraceEvent:
        """Sanitize and append one trace event."""
        return self._store.append_event(
            self.build_event(
                trace_id,
                event_type,
                payload,
                span_id=span_id,
                parent_span_id=parent_span_id,
                duration_ms=duration_ms,
                status=status,
                extra_secret_values=extra_secret_values,
            )
        )

    def build_event(
        self,
        trace_id: UUID,
        event_type: str,
        payload: JsonValue,
        *,
        span_id: UUID | None = None,
        parent_span_id: UUID | None = None,
        duration_ms: float | None = None,
        status: str = "ok",
        extra_secret_values: set[str] | None = None,
    ) -> TraceEvent:
        """Build one sanitized event for ordinary or atomic persistence."""
        secret_values = self._secret_values | (extra_secret_values or set())
        return TraceEvent.new(
            trace_id,
            event_type,
            sanitize_payload(payload, secret_values),
            span_id=span_id,
            parent_span_id=parent_span_id,
            duration_ms=duration_ms,
            status=status,
        )
