"""Regression-gate exactness and anti-tampering behavior."""

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import (
    AcceptedOutputEvidence,
    AttemptEvidence,
    CaseResult,
    EvaluationReport,
    FaultEvidence,
    LogicalActionProjection,
    ModeResult,
    OrderedTraceEvidence,
    OutputValidationEvidence,
)
from agent_reliability_lab.evaluation.runner import (
    canonical_action_fingerprint,
    compare_modes,
    run_evaluation,
    suite_sha256,
    trace_evidence_digest,
)

from .conftest import make_case, make_report

SUITE = Path(__file__).parents[2] / "scenarios" / "incident-response"


def _new_evidence(
    case: CaseResult,
    *,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> OrderedTraceEvidence:
    event_id = uuid5(case.trace_id, f"mutation:{sequence}:{event_type}:{payload!r}")
    return OrderedTraceEvidence(
        event_id=event_id,
        trace_id=case.trace_id,
        span_id=event_id,
        parent_span_id=case.trace_id,
        status=(
            "error"
            if event_type
            in {
                "fault.injected",
                "tool.output.validation_failed",
                "tool.attempt.failed",
                "tool.attempt.cancelled",
                "approval.denied",
                "run.failed",
            }
            else "ok"
        ),
        sequence=sequence,
        event_type=event_type,
        payload=payload,
    )


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


def _replace_named_case(
    report: EvaluationReport, mode: str, changed: CaseResult
) -> EvaluationReport:
    result = report.modes[mode]
    cases = tuple(
        changed if case.scenario_id == changed.scenario_id else case
        for case in result.cases
    )
    changed_mode = ModeResult(mode=mode, cases=cases, metrics=aggregate_cases(cases))
    candidate = report.model_copy(
        update={"modes": {**report.modes, mode: changed_mode}}
    )
    return candidate.model_copy(update={"comparison": compare_modes(candidate)})


def _refresh_trace(
    case: CaseResult,
    trace: tuple[OrderedTraceEvidence, ...],
    **updates: object,
) -> CaseResult:
    return case.model_copy(
        update={
            "trace_evidence": trace,
            "trace_digest": trace_evidence_digest(trace),
            **updates,
        }
    )


def _attack_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _coordinated_output_rewrite(
    case: CaseResult,
    *,
    action_step: int,
    output: dict[str, object],
    initial_context: dict[str, object],
) -> CaseResult:
    outputs: dict[str, object] = {}
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        payload = dict(item.payload)
        if item.event_type == "tool.attempt.succeeded":
            step = int(payload["action_step"])
            if step == action_step:
                payload["output"] = output
            outputs[str(step)] = payload["output"]
        elif item.event_type == "run.checkpointed":
            step = int(payload["action_step"])
            accepted = outputs[str(step)]
            payload["output_digest"] = _attack_digest(accepted)
            payload["context_digest"] = _attack_digest(
                {**initial_context, "tool_results": dict(outputs)}
            )
        trace.append(item.model_copy(update={"payload": payload}))
    accepted_outputs = tuple(
        item.model_copy(update={"output": output})
        if item.action_step == action_step
        else item
        for item in case.accepted_outputs
    )
    return _refresh_trace(
        case,
        tuple(trace),
        accepted_outputs=accepted_outputs,
    )


def _replace_manifest_actions(
    report: EvaluationReport,
    *,
    scenario_id: str,
    logical_actions: tuple[LogicalActionProjection, ...],
) -> EvaluationReport:
    manifest = tuple(
        entry.model_copy(update={"logical_actions": logical_actions})
        if entry.scenario_id == scenario_id
        else entry
        for entry in report.provenance.suite_manifest
    )
    return report.model_copy(
        update={
            "provenance": report.provenance.model_copy(
                update={
                    "suite_manifest": manifest,
                    "suite_hash": suite_sha256(manifest),
                }
            )
        }
    )


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
                    _new_evidence(
                        case,
                        sequence=len(trace) + 1,
                        event_type="run.waiting_approval",
                        payload={
                            "tool_name": "search_recent_logs",
                            "step": 0,
                            "action_fingerprint": fingerprint,
                        },
                    ),
                    _new_evidence(
                        case,
                        sequence=len(trace) + 2,
                        event_type="approval.recorded",
                        payload={
                            "allow": True,
                            "action_step": 0,
                            "action_fingerprint": fingerprint,
                        },
                    ),
                    _new_evidence(
                        case,
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
                _new_evidence(
                    case,
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


def test_gate_rejects_faultless_failed_attempt_and_retry_with_refreshed_trace() -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0),),
        fragile_cases=(make_case(mode="fragile", recovered=0, recoverable=0),),
    )
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "tool.attempt.succeeded":
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="tool.attempt.failed",
                    payload={
                        "action_step": 0,
                        "tool_name": "search_recent_logs",
                        "attempt": 1,
                        "code": "tool_timeout",
                        "transient": True,
                    },
                )
            )
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="tool.attempt.started",
                    payload={
                        "action_step": 0,
                        "tool_name": "search_recent_logs",
                        "attempt": 2,
                    },
                )
            )
            trace.append(
                item.model_copy(
                    update={
                        "sequence": len(trace) + 1,
                        "payload": {**item.payload, "attempt": 2},
                    }
                )
            )
            continue
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
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
    changed = _refresh_trace(
        case,
        tuple(trace),
        attempt_evidence=attempts,
        attempt_count=2,
        retry_attempt_count=1,
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_finish_followed_by_coordinated_run_failed() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    trace = tuple(
        _new_evidence(
            case,
            sequence=item.sequence,
            event_type="run.failed",
            payload={"code": case.expected_outcome},
        )
        if item.event_type == "run.succeeded"
        else item
        for item in case.trace_evidence
    )
    changed = _refresh_trace(
        case,
        trace,
        final_status="failed",
        final_result={"code": case.expected_outcome},
        terminal_success=False,
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_general_run_event_after_frozen_finish_action() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "run.succeeded":
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="run.running",
                    payload={"from_status": "running"},
                )
            )
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_removal_of_required_approval_cycle() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    removed = {
        "run.waiting_approval",
        "approval.recorded",
    }
    trace = tuple(
        item.model_copy(update={"sequence": index})
        for index, item in enumerate(
            (
                item
                for item in case.trace_evidence
                if item.event_type not in removed
                and not (
                    item.event_type == "run.running"
                    and item.payload.get("from_status") == "waiting_approval"
                )
            ),
            start=1,
        )
    )
    changed = _refresh_trace(case, trace, approval_reconstructed=False)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.parametrize(
    ("code", "transient", "with_validation"),
    [
        ("tool_execution_failed", False, False),
        ("invalid_output", False, True),
    ],
)
def test_gate_rejects_retry_after_permanent_or_validation_failure(
    code: str, transient: bool, with_validation: bool
) -> None:
    report = make_report(
        resilient_cases=(make_case(recovered=0, recoverable=0),),
        fragile_cases=(make_case(mode="fragile", recovered=0, recoverable=0),),
    )
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "tool.attempt.succeeded":
            if with_validation:
                trace.append(
                    _new_evidence(
                        case,
                        sequence=len(trace) + 1,
                        event_type="tool.output.validation_failed",
                        payload={
                            "action_step": 0,
                            "tool_name": "search_recent_logs",
                            "attempt": 1,
                            "code": "invalid_output",
                            "source": "output_model",
                        },
                    )
                )
            trace.extend(
                (
                    _new_evidence(
                        case,
                        sequence=len(trace) + 1,
                        event_type="tool.attempt.failed",
                        payload={
                            "action_step": 0,
                            "tool_name": "search_recent_logs",
                            "attempt": 1,
                            "code": code,
                            "transient": transient,
                        },
                    ),
                    _new_evidence(
                        case,
                        sequence=len(trace) + 2,
                        event_type="tool.attempt.started",
                        payload={
                            "action_step": 0,
                            "tool_name": "search_recent_logs",
                            "attempt": 2,
                        },
                    ),
                    item.model_copy(
                        update={
                            "sequence": len(trace) + 3,
                            "payload": {**item.payload, "attempt": 2},
                        }
                    ),
                )
            )
            continue
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    attempts = (
        AttemptEvidence(
            action_step=0,
            tool_name="search_recent_logs",
            attempt=1,
            status="failed",
            error_code=code,
            transient=transient,
        ),
        AttemptEvidence(
            action_step=0,
            tool_name="search_recent_logs",
            attempt=2,
            status="succeeded",
        ),
    )
    validation = (
        (
            OutputValidationEvidence(
                action_step=0,
                tool_name="search_recent_logs",
                attempt=1,
            ),
        )
        if with_validation
        else ()
    )
    changed = _refresh_trace(
        case,
        tuple(trace),
        attempt_evidence=attempts,
        attempt_count=2,
        retry_attempt_count=1,
        output_validation_failures=validation,
        invalid_output_detected_count=len(validation),
        invalid_output_rejected_count=len(validation),
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("run.running", {"from_status": "running"}),
        (
            "run.waiting_approval",
            {
                "tool_name": "search_recent_logs",
                "step": 0,
                "action_fingerprint": "a" * 64,
            },
        ),
        (
            "approval.recorded",
            {
                "actor": "evaluation-reviewer",
                "allow": True,
                "action_step": 0,
                "action_fingerprint": "a" * 64,
            },
        ),
        (
            "fault.injected",
            {
                "action_step": 0,
                "tool_name": "search_recent_logs",
                "attempt": 2,
                "kind": "timeout",
            },
        ),
        (
            "tool.output.validation_failed",
            {
                "action_step": 0,
                "tool_name": "search_recent_logs",
                "attempt": 2,
                "code": "invalid_output",
                "source": "output_model",
            },
        ),
    ],
)
def test_gate_rejects_every_state_approval_fault_event_after_finish(
    event_type: str, payload: dict[str, object]
) -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "run.succeeded":
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type=event_type,
                    payload=payload,
                )
            )
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["duplicate", "out_of_order", "deny"])
async def test_gate_rejects_invalid_approval_cycles(mutation: str) -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    original = list(case.trace_evidence)
    wait_index = next(
        index
        for index, item in enumerate(original)
        if item.event_type == "run.waiting_approval"
    )
    approval_index = next(
        index
        for index, item in enumerate(original)
        if item.event_type == "approval.recorded"
    )
    updates: dict[str, object] = {}
    if mutation == "duplicate":
        original.insert(approval_index + 1, original[approval_index])
    elif mutation == "out_of_order":
        original[wait_index], original[approval_index] = (
            original[approval_index],
            original[wait_index],
        )
        updates["approval_reconstructed"] = False
    else:
        original[approval_index] = original[approval_index].model_copy(
            update={"payload": {**original[approval_index].payload, "allow": False}}
        )
        updates["approval_reconstructed"] = False
    trace = tuple(
        item.model_copy(update={"sequence": index})
        for index, item in enumerate(original, start=1)
    )
    changed = _refresh_trace(case, trace, **updates)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("actor", "other-reviewer"), ("action_fingerprint", "b" * 64)],
)
async def test_gate_rejects_mismatched_approval_attribution(
    field: str, value: object
) -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    trace = tuple(
        item.model_copy(update={"payload": {**item.payload, field: value}})
        if item.event_type == "approval.recorded"
        else item
        for item in case.trace_evidence
    )
    updates = {"approval_reconstructed": False} if field == "action_fingerprint" else {}
    changed = _refresh_trace(case, trace, **updates)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_policy_cursor_rollback_and_replay() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    replay = next(
        item
        for item in case.trace_evidence
        if item.event_type == "policy.action" and item.payload["action_step"] == 0
    )
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "policy.action" and item.payload.get("type") == "finish":
            trace.append(replay.model_copy(update={"sequence": len(trace) + 1}))
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_overlapping_retry_before_prior_attempt_terminal() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "tool.attempt.succeeded":
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="tool.attempt.started",
                    payload={**item.payload, "attempt": 2},
                )
            )
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_coordinated_wait_and_allow_fingerprint_rewrite() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    trace = tuple(
        item.model_copy(
            update={"payload": {**item.payload, "action_fingerprint": "c" * 64}}
        )
        if item.event_type in {"run.waiting_approval", "approval.recorded"}
        else item
        for item in case.trace_evidence
    )
    changed = _refresh_trace(case, trace)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_approval_cycle_moved_after_write() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    cycle = [
        item
        for item in case.trace_evidence
        if item.event_type in {"run.waiting_approval", "approval.recorded"}
        or (
            item.event_type == "run.running"
            and item.payload.get("from_status") == "waiting_approval"
        )
    ]
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item in cycle:
            continue
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
        if (
            item.event_type == "tool.attempt.succeeded"
            and item.payload.get("tool_name") == "prepare_rollback"
        ):
            trace.extend(
                event.model_copy(update={"sequence": len(trace) + offset})
                for offset, event in enumerate(cycle, start=1)
            )
    changed = _refresh_trace(case, tuple(trace), approval_reconstructed=False)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_deny_before_allow_conflict_then_write() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        if item.event_type == "approval.recorded":
            trace.append(
                item.model_copy(
                    update={
                        "sequence": len(trace) + 1,
                        "payload": {**item.payload, "allow": False},
                    }
                )
            )
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_run_success_payload_rewritten_away_from_frozen_finish() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    result_payload = {
        "outcome": "other",
        "summary": "coordinated rewrite",
        "evidence_refs": [],
    }
    trace = tuple(
        item.model_copy(update={"payload": result_payload})
        if item.event_type == "run.succeeded"
        else item
        for item in case.trace_evidence
    )
    changed = _refresh_trace(
        case,
        trace,
        final_result=result_payload,
        observed_outcome="other",
        correct=False,
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_schema_valid_accepted_output_not_bound_to_trace() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    accepted = case.accepted_outputs[0]
    changed = case.model_copy(
        update={
            "accepted_outputs": (
                accepted.model_copy(
                    update={
                        "output": {
                            "entries": [
                                {"level": "INFO", "message": "fabricated but valid"}
                            ]
                        }
                    }
                ),
            )
        }
    )

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_case_trace_id_rewrite() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]

    result = enforce_gate(
        _replace_resilient_case(
            report,
            case.model_copy(update={"trace_id": UUID(int=999)}),
        )
    )

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_run_or_trace_identity_reused_between_cases() -> None:
    report = await run_evaluation(SUITE)
    cases = report.modes["resilient"].cases
    first, second = cases[:2]
    changed = second.model_copy(
        update={"run_id": first.run_id, "trace_id": first.trace_id}
    )

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.parametrize("mutation", ["fault_span", "success_status", "event_id"])
def test_gate_rejects_trace_metadata_splice(mutation: str) -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    first_event_id = case.trace_evidence[0].event_id
    trace = []
    for item in case.trace_evidence:
        updates: dict[str, object] = {}
        if mutation == "fault_span" and item.event_type == "fault.injected":
            updates["span_id"] = uuid5(case.trace_id, "spliced-span")
        elif (
            mutation == "success_status" and item.event_type == "tool.attempt.succeeded"
        ):
            updates["status"] = "error"
        elif mutation == "event_id" and item.event_type == "run.succeeded":
            updates["event_id"] = first_event_id
        trace.append(item.model_copy(update=updates))
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_missing_success_checkpoint() -> None:
    report = make_report()
    case = report.modes["resilient"].cases[0]
    checkpoint = next(
        item for item in case.trace_evidence if item.event_type == "run.checkpointed"
    )
    without = tuple(item for item in case.trace_evidence if item is not checkpoint)
    trace = tuple(
        item.model_copy(update={"sequence": index})
        for index, item in enumerate(without, start=1)
    )
    changed = _refresh_trace(case, trace)

    result = enforce_gate(_replace_resilient_case(report, changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


def test_gate_rejects_faultless_cross_mode_semantic_divergence() -> None:
    resilient = make_case(recovered=0, recoverable=0)
    fragile = make_case(mode="fragile", recovered=0, recoverable=0)
    report = make_report(resilient_cases=(resilient,), fragile_cases=(fragile,))
    changed = fragile.model_copy(update={"store_run_count": 2})

    result = enforce_gate(_replace_mode_case(report, "fragile", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_coordinated_success_after_preflight_failure() -> None:
    report = await run_evaluation(SUITE)
    output = {"entries": [{"level": "ERROR", "message": "fabricated after preflight"}]}
    changed_report = report
    for mode in ("fragile", "resilient"):
        case = next(
            item
            for item in report.modes[mode].cases
            if item.scenario_id == "permanent-invalid-input"
        )
        forged_span = uuid5(case.trace_id, "post-preflight-attempt")
        trace: list[OrderedTraceEvidence] = []
        for item in case.trace_evidence:
            trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
            if item.event_type == "tool.preflight.failed":
                started = _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="tool.attempt.started",
                    payload={
                        "action_step": 0,
                        "tool_name": "search_recent_logs",
                        "attempt": 1,
                    },
                ).model_copy(update={"span_id": forged_span})
                succeeded = _new_evidence(
                    case,
                    sequence=len(trace) + 2,
                    event_type="tool.attempt.succeeded",
                    payload={
                        "action_step": 0,
                        "tool_name": "search_recent_logs",
                        "attempt": 1,
                        "output": output,
                    },
                ).model_copy(update={"span_id": forged_span})
                checkpoint = _new_evidence(
                    case,
                    sequence=len(trace) + 3,
                    event_type="run.checkpointed",
                    payload={
                        "action_step": 0,
                        "attempt": 1,
                        "current_step": 1,
                        "cached": False,
                        "output_digest": _attack_digest(output),
                        "context_digest": _attack_digest(
                            {
                                "incident": "invalid-query",
                                "tool_results": {"0": output},
                            }
                        ),
                    },
                )
                trace.extend((started, succeeded, checkpoint))
        changed = _refresh_trace(
            case,
            tuple(trace),
            attempt_evidence=(
                AttemptEvidence(
                    action_step=0,
                    tool_name="search_recent_logs",
                    attempt=1,
                    status="succeeded",
                ),
            ),
            attempt_count=1,
            accepted_outputs=(
                AcceptedOutputEvidence(
                    action_step=0,
                    tool_name="search_recent_logs",
                    output=output,
                ),
            ),
            accepted_output_count=1,
        )
        changed_report = _replace_named_case(changed_report, mode, changed)

    result = enforce_gate(changed_report)

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_every_relevant_event_family_after_preflight_failure() -> (
    None
):
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "permanent-invalid-input"
    )
    attacks = (
        ("policy.action", {**case.logical_actions[1].action_payload, "action_step": 1}),
        (
            "tool.attempt.started",
            {"action_step": 0, "tool_name": "search_recent_logs", "attempt": 1},
        ),
        (
            "fault.injected",
            {
                "action_step": 0,
                "tool_name": "search_recent_logs",
                "attempt": 1,
                "kind": "timeout",
            },
        ),
        (
            "tool.output.validation_failed",
            {
                "action_step": 0,
                "tool_name": "search_recent_logs",
                "attempt": 1,
                "code": "invalid_output",
                "source": "output_model",
            },
        ),
        (
            "run.checkpointed",
            {
                "action_step": 0,
                "attempt": 1,
                "current_step": 1,
                "cached": False,
                "output_digest": "a" * 64,
                "context_digest": "b" * 64,
            },
        ),
        (
            "tool.attempt.succeeded",
            {
                "action_step": 0,
                "tool_name": "search_recent_logs",
                "attempt": 1,
                "output": {"entries": []},
            },
        ),
        (
            "run.succeeded",
            {"outcome": "diagnosed", "summary": "forged", "evidence_refs": []},
        ),
        ("run.running", {"from_status": "running"}),
    )
    for event_type, payload in attacks:
        trace: list[OrderedTraceEvidence] = []
        for item in case.trace_evidence:
            trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
            if item.event_type == "tool.preflight.failed":
                trace.append(
                    _new_evidence(
                        case,
                        sequence=len(trace) + 1,
                        event_type=event_type,
                        payload=payload,
                    )
                )
        changed = _refresh_trace(case, tuple(trace))

        result = enforce_gate(_replace_named_case(report, "resilient", changed))

        assert result.comparable is False, event_type
        assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_allow_followed_by_attributed_denial() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
        if item.event_type == "approval.recorded":
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="approval.denied",
                    payload={
                        "actor": item.payload["actor"],
                        "reason": "coordinated denial",
                        "action_step": item.payload["action_step"],
                        "action_fingerprint": item.payload["action_fingerprint"],
                    },
                )
            )
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_denial_inserted_after_approved_write_checkpoint() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "approval-reconstruction"
    )
    approval = next(
        item for item in case.trace_evidence if item.event_type == "approval.recorded"
    )
    trace: list[OrderedTraceEvidence] = []
    for item in case.trace_evidence:
        trace.append(item.model_copy(update={"sequence": len(trace) + 1}))
        if (
            item.event_type == "run.checkpointed"
            and item.payload.get("action_step") == 1
        ):
            trace.append(
                _new_evidence(
                    case,
                    sequence=len(trace) + 1,
                    event_type="approval.denied",
                    payload={
                        "actor": approval.payload["actor"],
                        "reason": "too late",
                        "action_step": approval.payload["action_step"],
                        "action_fingerprint": approval.payload["action_fingerprint"],
                    },
                )
            )
    changed = _refresh_trace(case, tuple(trace))

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_arbitrary_checkpoint_context_digest() -> None:
    report = await run_evaluation(SUITE)
    case = next(
        item
        for item in report.modes["resilient"].cases
        if item.scenario_id == "normal-success"
    )
    trace = tuple(
        item.model_copy(
            update={"payload": {**item.payload, "context_digest": "d" * 64}}
        )
        if item.event_type == "run.checkpointed"
        else item
        for item in case.trace_evidence
    )
    changed = _refresh_trace(case, trace)

    result = enforce_gate(_replace_named_case(report, "resilient", changed))

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_dual_mode_schema_valid_output_rewrite() -> None:
    report = await run_evaluation(SUITE)
    fabricated = {
        "entries": [{"level": "INFO", "message": "fabricated but schema-valid"}]
    }
    fabricated_digest = _attack_digest(fabricated)
    changed_report = report
    for mode in ("fragile", "resilient"):
        case = next(
            item
            for item in report.modes[mode].cases
            if item.scenario_id == "normal-success"
        )
        changed = _coordinated_output_rewrite(
            case,
            action_step=1,
            output=fabricated,
            initial_context={"incident": "checkout-latency"},
        )
        changed_actions = tuple(
            action.model_copy(update={"expected_output_digest": fabricated_digest})
            if action.action_step == 1
            else action
            for action in case.logical_actions
        )
        changed = changed.model_copy(update={"logical_actions": changed_actions})
        changed_report = _replace_named_case(changed_report, mode, changed)
    manifest = tuple(
        entry.model_copy(
            update={
                "logical_actions": tuple(
                    action.model_copy(
                        update={"expected_output_digest": fabricated_digest}
                    )
                    if action.action_step == 1
                    else action
                    for action in entry.logical_actions
                )
            }
        )
        if entry.scenario_id == "normal-success"
        else entry
        for entry in report.provenance.suite_manifest
    )
    changed_report = changed_report.model_copy(
        update={
            "provenance": changed_report.provenance.model_copy(
                update={
                    "suite_manifest": manifest,
                    "suite_hash": suite_sha256(manifest),
                }
            )
        }
    )

    result = enforce_gate(changed_report)

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_coordinated_frozen_initial_context_rewrite() -> None:
    report = await run_evaluation(SUITE)
    fabricated_context = {"incident": "rewritten-context"}
    changed_report = report
    for mode in ("fragile", "resilient"):
        case = next(
            item
            for item in report.modes[mode].cases
            if item.scenario_id == "normal-success"
        )
        changed = _coordinated_output_rewrite(
            case,
            action_step=99,
            output={},
            initial_context=fabricated_context,
        )
        changed_report = _replace_named_case(changed_report, mode, changed)
    manifest = tuple(
        entry.model_copy(update={"initial_context": fabricated_context})
        if entry.scenario_id == "normal-success"
        else entry
        for entry in report.provenance.suite_manifest
    )
    changed_report = changed_report.model_copy(
        update={
            "provenance": changed_report.provenance.model_copy(
                update={
                    "suite_manifest": manifest,
                    "suite_hash": suite_sha256(manifest),
                }
            )
        }
    )

    result = enforce_gate(changed_report)

    assert result.comparable is False
    assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
