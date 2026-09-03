"""Exact deterministic grader behavior."""

from decimal import Decimal

import pytest

from agent_reliability_lab.evaluation.graders import (
    aggregate_cases,
    nearest_rank_percentile,
    tool_sequence_grade,
    unnecessary_call_count,
)

from .conftest import make_case


@pytest.mark.parametrize(
    ("expected", "actual", "matches", "denominator", "score"),
    [
        (
            ("health", "logs", "deployment"),
            ("health", "deployment", "logs", "logs"),
            2,
            4,
            0.5,
        ),
        ((), (), 0, 0, 1.0),
        (("logs", "logs"), ("logs",), 1, 2, 0.5),
        (("a", "b", "c"), ("c", "b", "a"), 1, 3, 1 / 3),
    ],
)
def test_tool_sequence_grade_uses_lcs_and_literal_denominator(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
    matches: int,
    denominator: int,
    score: float,
) -> None:
    """A set-based or edit-distance grader would mis-score order and duplicates."""
    grade = tool_sequence_grade(expected, actual)

    assert grade.match_count == matches
    assert grade.denominator == denominator
    assert grade.accuracy == pytest.approx(score)


def test_unnecessary_calls_count_multiset_excess() -> None:
    """Retries and duplicates must not disappear in set subtraction."""
    assert unnecessary_call_count(("health", "logs"), ("health", "logs", "logs")) == 1
    assert unnecessary_call_count((), ("health", "health")) == 2


@pytest.mark.parametrize(
    ("values", "percentile", "expected"),
    [
        ((7,), 0.50, 7),
        ((1, 2), 0.50, 1),
        ((1, 2), 0.95, 2),
        ((9, 1, 5, 3), 0.50, 3),
        ((9, 1, 5, 3), 0.95, 9),
    ],
)
def test_nearest_rank_percentile_has_documented_edge_behavior(
    values: tuple[int, ...], percentile: float, expected: int
) -> None:
    assert nearest_rank_percentile(values, percentile) == expected


def test_aggregate_preserves_every_metric_numerator_and_denominator() -> None:
    cases = (
        make_case(recovered=1, recoverable=1, accepted=2),
        make_case(
            scenario_id="case-2",
            correct=False,
            terminal_success=False,
            recovered=0,
            recoverable=1,
            accepted=1,
            invalid_accepted=0,
        ),
    )

    metrics = aggregate_cases(cases)

    assert (metrics.case_correct_count, metrics.case_count) == (1, 2)
    assert metrics.task_correctness_rate == 0.5
    assert metrics.terminal_success_count == 1
    assert (metrics.recovered_transient_fault_count, metrics.transient_fault_count) == (
        1,
        2,
    )
    assert metrics.recovery_rate == 0.5
    assert (metrics.invalid_output_accepted_count, metrics.accepted_output_count) == (
        0,
        3,
    )
    assert metrics.invalid_output_rate == 0.0
    assert metrics.estimated_input_tokens == 0
    assert metrics.estimated_output_tokens == 0
    assert metrics.estimated_cost_usd == Decimal(0)


def test_zero_recovery_denominator_is_null_not_perfect() -> None:
    metrics = aggregate_cases((make_case(recovered=0, recoverable=0),))
    assert metrics.transient_fault_count == 0
    assert metrics.recovery_rate is None
