"""Regression-gate exactness and anti-tampering behavior."""

from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import ModeResult

from .conftest import make_case, make_report


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
    current = make_report(suite_hash="a" * 64)
    baseline = make_report(suite_hash="b" * 64)

    result = enforce_gate(current, baseline=baseline)

    assert result.passed is False
    assert result.comparable is False
    assert "incomparable_baseline" in result.infrastructure_errors