async def test_gate_rejects_full_approval_write_target_rewrite_with_or_without_baseline() -> (
    None
):
    baseline = await run_evaluation(SUITE)
    fabricated_id = "deploy-attacker-controlled"
    fabricated_output = {"deployment_id": fabricated_id, "prepared": True}
    fabricated_digest = _attack_digest(fabricated_output)
    action_payload = {
        "type": "call_tool",
        "tool_name": "prepare_rollback",
        "arguments": {"deployment_id": fabricated_id},
        "idempotency_key": "approval-reconstruction-v1",
    }
    action_fingerprint = canonical_action_fingerprint(action_payload)
    changed_report = baseline
    changed_actions: tuple[LogicalActionProjection, ...] | None = None
    for mode in ("fragile", "resilient"):
        case = next(
            item
            for item in baseline.modes[mode].cases
            if item.scenario_id == "approval-reconstruction"
        )
        changed = _coordinated_output_rewrite(
            case,
            action_step=1,
            output=fabricated_output,
            initial_context={"incident": "rollback-candidate"},
        )
        changed_actions = tuple(
            action.model_copy(
                update={
                    "action_payload": action_payload,
                    "action_fingerprint": action_fingerprint,
                    "expected_output_digest": fabricated_digest,
                }
            )
            if action.action_step == 1
            else action
            for action in case.logical_actions
        )
        trace = tuple(
            item.model_copy(
                update={
                    "payload": (
                        {**action_payload, "action_step": 1}
                        if item.event_type == "policy.action"
                        and item.payload.get("action_step") == 1
                        else {
                            **item.payload,
                            "action_fingerprint": action_fingerprint,
                        }
                        if item.event_type
                        in {"run.waiting_approval", "approval.recorded"}
                        else item.payload
                    )
                }
            )
            for item in changed.trace_evidence
        )
        changed = _refresh_trace(
            changed,
            trace,
            logical_actions=changed_actions,
        )
        changed_report = _replace_named_case(changed_report, mode, changed)
    assert changed_actions is not None
    changed_report = _replace_manifest_actions(
        changed_report,
        scenario_id="approval-reconstruction",
        logical_actions=changed_actions,
    )

    without_baseline = enforce_gate(changed_report)
    with_baseline = enforce_gate(changed_report, baseline=baseline)

    for result in (without_baseline, with_baseline):
        assert result.comparable is False
        assert "case_evidence_mismatch" in result.infrastructure_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["other_tool_args", "finish_payload"])
