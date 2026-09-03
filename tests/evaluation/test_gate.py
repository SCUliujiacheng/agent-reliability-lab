"""Regression-gate exactness and anti-tampering behavior."""

import pytest

from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import (
    CaseResult,
    EvaluationReport,
    ModeResult,
)
from agent_reliability_lab.evaluation.runner import compare_modes

from .conftest import make_case, make_report


def _replace_resilient_case(
    report: EvaluationReport, changed: CaseResult
) -> EvaluationReport:
    resilient = report.modes["resilient"]
    cases = (changed, *resilient.cases[1:])
    changed_mode = ModeResult(
        mode="resilient", cases=cases, metrics=aggregate_cases(cases)
    )
    candidate = report.model_copy(
        update={"modes": {**report.modes, "resilient": changed_mode}}
    )
    return candidate.model_copy(update={"comparison": compare_modes(candidate)})


def test_gate_accepts_exact_threshold_boundaries() -> None:
    cases = tuple(
        make_case(
            scenario_id=f"case-{index}",
            correct=index < 4,
            recovered=1 if index < 3 else 0,
            recoverable=1 if index < 4 else 0,
        )
        for index in range(5)
    )
    report = make_report(resilient_cases=cases)

    result = enforce_gate(report)

    assert result.passed is True
    assert result.failures == {}


def test_gate_fails_below_recovery_and_with_zero_denominator() -> None:
    below = tuple(
        make_case(
            scenario_id=f"case-{index}",
            recovered=1 if index < 2 else 0,
            recoverable=1,
        )
        for index in range(4)
    )
    result = enforce_gate(make_report(resilient_cases=below))
    assert result.passed is False
    assert "recovery_rate" in result.failures

    no_faults = (make_case(recovered=0, recoverable=0),)
    zero = enforce_gate(make_report(resilient_cases=no_faults))
    assert zero.passed is False
    assert "recovery_rate" in zero.failures


def test_gate_recomputes_summary_and_rejects_tampering() -> None:
    report = make_report()
    resilient = report.modes["resilient"]
    tampered_metrics = resilient.metrics.model_copy(
        update={"task_correctness_rate": 0.0}
    )
    tampered = report.model_copy(
        update={
            "modes": {
                **report.modes,
                "resilient": ModeResult(
                    mode="resilient",
                    cases=resilient.cases,
                    metrics=tampered_metrics,
                ),
            }
        }
    )

    result = enforce_gate(tampered)

    assert result.passed is False
    assert result.comparable is False
    assert "summary_mismatch" in result.infrastructure_errors


def test_gate_rejects_correct_flag_that_contradicts_exact_outcomes() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    tampered = _replace_resilient_case(
        report,
        case.model_copy(update={"observed_outcome": "different", "correct": True}),
    )

    result = enforce_gate(tampered)

    assert result.comparable is False
    assert "case_outcome_mismatch" in result.infrastructure_errors


def test_gate_rejects_lcs_and_sequence_counter_contradictions() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    tampered = _replace_resilient_case(
        report,
        case.model_copy(update={"sequence_match_count": 0}),
    )

    result = enforce_gate(tampered)

    assert result.comparable is False
    assert "sequence_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.parametrize(
    "updates",
    [
        {"logical_tool_call_count": 2},
        {"unnecessary_call_count": 1},
    ],
)
def test_gate_rejects_logical_and_unnecessary_call_counter_contradictions(
    updates: dict[str, int],
) -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]

    result = enforce_gate(
        _replace_resilient_case(report, case.model_copy(update=updates))
    )

    assert result.comparable is False
    assert "call_counter_mismatch" in result.infrastructure_errors


