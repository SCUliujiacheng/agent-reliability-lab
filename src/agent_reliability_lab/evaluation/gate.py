"""Exact-fraction reliability gate with summary and baseline integrity checks."""

import hashlib
import json
from collections import Counter, defaultdict
from fractions import Fraction

from pydantic import ValidationError

from agent_reliability_lab.evaluation.graders import (
    aggregate_cases,
    tool_sequence_grade,
    unnecessary_call_count,
)
from agent_reliability_lab.evaluation.models import (
    CaseResult,
    EvaluationReport,
    FaultEvidence,
    GateResult,
)
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry

_TRANSIENT_KINDS = {"timeout", "rate_limit", "tool_error"}
_FAULT_FAILURE = {
    "timeout": ("tool_timeout", True),
    "rate_limit": ("rate_limit", True),
    "tool_error": ("tool_error", True),
    "malformed_output": ("invalid_output", False),
}


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
    resilient = report.modes["resilient"]
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
    if set(report.modes) != {"fragile", "resilient"}:
        errors.append("mode_set_mismatch")
        return tuple(errors)
    if report.provenance.suite_hash != _manifest_hash(report):
        errors.append("suite_hash_mismatch")
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
        for case in result.cases:
            errors.extend(_case_evidence_errors(case))
    try:
        from agent_reliability_lab.evaluation.runner import compare_modes

        expected_comparison = compare_modes(report)
    except Exception:  # noqa: BLE001 - malformed reports fail closed at the gate.
        errors.append("comparison_mismatch")
    else:
        if report.comparison != expected_comparison:
            errors.append("comparison_mismatch")
    return tuple(dict.fromkeys(errors))


def _manifest_hash(report: EvaluationReport) -> str:
    canonical = json.dumps(
        [
            entry.model_dump(mode="json")
            for entry in sorted(
                report.provenance.suite_manifest,
                key=lambda item: item.relative_path,
            )
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _case_evidence_errors(case: CaseResult) -> list[str]:
    errors: list[str] = []
    if case.correct != (case.observed_outcome == case.expected_outcome):
        errors.append("case_outcome_mismatch")

    sequence = tool_sequence_grade(
        case.expected_tool_sequence, case.actual_tool_sequence
    )
    if (
        case.sequence_match_count != sequence.match_count
        or case.sequence_denominator != sequence.denominator
    ):
        errors.append("sequence_evidence_mismatch")
    if case.logical_tool_call_count != len(
        case.actual_tool_sequence
    ) or case.unnecessary_call_count != unnecessary_call_count(
        case.expected_tool_sequence, case.actual_tool_sequence
    ):
        errors.append("call_counter_mismatch")

    if Counter(case.declared_faults) != Counter(case.observed_faults):
        errors.append("case_evidence_mismatch")
    if any(
        not _fault_has_matching_failure(fault, case) for fault in case.observed_faults
    ):
        errors.append("case_evidence_mismatch")
    transient_faults = tuple(
        fault for fault in case.observed_faults if fault.kind in _TRANSIENT_KINDS
    )
    recovered = sum(_fault_recovered(fault, case) for fault in transient_faults)
    malformed = sum(fault.kind == "malformed_output" for fault in case.observed_faults)
    retry_attempts = _retry_attempt_count(case)
    if (
        case.attempt_count != len(case.attempt_evidence)
        or not _attempts_are_contiguous(case)
        or case.verified_transient_fault_count != len(transient_faults)
        or case.recovered_transient_fault_count != recovered
        or case.retry_attempt_count != retry_attempts
        or case.malformed_fault_injected_count != malformed
    ):
        errors.append("case_evidence_mismatch")

    validation_identities = Counter(
        (item.action_step, item.tool_name, item.attempt)
        for item in case.output_validation_failures
    )
    failed_invalid_identities = Counter(
        (item.action_step, item.tool_name, item.attempt)
        for item in case.attempt_evidence
        if item.status == "failed" and item.error_code == "invalid_output"
    )
    if validation_identities != failed_invalid_identities:
        errors.append("case_evidence_mismatch")
    invalid_accepted = _invalid_accepted_output_count(case)
    if (
        case.accepted_output_count != len(case.accepted_outputs)
        or case.invalid_output_accepted_count != invalid_accepted
        or case.invalid_output_detected_count != len(case.output_validation_failures)
        or case.invalid_output_rejected_count != len(case.output_validation_failures)
    ):
        errors.append("case_evidence_mismatch")

    if case.approval_reconstructed and (
        case.pre_pause_write_execution_count != 0 or case.write_execution_count != 1
    ):
        errors.append("case_evidence_mismatch")
    return errors


def _fault_recovered(fault: FaultEvidence, case: CaseResult) -> int:
    return int(
        case.correct
        and any(
            attempt.action_step == fault.action_step
            and attempt.tool_name == fault.tool_name
            and attempt.attempt > fault.attempt
            and attempt.status == "succeeded"
            for attempt in case.attempt_evidence
        )
    )


def _fault_has_matching_failure(fault: FaultEvidence, case: CaseResult) -> bool:
    expected_code, expected_transient = _FAULT_FAILURE[fault.kind]
    matches = [
        attempt
        for attempt in case.attempt_evidence
        if attempt.action_step == fault.action_step
        and attempt.tool_name == fault.tool_name
        and attempt.attempt == fault.attempt
        and attempt.status == "failed"
        and attempt.error_code == expected_code
        and attempt.transient is expected_transient
    ]
    return len(matches) == 1


def _attempts_are_contiguous(case: CaseResult) -> bool:
    attempts_by_action: dict[tuple[int, str], list[int]] = defaultdict(list)
    for attempt in case.attempt_evidence:
        if attempt.status == "succeeded" and (
            attempt.error_code is not None or attempt.transient is not None
        ):
            return False
        if attempt.status == "failed" and (
            attempt.error_code is None or attempt.transient is None
        ):
            return False
        attempts_by_action[(attempt.action_step, attempt.tool_name)].append(
            attempt.attempt
        )
    return all(
        sorted(attempts) == list(range(1, len(attempts) + 1))
        for attempts in attempts_by_action.values()
    )


def _retry_attempt_count(case: CaseResult) -> int:
    attempts_by_action: dict[tuple[int, str], int] = defaultdict(int)
    for attempt in case.attempt_evidence:
        attempts_by_action[(attempt.action_step, attempt.tool_name)] += 1
    return sum(max(0, count - 1) for count in attempts_by_action.values())


def _invalid_accepted_output_count(case: CaseResult) -> int:
    registry = incident_registry(IncidentBackend())
    invalid = 0
    for accepted in case.accepted_outputs:
        definition = registry.get(accepted.tool_name)
        if definition is None:
            invalid += 1
            continue
        try:
            definition.output_model.model_validate(accepted.output)
        except ValidationError:
            invalid += 1
    return invalid


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
