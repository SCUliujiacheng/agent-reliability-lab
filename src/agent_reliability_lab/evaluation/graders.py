"""Exact graders whose expectations come only from frozen scenario data."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from math import ceil

from agent_reliability_lab.evaluation.models import CaseResult, ModeMetrics


@dataclass(frozen=True, slots=True)
class SequenceGrade:
    match_count: int
    denominator: int
    accuracy: float


def tool_sequence_grade(
    expected: Sequence[str], actual: Sequence[str]
) -> SequenceGrade:
    """Grade order and multiplicity with LCS divided by the longer sequence."""
    denominator = max(len(expected), len(actual))
    matches = _lcs_length(expected, actual)
    accuracy = 1.0 if denominator == 0 else float(Fraction(matches, denominator))
    return SequenceGrade(matches, denominator, accuracy)


def tool_sequence_accuracy(expected: Sequence[str], actual: Sequence[str]) -> float:
    """Compatibility convenience returning only the exact LCS-derived rate."""
    return tool_sequence_grade(expected, actual).accuracy


def unnecessary_call_count(expected: Sequence[str], actual: Sequence[str]) -> int:
    """Count multiset excess without treating retry attempts as logical calls."""
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    return sum(
        max(0, count - expected_counts[name]) for name, count in actual_counts.items()
    )


def nearest_rank_percentile(values: Sequence[int], percentile: float) -> int:
    """Return the nearest-rank percentile (rank = ceil(p*n), one-indexed)."""
    if not values:
        return 0
    if percentile <= 0 or percentile > 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(values)
    return ordered[ceil(percentile * len(ordered)) - 1]


def aggregate_cases(cases: Sequence[CaseResult]) -> ModeMetrics:
    """Macro-average cases while retaining all source counts in the report."""
    count = len(cases)
    correct = sum(case.correct for case in cases)
    sequence_scores = [
        Fraction(case.sequence_match_count, case.sequence_denominator)
        if case.sequence_denominator
        else Fraction(1, 1)
        for case in cases
    ]
    sequence_average = (
        sum(sequence_scores, start=Fraction()) / count if count else Fraction()
    )
    recovered = sum(case.recovered_transient_fault_count for case in cases)
    recoverable = sum(case.verified_transient_fault_count for case in cases)
    invalid_accepted = sum(case.invalid_output_accepted_count for case in cases)
    accepted = sum(case.accepted_output_count for case in cases)
    latency_values = [case.latency_ns for case in cases]
    return ModeMetrics(
        case_correct_count=correct,
        case_count=count,
        task_correctness_rate=float(Fraction(correct, count)) if count else 0.0,
        terminal_success_count=sum(case.terminal_success for case in cases),
        tool_sequence_accuracy=float(sequence_average),
        tool_sequence_case_count=count,
        recovered_transient_fault_count=recovered,
        transient_fault_count=recoverable,
        recovery_rate=(
            float(Fraction(recovered, recoverable)) if recoverable else None
        ),
        invalid_output_accepted_count=invalid_accepted,
        accepted_output_count=accepted,
        invalid_output_rate=(
            float(Fraction(invalid_accepted, accepted)) if accepted else 0.0
        ),
        invalid_output_detected_count=sum(
            case.invalid_output_detected_count for case in cases
        ),
        invalid_output_rejected_count=sum(
            case.invalid_output_rejected_count for case in cases
        ),
        malformed_fault_injected_count=sum(
            case.malformed_fault_injected_count for case in cases
        ),
        unnecessary_call_count=sum(case.unnecessary_call_count for case in cases),
        retry_attempt_count=sum(case.retry_attempt_count for case in cases),
        p50_latency_ms=nearest_rank_percentile(latency_values, 0.50) / 1_000_000,
        p95_latency_ms=nearest_rank_percentile(latency_values, 0.95) / 1_000_000,
        estimated_input_tokens=sum(case.estimated_input_tokens for case in cases),
        estimated_output_tokens=sum(case.estimated_output_tokens for case in cases),
        estimated_cost_usd=sum(
            (case.estimated_cost_usd for case in cases), start=Decimal(0)
        ),
    )


def _lcs_length(left: Sequence[str], right: Sequence[str]) -> int:
    previous = [0] * (len(right) + 1)
    for left_item in left:
        current = [0]
        for index, right_item in enumerate(right, start=1):
            if left_item == right_item:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


__all__ = [
    "SequenceGrade",
    "aggregate_cases",
    "nearest_rank_percentile",
    "tool_sequence_accuracy",
    "tool_sequence_grade",
    "unnecessary_call_count",
]
