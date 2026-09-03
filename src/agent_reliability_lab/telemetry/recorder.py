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

    def record(self, trace_id: UUID, event_type: str, payload: JsonValue) -> TraceEvent:
        """Sanitize and append one trace event."""
        event = TraceEvent.new(
            trace_id, event_type, sanitize_payload(payload, self._secret_values)
        )
        return self._store.append_event(event)
