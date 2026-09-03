"""Regression-gate exactness and anti-tampering behavior."""

import pytest

from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import (
    AcceptedOutputEvidence,
    AttemptEvidence,
    CaseResult,
    EvaluationReport,
    FaultEvidence,
    ModeResult,
    OrderedTraceEvidence,
)
from agent_reliability_lab.evaluation.runner import (
    compare_modes,
    suite_sha256,
    trace_evidence_digest,
)

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


def _replace_mode_case(
    report: EvaluationReport, mode: str, changed: CaseResult
) -> EvaluationReport:
    result = report.modes[mode]
    cases = (changed, *result.cases[1:])
    changed_mode = ModeResult(mode=mode, cases=cases, metrics=aggregate_cases(cases))
    candidate = report.model_copy(
        update={"modes": {**report.modes, mode: changed_mode}}
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


@pytest.mark.parametrize(
    "attempt",
    [
        AttemptEvidence(
            action_step=99,
            tool_name="search_recent_logs",
            attempt=1,
            status="succeeded",
        ),
        AttemptEvidence(
            action_step=0,
            tool_name="get_deployment",
            attempt=1,
            status="succeeded",
        ),
    ],
)
def test_gate_rejects_attempt_evidence_reassigned_off_frozen_action(
    attempt: AttemptEvidence,
) -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0),),
        fragile_cases=(make_case(mode="fragile", recovered=0, recoverable=0),),
    )
    case = report.modes["resilient"].cases[0]
    tampered = _replace_resilient_case(
        report,
        case.model_copy(update={"attempt_evidence": (attempt,)}),
    )

    result = enforce_gate(tampered)

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_invented_self_consistent_fault_and_retry() -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0),),
        fragile_cases=(make_case(mode="fragile", recovered=0, recoverable=0),),
    )
    case = report.modes["resilient"].cases[0]
    invented_fault = FaultEvidence(
        action_step=0,
        tool_name="search_recent_logs",
        attempt=1,
        kind="timeout",
    )
    attempts = (
        AttemptEvidence(
            action_step=0,
            tool_name="search_recent_logs",
            attempt=1,
            status="failed",
            error_code="tool_timeout",
            transient=True,
        ),
        AttemptEvidence(
            action_step=0,
            tool_name="search_recent_logs",
            attempt=2,
            status="succeeded",
        ),
    )
    changed = case.model_copy(
        update={
            "attempt_evidence": attempts,
            "attempt_count": 2,
            "retry_attempt_count": 1,
            "declared_faults": (invented_fault,),
            "observed_faults": (invented_fault,),
            "verified_transient_fault_count": 1,
            "recovered_transient_fault_count": 1,
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_derives_terminal_success_from_terminal_evidence() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]

    result = enforce_gate(
        _replace_resilient_case(
            report,
            case.model_copy(update={"terminal_success": not case.terminal_success}),
        )
    )

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_derives_approval_reconstruction_from_ordered_evidence() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    fingerprint = case.logical_actions[0].action_fingerprint
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
        if item.event_type == "policy.action" and item.payload["action_step"] == 0:
            trace.extend(
                (
                    OrderedTraceEvidence(
                        sequence=len(trace) + 1,
                        event_type="run.waiting_approval",
                        payload={
                            "tool_name": "search_recent_logs",
                            "step": 0,
                            "action_fingerprint": fingerprint,
                        },
                    ),
                    OrderedTraceEvidence(
                        sequence=len(trace) + 2,
                        event_type="approval.recorded",
                        payload={
                            "allow": True,
                            "action_step": 0,
                            "action_fingerprint": fingerprint,
                        },
                    ),
                    OrderedTraceEvidence(
                        sequence=len(trace) + 3,
                        event_type="run.running",
                        payload={"from_status": "waiting_approval"},
                    ),
                )
            )
    trace_evidence = tuple(trace)
    case = case.model_copy(
        update={
            "trace_evidence": trace_evidence,
            "trace_digest": trace_evidence_digest(trace_evidence),
            "approval_reconstructed": True,
            "pre_pause_write_execution_count": 0,
            "write_execution_count": 1,
        }
    )
    report = _replace_resilient_case(report, case)
    manifest = (
        report.provenance.suite_manifest[0].model_copy(
            update={"approval_supplied": True}
        ),
    )
    report = report.model_copy(
        update={
            "provenance": report.provenance.model_copy(
                update={
                    "suite_manifest": manifest,
                    "suite_hash": suite_sha256(manifest),
                }
            )
        }
    )

    result = enforce_gate(
        _replace_resilient_case(
            report,
            case.model_copy(update={"approval_reconstructed": False}),
        )
    )

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_changed_action_arguments_even_with_refreshed_digest() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    changed_trace = tuple(
        item.model_copy(
            update={"payload": {**item.payload, "arguments": {"limit": 99}}}
        )
        if item.event_type == "policy.action" and item.payload.get("action_step") == 0
        else item
        for item in case.trace_evidence
    )
    changed = case.model_copy(
        update={
            "trace_evidence": changed_trace,
            "trace_digest": trace_evidence_digest(changed_trace),
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_changed_frozen_action_fingerprint() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    changed_actions = (
        case.logical_actions[0].model_copy(update={"action_fingerprint": "a" * 64}),
        *case.logical_actions[1:],
    )

    result = enforce_gate(
        _replace_resilient_case(
            report, case.model_copy(update={"logical_actions": changed_actions})
        )
    )

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_attempt_evidence_after_finish_action() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "run.succeeded":
            trace.append(
                OrderedTraceEvidence(
                    sequence=len(trace) + 1,
                    event_type="tool.attempt.started",
                    payload={
                        "action_step": 0,
                        "tool_name": "search_recent_logs",
                        "attempt": 3,
                    },
                )
            )
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed_trace = tuple(trace)
    changed = case.model_copy(
        update={
            "trace_evidence": changed_trace,
            "trace_digest": trace_evidence_digest(changed_trace),
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_accepted_output_rebound_to_compatible_action() -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0, accepted=2),),
        fragile_cases=(
            make_case(mode="fragile", recovered=0, recoverable=0, accepted=2),
        ),
    )
    case = report.modes["resilient"].cases[0]
    first, second = case.accepted_outputs
    changed = case.model_copy(
        update={
            "accepted_outputs": (
                first.model_copy(update={"action_step": second.action_step}),
                second,
            )
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_accepted_output_reassigned_to_unknown_action() -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0),),
        fragile_cases=(make_case(mode="fragile", recovered=0, recoverable=0),),
    )
    case = report.modes["resilient"].cases[0]
    accepted = case.accepted_outputs[0]
    changed = case.model_copy(
        update={
            "accepted_outputs": (
                AcceptedOutputEvidence(
                    action_step=99,
                    tool_name=accepted.tool_name,
                    output=accepted.output,
                ),
            )
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_cross_mode_expected_projection_divergence() -> None:
    report = make_report()
    fragile = report.modes["fragile"].cases[0]
    changed = fragile.model_copy(
        update={
            "expected_outcome": "different-but-self-consistent",
            "observed_outcome": "different-but-self-consistent",
            "correct": True,
        }
    )

    result = enforce_gate(_replace_mode_case(report, "fragile", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


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
