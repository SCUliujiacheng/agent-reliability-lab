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
        status: str = "ok",
    ) -> TraceEvent:
        """Sanitize and append one trace event."""
        event = TraceEvent.new(
            trace_id,
            event_type,
            sanitize_payload(payload, self._secret_values),
            span_id=span_id,
            parent_span_id=parent_span_id,
            status=status,
        )
        return self._store.append_event(event)
