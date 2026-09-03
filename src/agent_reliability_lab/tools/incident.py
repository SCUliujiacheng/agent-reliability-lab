"""Deterministic incident-response tools used by the reliability scenarios."""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from agent_reliability_lab.tools.contracts import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
)

_FROZEN_INCIDENT_ACTIONS: dict[str, tuple[dict[str, JsonValue], ...]] = {
    "approval-reconstruction": (
        {
            "type": "call_tool",
            "tool_name": "get_deployment",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "prepare_rollback",
            "arguments": {"deployment_id": "deploy-2026-09-04-001"},
            "idempotency_key": "approval-reconstruction-v1",
        },
        {
            "type": "finish",
            "summary": "Rollback was prepared after durable approval reconstruction.",
            "evidence_refs": ["deployment", "approval"],
            "outcome": "prepared",
        },
    ),
    "malformed-output-rejected": (
        {
            "type": "call_tool",
            "tool_name": "get_service_health",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "get_deployment",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "finish",
            "summary": "This action is unreachable after safe schema rejection.",
            "evidence_refs": [],
            "outcome": "diagnosed",
        },
    ),
    "normal-success": (
        {
            "type": "call_tool",
            "tool_name": "get_service_health",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "search_recent_logs",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "get_deployment",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "finish",
            "summary": "Checkout degradation traced to the current deployment.",
            "evidence_refs": ["health", "logs", "deployment"],
            "outcome": "diagnosed",
        },
    ),
    "permanent-invalid-input": (
        {
            "type": "call_tool",
            "tool_name": "search_recent_logs",
            "arguments": {"unsupported_filter": True},
            "idempotency_key": None,
        },
        {
            "type": "finish",
            "summary": "This action is unreachable after input rejection.",
            "evidence_refs": [],
            "outcome": "diagnosed",
        },
    ),
    "rate-limit-recovery": (
        {
            "type": "call_tool",
            "tool_name": "get_service_health",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "get_deployment",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "finish",
            "summary": (
                "Deployment evidence was collected after rate limiting cleared."
            ),
            "evidence_refs": ["health", "deployment"],
            "outcome": "diagnosed",
        },
    ),
    "timeout-recovery": (
        {
            "type": "call_tool",
            "tool_name": "get_service_health",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "search_recent_logs",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "call_tool",
            "tool_name": "get_deployment",
            "arguments": {},
            "idempotency_key": None,
        },
        {
            "type": "finish",
            "summary": "Investigation recovered after the transient timeout.",
            "evidence_refs": ["health", "logs", "deployment"],
            "outcome": "diagnosed",
        },
    ),
}


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


def deterministic_incident_output(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, JsonValue] | None:
    """Return the immutable expected fixture output for one valid built-in call."""
    if tool_name == "get_service_health" and arguments == {}:
        return {"service": "checkout", "status": "degraded"}
    if tool_name == "search_recent_logs" and arguments == {}:
        return {
            "entries": [
                {
                    "level": "ERROR",
                    "message": "checkout timeout contacting ledger",
                }
            ]
        }
    if tool_name == "get_deployment" and arguments == {}:
        return {
            "deployment_id": "deploy-2026-09-04-001",
            "version": "2026.09.04.1",
        }
    if tool_name == "prepare_rollback" and arguments == {
        "deployment_id": "deploy-2026-09-04-001"
    }:
        return {"deployment_id": "deploy-2026-09-04-001", "prepared": True}
    return None


def deterministic_incident_actions(
    scenario_id: str,
) -> tuple[dict[str, JsonValue], ...] | None:
    """Return an independent copy of one built-in scenario's golden actions."""
    actions = _FROZEN_INCIDENT_ACTIONS.get(scenario_id)
    return deepcopy(actions) if actions is not None else None


def deterministic_incident_initial_context(
    scenario_id: str,
) -> dict[str, JsonValue] | None:
    """Return the independently frozen initial context for the golden suite."""
    incident_by_scenario = {
        "approval-reconstruction": "rollback-candidate",
        "malformed-output-rejected": "malformed-deployment-response",
        "normal-success": "checkout-latency",
        "permanent-invalid-input": "invalid-query",
        "rate-limit-recovery": "deployment-rate-limit",
        "timeout-recovery": "checkout-timeout",
    }
    incident = incident_by_scenario.get(scenario_id)
    return {"incident": incident} if incident is not None else None
