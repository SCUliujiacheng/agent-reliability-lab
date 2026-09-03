"""Privacy-preserving recording of trace events."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import JsonValue, TypeAdapter

from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore

_SENSITIVE_KEYS = {"authorization", "api_key", "apikey", "password", "secret", "token"}
_REDACTED = "[REDACTED]"
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def sanitize_payload(value: JsonValue, secret_values: set[str]) -> JsonValue:
    """Recursively replace secret-bearing fields and known secret values."""
    return _sanitize(value, secret_values)


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


def _sanitize(value: Any, secret_values: set[str]) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED
            if str(key).lower() in _SENSITIVE_KEYS
            else _sanitize(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, secret_values) for item in value]
    if isinstance(value, str) and value in secret_values:
        return _REDACTED
    return _JSON_VALUE.validate_python(value)
