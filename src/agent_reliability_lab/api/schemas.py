"""Strict request models and deliberately narrow public response DTOs."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from agent_reliability_lab.domain.runs import RunStatus
from agent_reliability_lab.evaluation.models import ModeComparison, ModeMetrics

_CATALOG_ID = r"^[a-z0-9](?:[a-z0-9-]{0,127})$"


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunCreateRequest(StrictRequest):
    scenario_id: str = Field(min_length=1, max_length=128, pattern=_CATALOG_ID)
    mode: Literal["fragile", "resilient"]


class ApprovalRequest(StrictRequest):
    actor: str = Field(min_length=1, max_length=128)
    allow: StrictBool
    reason: str | None = Field(default=None, max_length=500)


class EvaluationCreateRequest(StrictRequest):
    suite: str = Field(min_length=1, max_length=128, pattern=_CATALOG_ID)


class RunResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str | None = None
    summary: str | None = None
    code: str | None = None
    reason: str | None = None
    explanation: str | None = None
    evidence_refs: tuple[str, ...] = ()


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    trace_id: UUID
    scenario_id: str
    mode: Literal["fragile", "resilient"]
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    duration_ms: float
    approval_required: bool
    attempt_count: int = Field(ge=0)
    result: RunResultResponse | None = None


class RunListResponse(BaseModel):
    items: list[RunResponse]


class ScenarioFaultResponse(BaseModel):
    tool_name: str
    attempt: int
    type: Literal["timeout", "rate_limit", "tool_error", "malformed_output"]


class ScenarioResponse(BaseModel):
    id: str
    expected_outcome: str
    expected_tool_sequence: tuple[str, ...]
    faults: tuple[ScenarioFaultResponse, ...]
    approval_required: bool


class ScenarioListResponse(BaseModel):
    items: list[ScenarioResponse]


class TracePayloadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str | None = None
    attempt: int | None = None
    after_attempt: int | None = None
    action_step: int | None = None
    step: int | None = None
    code: str | None = None
    kind: str | None = None
    transient: bool | None = None
    cached: bool | None = None
    retry_delay_seconds: float | None = None
    source: str | None = None
    actor: str | None = None
    allow: bool | None = None
    outcome: str | None = None
    summary: str | None = None
    reason: str | None = None
    from_status: str | None = None


class TraceEventResponse(BaseModel):
    id: UUID
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    sequence: int
    event_type: str
    payload: TracePayloadResponse
    duration_ms: float | None
    status: str
    created_at: datetime


class TracePageResponse(BaseModel):
    events: list[TraceEventResponse]
    next_after_sequence: int
    has_more: bool


class EvaluationCaseResponse(BaseModel):
    scenario_id: str
    mode: Literal["fragile", "resilient"]
    run_id: UUID
    final_status: Literal["succeeded", "failed", "waiting_approval"]
    expected_outcome: str
    observed_outcome: str
    correct: bool
    attempt_count: int
    retry_attempt_count: int


class EvaluationModeResponse(BaseModel):
    mode: Literal["fragile", "resilient"]
    metrics: ModeMetrics
    cases: tuple[EvaluationCaseResponse, ...]


class EvaluationProvenanceResponse(BaseModel):
    report_version: str
    grader_version: str
    suite_hash: str
    git_revision: str
    git_dirty: bool
    package_version: str


class EvaluationResponse(BaseModel):
    evaluation_id: UUID
    generated_at: datetime
    provenance: EvaluationProvenanceResponse
    modes: dict[Literal["fragile", "resilient"], EvaluationModeResponse]
    comparison: ModeComparison | None


class EvaluationListResponse(BaseModel):
    items: list[EvaluationResponse]


class HealthResponse(BaseModel):
    status: Literal["ready"]
    database: Literal["ready"]


__all__ = [
    "ApprovalRequest",
    "EvaluationCreateRequest",
    "EvaluationListResponse",
    "EvaluationResponse",
    "HealthResponse",
    "RunCreateRequest",
    "RunListResponse",
    "RunResponse",
    "ScenarioListResponse",
    "TracePageResponse",
]
