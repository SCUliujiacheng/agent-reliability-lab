"""Exact-fraction reliability gate with summary and baseline integrity checks."""

from fractions import Fraction

from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import EvaluationReport, GateResult


def enforce_gate(
    report: EvaluationReport, baseline: EvaluationReport | None = None
) -> GateResult:
    """Recompute evidence and enforce fixed resilient and regression thresholds."""
    report_errors = _integrity_errors(report)
    baseline_errors = _integrity_errors(baseline) if baseline is not None else ()
    integrity_errors = tuple(dict.fromkeys((*report_errors, *baseline_errors)))
    if integrity_errors:
        return GateResult(
            passed=False,
            comparable=False,
            failures={},
            infrastructure_errors=integrity_errors,
        )
    resilient = report.modes.get("resilient")
    if resilient is None:
        return GateResult(
            passed=False,
            comparable=False,
            failures={},
            infrastructure_errors=("missing_resilient_mode",),
        )
    if baseline is not None and not _comparable(report, baseline):
        return GateResult(
            passed=False,
            comparable=False,
            failures={},
            infrastructure_errors=("incomparable_baseline",),
        )

    metrics = resilient.metrics
    failures: dict[str, str] = {}
    correctness = _fraction(metrics.case_correct_count, metrics.case_count)
    if correctness < Fraction(4, 5):
        failures["task_correctness_rate"] = "must be at least 0.80"
    if metrics.transient_fault_count == 0:
        failures["recovery_rate"] = "requires a non-zero verified fault denominator"
    elif Fraction(
        metrics.recovered_transient_fault_count, metrics.transient_fault_count
    ) < Fraction(3, 4):
        failures["recovery_rate"] = "must be at least 0.75"
    if metrics.invalid_output_accepted_count != 0:
        failures["invalid_output_rate"] = "accepted invalid outputs must be zero"

    if baseline is not None:
        baseline_resilient = baseline.modes.get("resilient")
        if baseline_resilient is None:
            return GateResult(
                passed=False,
                comparable=False,
                failures={},
                infrastructure_errors=("incomparable_baseline",),
            )
        baseline_success = _fraction(
            baseline_resilient.metrics.case_correct_count,
            baseline_resilient.metrics.case_count,
        )
        if baseline_success - correctness > Fraction(1, 20):
            failures["success_regression"] = "must not regress by more than 0.05"

    return GateResult(
        passed=not failures,
        comparable=True,
        failures=failures,
    )


def _fraction(numerator: int, denominator: int) -> Fraction:
    return Fraction(numerator, denominator) if denominator else Fraction()


def _integrity_errors(report: EvaluationReport) -> tuple[str, ...]:
    errors: list[str] = []
    if any(
        result.metrics != aggregate_cases(result.cases)
        for result in report.modes.values()
    ):
        errors.append("summary_mismatch")
    expected = {
        (
            entry.scenario_id,
            entry.version,
            entry.relative_path,
            entry.scenario_sha256,
        )
        for entry in report.provenance.suite_manifest
    }
    if len(expected) != len(report.provenance.suite_manifest):
        errors.append("case_manifest_mismatch")
    for mode, result in report.modes.items():
        actual = {
            (
                case.scenario_id,
                case.scenario_version,
                case.scenario_path,
                case.scenario_sha256,
            )
            for case in result.cases
        }
        if (
            result.mode != mode
            or len(actual) != len(result.cases)
            or actual != expected
            or any(case.mode != mode for case in result.cases)
        ):
            errors.append("case_manifest_mismatch")
            break
    return tuple(dict.fromkeys(errors))


def _comparable(current: EvaluationReport, baseline: EvaluationReport) -> bool:
    current_provenance = current.provenance
    baseline_provenance = baseline.provenance
    return (
        current_provenance.suite_hash == baseline_provenance.suite_hash
        and current_provenance.suite_manifest == baseline_provenance.suite_manifest
        and current_provenance.report_version == baseline_provenance.report_version
        and current_provenance.schema_version == baseline_provenance.schema_version
        and current_provenance.grader_version == baseline_provenance.grader_version
        and current_provenance.effective_configuration
        == baseline_provenance.effective_configuration
        and set(current.modes) == set(baseline.modes)
    )


__all__ = ["enforce_gate"]
