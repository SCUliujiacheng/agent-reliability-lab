"""Typed contracts for registered tool handlers."""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# At these caps, one call can spend at most 300 seconds in handlers and
# 75 seconds in deterministic backoff (5 + 10 + 20 + 40).
MAX_TOOL_ATTEMPTS = 5
MAX_TOOL_TIMEOUT_SECONDS = 60.0
MAX_TOOL_INITIAL_DELAY_SECONDS = 5.0


class ToolExecutionContext(BaseModel):
    """Stable per-request identity supplied to a tool handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    tool_name: str = Field(min_length=1, max_length=128)
    idempotency_token: str = Field(min_length=1, max_length=128)
    request_fingerprint: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True, slots=True)
class ToolDefinition[InputT: BaseModel, OutputT: BaseModel]:
    """One named handler whose request and response schemas are explicit."""

    name: str
    input_model: type[InputT]
    output_model: type[OutputT]
    handler: Callable[[InputT, ToolExecutionContext], Awaitable[Any]]
    requires_approval: bool = False
    is_write: bool = False
    idempotent: bool = False
    timeout_seconds: float = 1.0
    max_attempts: int = 2
    initial_delay_seconds: float = 0.1

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("tool name must be between 1 and 128 characters")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or (
                isinstance(self.timeout_seconds, float)
                and not math.isfinite(self.timeout_seconds)
            )
        ):
            raise ValueError("timeout_seconds must be finite")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.timeout_seconds > MAX_TOOL_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must not exceed {MAX_TOOL_TIMEOUT_SECONDS:g}"
            )
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            # Definition validation consistently exposes ValueError to callers.
            raise ValueError("max_attempts must be an integer")  # noqa: TRY004
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.max_attempts > MAX_TOOL_ATTEMPTS:
            raise ValueError(f"max_attempts must not exceed {MAX_TOOL_ATTEMPTS}")
        if (
            isinstance(self.initial_delay_seconds, bool)
            or not isinstance(self.initial_delay_seconds, (int, float))
            or (
                isinstance(self.initial_delay_seconds, float)
                and not math.isfinite(self.initial_delay_seconds)
            )
        ):
            raise ValueError("initial_delay_seconds must be finite")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
        if self.initial_delay_seconds > MAX_TOOL_INITIAL_DELAY_SECONDS:
            raise ValueError(
                "initial_delay_seconds must not exceed "
                f"{MAX_TOOL_INITIAL_DELAY_SECONDS:g}"
            )
        if (self.is_write or self.requires_approval) and not self.idempotent:
            raise ValueError("write or high-risk tools must be declared idempotent")

    def delay_seconds(self, attempt: int) -> float:
        """Return a deterministic exponential retry delay for a failed attempt."""
        return self.initial_delay_seconds * float(2 ** (attempt - 1))


class ToolRegistry:
    """Closed registry that rejects duplicate tool names."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition[Any, Any]] = {}

    def register(self, definition: ToolDefinition[Any, Any]) -> None:
        """Register a definition exactly once to prevent handler replacement."""
        if definition.name in self._definitions:
            raise ValueError(f"tool already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition[Any, Any] | None:
        """Return the matching definition, with no dynamic fallback."""
        return self._definitions.get(name)
