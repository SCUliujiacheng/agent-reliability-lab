"""Shared exact report fixtures for evaluation tests."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

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
    LogicalActionProjection,
    ModeResult,
    OrderedTraceEvidence,
    OutputValidationEvidence,
    SuiteManifestEntry,
)
from agent_reliability_lab.evaluation.runner import (
    canonical_action_fingerprint,
    compare_modes,
    suite_sha256,
    trace_evidence_digest,
)
from agent_reliability_lab.tools.incident import deterministic_incident_output


def _fixture_digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    fault_step: int | None = None,
) -> CaseResult:
    if fault_step is None:
        fault_step = max(0, accepted - int(bool(recovered)))
    call_count = fault_step + 1 if recoverable else max(1, accepted)
    declared = (
        (
            FaultEvidence(
                action_step=fault_step,
                tool_name="search_recent_logs",
                attempt=1,
                kind="timeout",
            ),
        )
        if recoverable
        else ()
    )
    effective_recovered = recovered if correct else 0
    attempts: list[AttemptEvidence] = []
    accepted_steps: list[int] = []
    for step in range(call_count):
        if recoverable and step == fault_step:
            continue
        attempts.append(
            AttemptEvidence(
                action_step=step,
                tool_name="search_recent_logs",
                attempt=1,
                status="succeeded",
            )
        )
        if len(accepted_steps) < accepted:
            accepted_steps.append(step)
    if recoverable:
        attempts.append(
            AttemptEvidence(
                action_step=fault_step,
                tool_name="search_recent_logs",
                attempt=1,
                status="failed",
                error_code="tool_timeout",
                transient=True,
            )
        )
        if recovered:
            attempts.append(
                AttemptEvidence(
                    action_step=fault_step,
                    tool_name="search_recent_logs",
                    attempt=2,
                    status="succeeded",
                )
            )
            accepted_steps.append(fault_step)
    accepted_outputs = tuple(
        AcceptedOutputEvidence(
            action_step=step,
            tool_name="search_recent_logs",
            output=(
                {}
                if index < invalid_accepted
                else {
                    "entries": [
                        {
                            "level": "ERROR",
                            "message": "checkout timeout contacting ledger",
                        }
                    ]
                }
            ),
        )
        for index, step in enumerate(accepted_steps[:accepted])
    )
    call_payloads = tuple(
        {
            "type": "call_tool",
            "tool_name": "search_recent_logs",
            "arguments": {},
            "idempotency_key": None,
        }
        for _ in range(call_count)
    )
    final_outcome = "resolved" if correct else "tool_timeout"
    finish_payload = {
        "type": "finish",
        "summary": "fixture conclusion",
        "evidence_refs": [],
        "outcome": final_outcome if terminal_success else "resolved",
    }
    action_payloads = (*call_payloads, finish_payload)
    logical_actions = tuple(
        LogicalActionProjection(
            action_step=step,
            kind=payload["type"],
            tool_name=(
                str(payload["tool_name"]) if payload["type"] == "call_tool" else None
            ),
            action_payload=payload,
            action_fingerprint=canonical_action_fingerprint(payload),
            expected_output_digest=(
                _fixture_digest(expected_output)
                if payload["type"] == "call_tool"
                and (
                    expected_output := deterministic_incident_output(
                        str(payload["tool_name"]), payload["arguments"]
                    )
                )
                is not None
                else None
            ),
        )
        for step, payload in enumerate(action_payloads)
    )
    trace_items: list[OrderedTraceEvidence] = []
    run_id = uuid5(NAMESPACE_URL, f"fixture-run:{scenario_id}:{mode}")
    trace_id = uuid5(NAMESPACE_URL, f"fixture-trace:{scenario_id}:{mode}")
    sequence = 1

    def record(
        event_type: str,
        payload: dict[str, object],
        *,
        span_id: UUID | None = None,
        status: str = "ok",
    ) -> None:
        nonlocal sequence
        event_id = uuid5(trace_id, f"event:{sequence}")
        trace_items.append(
            OrderedTraceEvidence(
                event_id=event_id,
                trace_id=trace_id,
                span_id=span_id or event_id,
                parent_span_id=trace_id,
                status=status,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
            )
        )
        sequence += 1

    record("run.running", {"from_status": "queued"})
    context_outputs: dict[str, object] = {}
    for step, payload in enumerate(call_payloads):
        record("policy.action", {**payload, "action_step": step})
        attempt_span = uuid5(trace_id, f"attempt:{step}:1")
        record(
            "tool.attempt.started",
            {"action_step": step, "tool_name": "search_recent_logs", "attempt": 1},
            span_id=attempt_span,
        )
        if recoverable and step == fault_step:
            record(
                "fault.injected",
                {
                    "action_step": step,
                    "tool_name": "search_recent_logs",
                    "attempt": 1,
                    "kind": "timeout",
                },
                span_id=attempt_span,
                status="error",
            )
            record(
                "tool.attempt.failed",
                {
                    "action_step": step,
                    "tool_name": "search_recent_logs",
                    "attempt": 1,
                    "code": "tool_timeout",
                    "transient": True,
                },
                span_id=attempt_span,
                status="error",
            )
            if recovered:
                retry_span = uuid5(trace_id, f"attempt:{step}:2")
                record(
                    "tool.attempt.started",
                    {
                        "action_step": step,
                        "tool_name": "search_recent_logs",
                        "attempt": 2,
                    },
                    span_id=retry_span,
                )
                output = next(
                    item.output for item in accepted_outputs if item.action_step == step
                )
                context_outputs[str(step)] = output
                record(
                    "tool.attempt.succeeded",
                    {
                        "action_step": step,
                        "tool_name": "search_recent_logs",
                        "attempt": 2,
                        "output": output,
                    },
                    span_id=retry_span,
                )
                record(
                    "run.checkpointed",
                    {
                        "action_step": step,
                        "attempt": 2,
                        "current_step": step + 1,
                        "cached": False,
                        "output_digest": _fixture_digest(output),
                        "context_digest": _fixture_digest(
                            {"tool_results": dict(context_outputs)}
                        ),
                    },
                )
        else:
            output = next(
                item.output for item in accepted_outputs if item.action_step == step
            )
            record(
                "tool.attempt.succeeded",
                {
                    "action_step": step,
                    "tool_name": "search_recent_logs",
                    "attempt": 1,
                    "output": output,
                },
                span_id=attempt_span,
            )
            context_outputs[str(step)] = output
            record(
                "run.checkpointed",
                {
                    "action_step": step,
                    "attempt": 1,
                    "current_step": step + 1,
                    "cached": False,
                    "output_digest": _fixture_digest(output),
                    "context_digest": _fixture_digest(
                        {"tool_results": dict(context_outputs)}
                    ),
                },
            )
    final_status = "succeeded" if terminal_success else "failed"
    if terminal_success:
        finish_step = len(call_payloads)
        record("policy.action", {**finish_payload, "action_step": finish_step})
        final_result = {
            "outcome": final_outcome,
            "summary": "fixture conclusion",
            "evidence_refs": [],
        }
        record("run.succeeded", final_result)
    else:
        final_result = {"code": final_outcome}
        record("run.failed", final_result, status="error")
    trace_evidence = tuple(trace_items)
    return CaseResult(
        scenario_id=scenario_id,
        scenario_version=1,
        scenario_path=f"{scenario_id}.yaml",
        scenario_sha256="1" * 64,
        mode=mode,
        run_id=run_id,
        trace_id=trace_id,
        trace_digest=trace_evidence_digest(trace_evidence),
        trace_evidence=trace_evidence,
        logical_actions=logical_actions,
        final_status=final_status,
        final_result=final_result,
        expected_outcome="resolved",
        observed_outcome="resolved" if correct else "tool_timeout",
        correct=correct,
        terminal_success=terminal_success,
        expected_tool_sequence=("search_recent_logs",) * call_count,
        actual_tool_sequence=("search_recent_logs",) * call_count,
        sequence_match_count=call_count,
        sequence_denominator=call_count,
        unnecessary_call_count=0,
        logical_tool_call_count=call_count,
        attempt_evidence=tuple(attempts),
        attempt_count=len(attempts),
        retry_attempt_count=int(bool(recovered)),
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
                accepted=(
                    case.declared_faults[0].action_step
                    if case.declared_faults
                    else case.accepted_output_count
                ),
                fault_step=(
                    case.declared_faults[0].action_step
                    if case.declared_faults
                    else None
                ),
                correct=(
                    case.correct if case.verified_transient_fault_count == 0 else False
                ),
                terminal_success=(
                    case.terminal_success
                    if case.verified_transient_fault_count == 0
                    else False
                ),
            )
            for case in resilient_cases
        )
    manifest = tuple(
        SuiteManifestEntry(
            relative_path=f"{case.scenario_id}.yaml",
            scenario_id=case.scenario_id,
            version=1,
            scenario_sha256=case.scenario_sha256,
            initial_context={},
            logical_actions=case.logical_actions,
            expected_tool_sequence=case.expected_tool_sequence,
            expected_outcome=case.expected_outcome,
            declared_faults=case.declared_faults,
            approval_supplied=False,
        )
        for case in resilient_cases
    )
    provenance = EvaluationProvenance(
        report_version="5",
        schema_version="5",
        grader_version="exact-v5",
        normalization_version="baseline-v5",
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
