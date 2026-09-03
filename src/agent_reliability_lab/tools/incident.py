"""Deterministic incident-response tools used by the reliability scenarios."""

from pydantic import BaseModel, ConfigDict, Field

from agent_reliability_lab.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)


class EmptyInput(BaseModel):
    """An input schema that rejects unrecognised arguments."""

    model_config = ConfigDict(extra="forbid")


class ServiceHealthOutput(BaseModel):
    """Health state for the fixed incident service."""

    model_config = ConfigDict(extra="forbid")

    service: str
    status: str


class LogEntry(BaseModel):
    """One redacted-safe synthetic incident log entry."""

    model_config = ConfigDict(extra="forbid")

    level: str
    message: str


class RecentLogsOutput(BaseModel):
    """Recent synthetic logs for the fixed incident."""

    model_config = ConfigDict(extra="forbid")

    entries: list[LogEntry]


class DeploymentOutput(BaseModel):
    """Current deployment metadata."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    version: str


class PrepareRollbackInput(BaseModel):
    """Input for the approval-gated rollback preparation action."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1, max_length=128)


class PrepareRollbackOutput(BaseModel):
    """Confirmation that rollback work was prepared, never executed."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    prepared: bool


class IncidentBackend:
    """In-memory deterministic fixture backend; it never invokes a shell."""

    def __init__(self) -> None:
        self.rollback_preparations = 0
        self._rollback_by_token: dict[str, PrepareRollbackOutput] = {}

    async def get_service_health(
        self, _: EmptyInput, __: ToolExecutionContext
    ) -> ServiceHealthOutput:
        return ServiceHealthOutput(service="checkout", status="degraded")

    async def search_recent_logs(
        self, _: EmptyInput, __: ToolExecutionContext
    ) -> RecentLogsOutput:
        return RecentLogsOutput(
            entries=[
                LogEntry(level="ERROR", message="checkout timeout contacting ledger")
            ]
        )

    async def get_deployment(
        self, _: EmptyInput, __: ToolExecutionContext
    ) -> DeploymentOutput:
        return DeploymentOutput(
            deployment_id="deploy-2026-09-04-001", version="2026.09.04.1"
        )

    async def prepare_rollback(
        self, value: PrepareRollbackInput, context: ToolExecutionContext
    ) -> PrepareRollbackOutput:
        existing = self._rollback_by_token.get(context.idempotency_token)
        if existing is not None:
            return existing
        self.rollback_preparations += 1
        prepared = PrepareRollbackOutput(
            deployment_id=value.deployment_id, prepared=True
        )
        self._rollback_by_token[context.idempotency_token] = prepared
        return prepared


def incident_registry(backend: IncidentBackend) -> ToolRegistry:
    """Build the fixed, closed set of incident tool definitions."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "get_service_health",
            EmptyInput,
            ServiceHealthOutput,
            backend.get_service_health,
        )
    )
    registry.register(
        ToolDefinition(
            "search_recent_logs",
            EmptyInput,
            RecentLogsOutput,
            backend.search_recent_logs,
        )
    )
    registry.register(
        ToolDefinition(
            "get_deployment", EmptyInput, DeploymentOutput, backend.get_deployment
        )
    )
    registry.register(
        ToolDefinition(
            "prepare_rollback",
            PrepareRollbackInput,
            PrepareRollbackOutput,
            backend.prepare_rollback,
            requires_approval=True,
            is_write=True,
            idempotent=True,
        )
    )
    return registry
