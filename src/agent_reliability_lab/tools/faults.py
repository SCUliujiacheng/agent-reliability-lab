"""Deterministic, schema-validated fault injection for benchmark tools."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ToolExecutionError(Exception):
    """Base error carrying a stable tool-gateway failure code."""

    def __init__(self, code: str, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.code = code
        self.transient = transient


class TransientToolError(ToolExecutionError):
    """A retryable failure from a tool or deterministic fault plan."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, transient=True)


class PermanentToolError(ToolExecutionError):
    """A non-retryable tool failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, transient=False)


class FaultKind(StrEnum):
    """Faults deliberately supported by the deterministic benchmark."""

    TIMEOUT = "timeout"
    TRANSIENT_FAILURE = "transient_failure"


class FaultEvent(BaseModel):
    """Typed fault payload emitted to the trace before a failed attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1)
    kind: FaultKind
    code: str = Field(min_length=1, max_length=128)
    transient: bool = True


class FaultRule(BaseModel):
    """Inject one named fault on one exact tool attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1)
    kind: FaultKind

    def fault_for(self, tool_name: str, attempt: int) -> FaultEvent | None:
        """Return a fault only for the exact configured action wire fields."""
        if self.tool_name != tool_name or self.attempt != attempt:
            return None
        code = "tool_timeout" if self.kind is FaultKind.TIMEOUT else "transient_fault"
        return FaultEvent(
            tool_name=tool_name,
            attempt=attempt,
            kind=self.kind,
            code=code,
        )


class FaultPlan:
    """A deterministic collection of explicit fault rules."""

    def __init__(self, rules: tuple[FaultRule, ...] = ()) -> None:
        if any(not isinstance(rule, FaultRule) for rule in rules):
            raise TypeError("FaultPlan rules must be FaultRule instances")
        self._rules = rules

    @property
    def rules(self) -> tuple[FaultRule, ...]:
        """Expose the immutable, already runtime-validated rule set."""
        return self._rules

    def fault_for(self, tool_name: str, attempt: int) -> FaultEvent | None:
        """Return the first matching deterministic fault."""
        for rule in self._rules:
            fault = rule.fault_for(tool_name, attempt)
            if fault is not None:
                return fault
        return None


class InjectedFault(TransientToolError):
    """A typed retryable error created from a matching fault rule."""

    def __init__(self, kind: FaultKind, *, tool_name: str) -> None:
        code = "tool_timeout" if kind is FaultKind.TIMEOUT else "transient_fault"
        super().__init__(code, f"injected {kind} for {tool_name}")
        self.kind = kind
        self.tool_name = tool_name


def no_faults() -> FaultPlan:
    """Return an empty deterministic plan."""
    return FaultPlan()


def timeout_on_attempt(
    attempt: int, *, tool_name: str = "search_recent_logs"
) -> FaultPlan:
    """Inject a transient timeout at one reproducible tool attempt."""
    return FaultPlan(
        (FaultRule(tool_name=tool_name, attempt=attempt, kind=FaultKind.TIMEOUT),)
    )
