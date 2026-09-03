"""Shared recursive sanitization used before every persisted trace write."""

from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue, TypeAdapter

_REDACTED = "[REDACTED]"
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_SENSITIVE_KEYS = {
    "authorization",
    "apikey",
    "password",
    "secret",
    "token",
    "xapikey",
}


def sanitize_payload(value: JsonValue, secret_values: set[str]) -> JsonValue:
    """Replace credential fields and all configured secret substrings recursively."""
    return _sanitize(value, secret_values)


def _sanitize(value: Any, secret_values: set[str]) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED
            if _normalize_key(str(key)) in _SENSITIVE_KEYS
            else _sanitize(item, secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, secret_values) for item in value]
    if isinstance(value, str):
        clean = value
        for secret in secret_values:
            clean = clean.replace(secret, _REDACTED)
        return clean
    return _JSON_VALUE.validate_python(value)


def _normalize_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())
