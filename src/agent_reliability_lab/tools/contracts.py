"""Typed contracts for registered tool handlers."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must not be negative")
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
