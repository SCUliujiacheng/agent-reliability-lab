"""Strict report contracts for reproducible golden-suite evaluations."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type EvaluationMode = Literal["fragile", "resilient"]


class ReportModel(BaseModel):
    """A frozen report value that rejects extensions and non-finite numbers."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class SuiteManifestEntry(ReportModel):
    relative_path: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    scenario_sha256: str = Field(min_length=64, max_length=64)


class EffectiveConfiguration(ReportModel):
    fragile_max_attempts: int = 1
    resilient_max_attempts: int = 2
    timeout_seconds: float = 1.0
    initial_backoff_seconds: float = 0.1


class EvaluationProvenance(ReportModel):
    report_version: Literal["1"] = "1"
    schema_version: Literal["1"] = "1"
    grader_version: Literal["exact-v1"] = "exact-v1"
    normalization_version: Literal["baseline-v1"] = "baseline-v1"
    suite_hash: str = Field(min_length=64, max_length=64)
    suite_manifest: tuple[SuiteManifestEntry, ...]
    git_revision: str
    git_dirty: bool
    policy_name: Literal["scripted"] = "scripted"
    effective_configuration: EffectiveConfiguration
    python_version: str
    package_version: str
    latency_kind: Literal["perf_counter_ns"] = "perf_counter_ns"
    percentile_method: Literal["nearest-rank"] = "nearest-rank"
    token_estimator: Literal["scripted-no-provider-v1"] = "scripted-no-provider-v1"


class FaultEvidence(ReportModel):
    action_step: int = Field(ge=0)
    tool_name: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    kind: Literal["timeout", "rate_limit", "tool_error", "malformed_output"]


class CaseResult(ReportModel):
    scenario_id: str
    scenario_version: int = Field(ge=1)
    scenario_path: str
    scenario_sha256: str = Field(min_length=64, max_length=64)
    mode: EvaluationMode
    run_id: UUID
    trace_id: UUID
    trace_digest: str = Field(min_length=64, max_length=64)
    expected_outcome: str
    observed_outcome: str
    correct: bool
    terminal_success: bool
    expected_tool_sequence: tuple[str, ...]
    actual_tool_sequence: tuple[str, ...]
    sequence_match_count: int = Field(ge=0)
    sequence_denominator: int = Field(ge=0)
    unnecessary_call_count: int = Field(ge=0)
    logical_tool_call_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    retry_attempt_count: int = Field(ge=0)
    declared_faults: tuple[FaultEvidence, ...]
    observed_faults: tuple[FaultEvidence, ...]
    verified_transient_fault_count: int = Field(ge=0)
    recovered_transient_fault_count: int = Field(ge=0)
    accepted_output_count: int = Field(ge=0)
    invalid_output_accepted_count: int = Field(ge=0)
    invalid_output_detected_count: int = Field(ge=0)
    invalid_output_rejected_count: int = Field(ge=0)
    malformed_fault_injected_count: int = Field(ge=0)
    latency_ns: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)
    approval_reconstructed: bool
    write_execution_count: int = Field(ge=0)
    store_run_count: int = Field(ge=0)


class ModeMetrics(ReportModel):
    case_correct_count: int = Field(ge=0)
    case_count: int = Field(ge=0)
    task_correctness_rate: float = Field(ge=0, le=1)
    terminal_success_count: int = Field(ge=0)
    tool_sequence_accuracy: float = Field(ge=0, le=1)
    tool_sequence_case_count: int = Field(ge=0)
    recovered_transient_fault_count: int = Field(ge=0)
    transient_fault_count: int = Field(ge=0)
    recovery_rate: float | None = Field(default=None, ge=0, le=1)
    invalid_output_accepted_count: int = Field(ge=0)
    accepted_output_count: int = Field(ge=0)
    invalid_output_rate: float = Field(ge=0, le=1)
    invalid_output_detected_count: int = Field(ge=0)
    invalid_output_rejected_count: int = Field(ge=0)
    malformed_fault_injected_count: int = Field(ge=0)
    unnecessary_call_count: int = Field(ge=0)
    retry_attempt_count: int = Field(ge=0)
    p50_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0)


class ModeResult(ReportModel):
    mode: EvaluationMode
    cases: tuple[CaseResult, ...]
    metrics: ModeMetrics


class ModeComparison(ReportModel):
    task_correctness_rate_delta: float
    recovery_rate_delta: float | None
    tool_sequence_accuracy_delta: float
    invalid_output_rate_delta: float
    unnecessary_call_count_delta: int
    retry_attempt_count_delta: int
    p50_latency_ms_delta: float
    p95_latency_ms_delta: float
    fragile_worse_recovery_scenarios: tuple[str, ...]


class EvaluationReport(ReportModel):
    evaluation_id: UUID
    generated_at: datetime
    provenance: EvaluationProvenance
    modes: dict[EvaluationMode, ModeResult]
    comparison: ModeComparison | None = None


class GateResult(ReportModel):
    passed: bool
    comparable: bool
    failures: dict[str, str]
    infrastructure_errors: tuple[str, ...] = ()


__all__ = [
    "CaseResult",
    "EffectiveConfiguration",
    "EvaluationMode",
    "EvaluationProvenance",
    "EvaluationReport",
    "FaultEvidence",
    "GateResult",
    "ModeComparison",
    "ModeMetrics",
    "ModeResult",
    "SuiteManifestEntry",
]