async def test_gate_rejects_other_frozen_action_payload_rewrites(
    mutation: str,
) -> None:
    baseline = await run_evaluation(SUITE)
    scenario_id = (
        "permanent-invalid-input" if mutation == "other_tool_args" else "normal-success"
    )
    action_step = 0 if mutation == "other_tool_args" else 3
    if mutation == "other_tool_args":
        action_payload = {
            "type": "call_tool",
            "tool_name": "search_recent_logs",
            "arguments": {"different_invalid_filter": True},
            "idempotency_key": None,
        }
    else:
        action_payload = {
            "type": "finish",
            "summary": "Coordinated but forged final summary.",
            "evidence_refs": ["health", "logs", "deployment"],
            "outcome": "diagnosed",
        }
    fingerprint = canonical_action_fingerprint(action_payload)
    changed_report = baseline
    changed_actions: tuple[LogicalActionProjection, ...] | None = None
    for mode in ("fragile", "resilient"):
        case = next(
            item
            for item in baseline.modes[mode].cases
            if item.scenario_id == scenario_id
        )
        changed_actions = tuple(
            action.model_copy(
                update={
                    "action_payload": action_payload,
                    "action_fingerprint": fingerprint,
                }
            )
            if action.action_step == action_step
            else action
            for action in case.logical_actions
        )
        trace = tuple(
            item.model_copy(
                update={
                    "payload": (
                        {**action_payload, "action_step": action_step}
                        if item.event_type == "policy.action"
                        and item.payload.get("action_step") == action_step
                        else {
                            **item.payload,
                            "action_fingerprint": fingerprint,
                        }
                        if mutation == "other_tool_args"
                        and item.event_type == "tool.preflight.failed"
                        else {
                            **item.payload,
                            "summary": action_payload["summary"],
                        }
                        if mutation == "finish_payload"
                        and item.event_type == "run.succeeded"
                        else item.payload
                    )
                }
            )
            for item in case.trace_evidence
        )
        updates: dict[str, object] = {"logical_actions": changed_actions}
        if mutation == "finish_payload":
            updates["final_result"] = {
                **case.final_result,
                "summary": action_payload["summary"],
            }
        changed = _refresh_trace(case, trace, **updates)
        changed_report = _replace_named_case(changed_report, mode, changed)
    assert changed_actions is not None
    changed_report = _replace_manifest_actions(
        changed_report,
        scenario_id=scenario_id,
        logical_actions=changed_actions,
    )

    for result in (
        enforce_gate(changed_report),
        enforce_gate(changed_report, baseline=baseline),
    ):
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
        make_case(
            scenario_id=f"case-{index}",
            correct=index < 19,
            recovered=1 if index < 19 else 0,
            accepted=1 if index < 19 else 0,
            fault_step=0,
            terminal_success=index < 19,
        )
        for index in range(20)
    )
    current = make_report(resilient_cases=at_boundary)

    assert enforce_gate(current, baseline=baseline).passed is True

    beyond = tuple(
        make_case(
            scenario_id=f"case-{index}",
            correct=index < 18,
            recovered=1 if index < 18 else 0,
            accepted=1 if index < 18 else 0,
            fault_step=0,
            terminal_success=index < 18,
        )
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