@pytest.mark.parametrize(
    "field",
    [
        "verified_transient_fault_count",
        "recovered_transient_fault_count",
        "retry_attempt_count",
        "malformed_fault_injected_count",
        "invalid_output_detected_count",
        "invalid_output_rejected_count",
    ],
)
def test_gate_rejects_fault_recovery_retry_and_validation_counter_tampering(
    field: str,
) -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    current = getattr(case, field)
    changed = 0 if current else 1

    result = enforce_gate(
        _replace_resilient_case(
            report,
            case.model_copy(update={field: changed}),
        )
    )

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_fault_or_attempt_evidence_that_cannot_support_recovery() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    missing_fault = _replace_resilient_case(
        report, case.model_copy(update={"observed_faults": ()})
    )
    missing_attempt = _replace_resilient_case(
        report, case.model_copy(update={"attempt_evidence": case.attempt_evidence[:1]})
    )
    wrong_fault_attempts = (
        case.attempt_evidence[0].model_copy(
            update={"status": "succeeded", "error_code": None, "transient": None}
        ),
        *case.attempt_evidence[1:],
    )
    wrong_fault_attempt = _replace_resilient_case(
        report, case.model_copy(update={"attempt_evidence": wrong_fault_attempts})
    )

    assert "case_evidence_mismatch" in enforce_gate(missing_fault).infrastructure_errors
    assert (
        "case_evidence_mismatch" in enforce_gate(missing_attempt).infrastructure_errors
    )
    assert (
        "case_evidence_mismatch"
        in enforce_gate(wrong_fault_attempt).infrastructure_errors
    )


def test_gate_rejects_suite_hash_mode_set_and_comparison_tampering() -> None:
    report = make_report()
    bad_hash = report.model_copy(
        update={
            "provenance": report.provenance.model_copy(update={"suite_hash": "b" * 64})
        }
    )
    missing_mode = report.model_copy(
        update={"modes": {"resilient": report.modes["resilient"]}}
    )
    extra_mode = report.model_copy(
        update={"modes": {**report.modes, "unexpected": report.modes["fragile"]}}
    )
    assert report.comparison is not None
    bad_comparison = report.model_copy(
        update={
            "comparison": report.comparison.model_copy(
                update={"task_correctness_rate_delta": 99.0}
            )
        }
    )

    assert "suite_hash_mismatch" in enforce_gate(bad_hash).infrastructure_errors
    assert "mode_set_mismatch" in enforce_gate(missing_mode).infrastructure_errors
    assert "mode_set_mismatch" in enforce_gate(extra_mode).infrastructure_errors
    assert "comparison_mismatch" in enforce_gate(bad_comparison).infrastructure_errors


def test_gate_rejects_self_consistent_cases_that_do_not_cover_manifest() -> None:
    """Recomputing a summary is insufficient if a case was deleted with it."""
    report = make_report()
    removed = ModeResult(
        mode="resilient",
        cases=(),
        metrics=aggregate_cases(()),
    )
    tampered = report.model_copy(
        update={"modes": {**report.modes, "resilient": removed}}
    )

    result = enforce_gate(tampered)

    assert result.passed is False
    assert result.comparable is False
    assert "case_manifest_mismatch" in result.infrastructure_errors


def test_success_regression_exactly_point_zero_five_passes_but_more_fails() -> None:
    baseline_cases = tuple(
        make_case(scenario_id=f"case-{index}") for index in range(20)
    )
    baseline = make_report(resilient_cases=baseline_cases)
    at_boundary = tuple(
        make_case(scenario_id=f"case-{index}", correct=index < 19)
        for index in range(20)
    )
    current = make_report(resilient_cases=at_boundary)

    assert enforce_gate(current, baseline=baseline).passed is True

    beyond = tuple(
        make_case(scenario_id=f"case-{index}", correct=index < 18)
        for index in range(20)
    )
    result = enforce_gate(make_report(resilient_cases=beyond), baseline=baseline)
    assert result.passed is False
    assert "success_regression" in result.failures


def test_incomparable_baseline_is_an_infrastructure_result() -> None:
    current = make_report()
    baseline = make_report(resilient_cases=(make_case(scenario_id="different-case"),))

    result = enforce_gate(current, baseline=baseline)

    assert result.passed is False
    assert result.comparable is False
    assert "incomparable_baseline" in result.infrastructure_errors
