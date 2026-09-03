"""Shared exact report fixtures for evaluation tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import (
    AcceptedOutputEvidence,
    AttemptEvidence,
    CaseResult,
    EffectiveConfiguration,
    EvaluationProvenance,
    EvaluationReport,
    FaultEvidence,
    ModeResult,
    OutputValidationEvidence,
    SuiteManifestEntry,
)
from agent_reliability_lab.evaluation.runner import compare_modes, suite_sha256


def make_case(
    *,
    scenario_id: str = "case-1",
    mode: str = "resilient",
    correct: bool = True,
    terminal_success: bool = True,
    recovered: int = 1,
    recoverable: int = 1,
    accepted: int = 1,
    invalid_accepted: int = 0,
) -> CaseResult:
    declared = (
        (
            FaultEvidence(
                action_step=0,
                tool_name="search_recent_logs",
                attempt=1,
                kind="timeout",
            ),
        )
        if recoverable
        else ()
    )
    effective_recovered = recovered if correct else 0
    attempts = []
    if recoverable:
        attempts.append(
            AttemptEvidence(
                action_step=0,
                tool_name="search_recent_logs",
                attempt=1,
                status="failed",
                error_code="tool_timeout",
                transient=True,
            )
        )
        if effective_recovered:
            attempts.append(
                AttemptEvidence(
                    action_step=0,
                    tool_name="search_recent_logs",
                    attempt=2,
                    status="succeeded",
                )
            )
    elif accepted:
        attempts.append(
            AttemptEvidence(
                action_step=0,
                tool_name="search_recent_logs",
                attempt=1,
                status="succeeded",
            )
        )
    accepted_outputs = tuple(
        AcceptedOutputEvidence(
            action_step=index,
            tool_name="search_recent_logs",
            output=(
                {}
                if index < invalid_accepted
                else {
                    "entries": [
                        {"level": "ERROR", "message": "synthetic benchmark log"}
                    ]
                }
            ),
        )
        for index in range(accepted)
    )
    return CaseResult(
        scenario_id=scenario_id,
        scenario_version=1,
        scenario_path=f"{scenario_id}.yaml",
        scenario_sha256="1" * 64,
        mode=mode,
        run_id=UUID(int=1),
        trace_id=UUID(int=2),
        trace_digest="2" * 64,
        expected_outcome="resolved",
        observed_outcome="resolved" if correct else "tool_timeout",
        correct=correct,
        terminal_success=terminal_success,
        expected_tool_sequence=("search_recent_logs",),
        actual_tool_sequence=("search_recent_logs",),
        sequence_match_count=1,
        sequence_denominator=1,
        unnecessary_call_count=0,
        logical_tool_call_count=1,
        attempt_evidence=tuple(attempts),
        attempt_count=len(attempts),
        retry_attempt_count=effective_recovered,
        declared_faults=declared,
        observed_faults=declared,
        verified_transient_fault_count=recoverable,
        recovered_transient_fault_count=effective_recovered,
        accepted_outputs=accepted_outputs,
        accepted_output_count=accepted,
        invalid_output_accepted_count=invalid_accepted,
        invalid_output_detected_count=0,
        invalid_output_rejected_count=0,
        malformed_fault_injected_count=0,
        output_validation_failures=tuple[OutputValidationEvidence, ...](),
        latency_ns=1_000_000,
        estimated_input_tokens=0,
        estimated_output_tokens=0,
        estimated_cost_usd=Decimal(0),
        approval_reconstructed=False,
        pre_pause_write_execution_count=0,
        write_execution_count=0,
        store_run_count=1,
    )


def make_report(
    *,
    resilient_cases: tuple[CaseResult, ...] | None = None,
    fragile_cases: tuple[CaseResult, ...] | None = None,
    suite_hash: str | None = None,
) -> EvaluationReport:
    if resilient_cases is None:
        resilient_cases = (make_case(),)
    if fragile_cases is None:
        fragile_cases = tuple(
            make_case(
                scenario_id=case.scenario_id,
                mode="fragile",
                recovered=0,
                recoverable=case.verified_transient_fault_count,
                correct=False,
                terminal_success=False,
            )
            for case in resilient_cases
        )
    manifest = tuple(
        SuiteManifestEntry(
            relative_path=f"{case.scenario_id}.yaml",
            scenario_id=case.scenario_id,
            version=1,
            scenario_sha256=case.scenario_sha256,
        )
        for case in resilient_cases
    )
    provenance = EvaluationProvenance(
        report_version="2",
        schema_version="2",
        grader_version="exact-v2",
        normalization_version="baseline-v2",
        suite_hash=suite_hash or suite_sha256(manifest),
        suite_manifest=manifest,
        git_revision="f" * 40,
        git_dirty=False,
        policy_name="scripted",
        effective_configuration=EffectiveConfiguration(),
        python_version="3.12.0",
        package_version="0.1.0",
        latency_kind="perf_counter_ns",
        percentile_method="nearest-rank",
        token_estimator="scripted-no-provider-v1",
    )
    report = EvaluationReport(
        evaluation_id=UUID(int=3),
        generated_at=datetime(2026, 9, 4, tzinfo=UTC),
        provenance=provenance,
        modes={
            "fragile": ModeResult(
                mode="fragile",
                cases=fragile_cases,
                metrics=aggregate_cases(fragile_cases),
            ),
            "resilient": ModeResult(
                mode="resilient",
                cases=resilient_cases,
                metrics=aggregate_cases(resilient_cases),
            ),
        },
    )
    return report.model_copy(update={"comparison": compare_modes(report)})


@pytest.fixture
def report_factory():
    return make_report
