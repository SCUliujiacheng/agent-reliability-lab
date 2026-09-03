"""Exact-fraction reliability gate with summary and baseline integrity checks."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise
from typing import Any, cast

from pydantic import ValidationError

from agent_reliability_lab.evaluation.graders import (
    aggregate_cases,
    tool_sequence_grade,
    unnecessary_call_count,
)
from agent_reliability_lab.evaluation.models import (
    AcceptedOutputEvidence,
    AttemptEvidence,
    CaseResult,
    EvaluationReport,
    FaultEvidence,
    GateResult,
    OutputValidationEvidence,
    SuiteManifestEntry,
)
from agent_reliability_lab.evaluation.runner import (
    canonical_action_fingerprint,
    trace_evidence_digest,
)
from agent_reliability_lab.tools.incident import (
    IncidentBackend,
    deterministic_incident_initial_context,
    deterministic_incident_output,
    incident_registry,
)

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
    manifest_by_id = {
        entry.scenario_id: entry for entry in report.provenance.suite_manifest
    }
    run_ids = [case.run_id for result in report.modes.values() for case in result.cases]
    trace_ids = [
        case.trace_id for result in report.modes.values() for case in result.cases
    ]
    event_ids = [
        item.event_id
        for result in report.modes.values()
        for case in result.cases
        for item in case.trace_evidence
    ]
    if (
        len(run_ids) != len(set(run_ids))
        or len(trace_ids) != len(set(trace_ids))
        or len(event_ids) != len(set(event_ids))
    ):
        errors.append("case_evidence_mismatch")
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
            errors.extend(_case_evidence_errors(case, manifest_by_id[case.scenario_id]))
    fragile_by_id = {case.scenario_id: case for case in report.modes["fragile"].cases}
    resilient_by_id = {
        case.scenario_id: case for case in report.modes["resilient"].cases
    }
    for scenario_id in fragile_by_id.keys() & resilient_by_id.keys():
        fragile = fragile_by_id[scenario_id]
        resilient = resilient_by_id[scenario_id]
        if (
            fragile.logical_actions != resilient.logical_actions
            or fragile.expected_outcome != resilient.expected_outcome
            or fragile.expected_tool_sequence != resilient.expected_tool_sequence
            or fragile.declared_faults != resilient.declared_faults
        ):
            errors.append("case_evidence_mismatch")
        if not any(
            fault.kind in _TRANSIENT_KINDS for fault in fragile.declared_faults
        ) and _normalized_case_semantics(fragile) != _normalized_case_semantics(
            resilient
        ):
            errors.append("case_evidence_mismatch")
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


def _case_evidence_errors(case: CaseResult, frozen: SuiteManifestEntry) -> list[str]:
    errors: list[str] = []
    expected_initial_context = deterministic_incident_initial_context(
        frozen.scenario_id
    )
    if (
        expected_initial_context is not None
        and frozen.initial_context != expected_initial_context
    ):
        errors.append("case_evidence_mismatch")
        return errors
    trace = _trace_claims(case, frozen)
    if trace is None:
        errors.append("case_evidence_mismatch")
        return errors
    if (
        case.logical_actions != frozen.logical_actions
        or case.expected_outcome != frozen.expected_outcome
        or case.expected_tool_sequence != frozen.expected_tool_sequence
        or case.declared_faults != frozen.declared_faults
        or case.attempt_evidence != trace.attempts
        or case.observed_faults != trace.faults
        or case.output_validation_failures != trace.validation_failures
        or case.accepted_outputs != trace.accepted_outputs
        or case.actual_tool_sequence != trace.actual_tool_sequence
        or case.final_status != trace.final_status
        or case.final_result != trace.final_result
        or case.terminal_success != (trace.final_status == "succeeded")
        or case.approval_reconstructed != trace.approval_reconstructed
    ):
        errors.append("case_evidence_mismatch")
    observed_outcome = _terminal_outcome(trace.final_status, trace.final_result)
    if case.observed_outcome != observed_outcome or case.correct != (
        observed_outcome == frozen.expected_outcome
    ):
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

    if Counter(frozen.declared_faults) != Counter(case.observed_faults):
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

    if trace.approval_reconstructed and (
        not frozen.approval_supplied
        or case.pre_pause_write_execution_count != 0
        or case.write_execution_count != 1
    ):
        errors.append("case_evidence_mismatch")
    return errors


@dataclass(frozen=True, slots=True)
class _TraceClaims:
    actual_tool_sequence: tuple[str, ...]
    attempts: tuple[AttemptEvidence, ...]
    faults: tuple[FaultEvidence, ...]
    validation_failures: tuple[OutputValidationEvidence, ...]
    accepted_outputs: tuple[AcceptedOutputEvidence, ...]
    final_status: str
    final_result: dict[str, Any]
    approval_reconstructed: bool


def _trace_claims(case: CaseResult, frozen: SuiteManifestEntry) -> _TraceClaims | None:
    if case.trace_digest != trace_evidence_digest(case.trace_evidence):
        return None
    if any(
        item.trace_id != case.trace_id or item.parent_span_id != case.trace_id
        for item in case.trace_evidence
    ):
        return None
    event_ids = [item.event_id for item in case.trace_evidence]
    if len(event_ids) != len(set(event_ids)):
        return None
    sequences = [item.sequence for item in case.trace_evidence]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        return None
    frozen_by_step = {item.action_step: item for item in frozen.logical_actions}
    if sorted(frozen_by_step) != list(range(len(frozen_by_step))):
        return None
    for action in frozen.logical_actions:
        if action.action_payload.get("type") != action.kind:
            return None
        if action.kind == "call_tool":
            if action.action_payload.get("tool_name") != action.tool_name:
                return None
        elif action.tool_name is not None:
            return None
        if (
            canonical_action_fingerprint(action.action_payload)
            != action.action_fingerprint
        ):
            return None
        if action.kind == "call_tool":
            arguments = action.action_payload.get("arguments")
            if not isinstance(arguments, dict) or action.tool_name is None:
                return None
            expected_output = deterministic_incident_output(action.tool_name, arguments)
            expected_digest = (
                _canonical_json_digest(expected_output)
                if expected_output is not None
                else None
            )
            if action.expected_output_digest != expected_digest:
                return None
        elif action.expected_output_digest is not None:
            return None

    seen_actions: dict[int, str] = {}
    last_policy_step = -1
    policy_positions: dict[int, list[int]] = defaultdict(list)
    actual: list[str] = []
    active_call: tuple[int, str] | None = None
    terminal_action_seen = False
    terminal_action_kind: str | None = None
    terminal_action_payload: dict[str, Any] | None = None
    terminal_seen = False
    started: set[tuple[int, str, int]] = set()
    attempts: list[AttemptEvidence] = []
    attempt_start_positions: dict[tuple[int, str, int], int] = {}
    attempt_spans: dict[tuple[int, str, int], Any] = {}
    last_attempt_by_action: dict[tuple[int, str], AttemptEvidence] = {}
    pending_success: tuple[int, str, int, Any] | None = None
    accepted_outputs: list[AcceptedOutputEvidence] = []
    reconstructed_context = dict(frozen.initial_context)
    initial_results = reconstructed_context.get("tool_results", {})
    context_outputs = dict(initial_results) if isinstance(initial_results, dict) else {}
    faults: list[FaultEvidence] = []
    validations: list[OutputValidationEvidence] = []
    waits: list[tuple[int, int, str, str]] = []
    approvals: list[tuple[int, int, str, str, bool]] = []
    resumes: list[int] = []
    final_status: str | None = None
    final_result: dict[str, Any] | None = None
    preflight_failure: tuple[int, str, str] | None = None
    denied: tuple[int, str, str, str] | None = None
    must_fail_preflight: str | None = None
    must_record_denial: tuple[int, str, str] | None = None
    must_fail_approval_denied: str | None = None

    for item in case.trace_evidence:
        payload = item.payload
        if pending_success is not None and item.event_type != "run.checkpointed":
            return None
        if must_fail_preflight is not None and item.event_type != "run.failed":
            return None
        if must_record_denial is not None and item.event_type != "approval.denied":
            return None
        if must_fail_approval_denied is not None and item.event_type != "run.failed":
            return None
        if terminal_seen:
            return None
        if terminal_action_seen and item.event_type not in {
            "run.succeeded",
            "run.failed",
        }:
            return None
        if item.event_type == "policy.action":
            if terminal_action_seen:
                return None
            try:
                step = _payload_int(payload, "action_step")
                action_payload = {
                    key: value for key, value in payload.items() if key != "action_step"
                }
                kind = cast(str, action_payload["type"])
                frozen_action = frozen_by_step[step]
                tool_name = cast(str | None, action_payload.get("tool_name"))
            except (KeyError, TypeError, ValueError):
                return None
            if (
                kind != frozen_action.kind
                or tool_name != frozen_action.tool_name
                or action_payload != frozen_action.action_payload
                or canonical_action_fingerprint(action_payload)
                != frozen_action.action_fingerprint
            ):
                return None
            if step < last_policy_step:
                return None
            if step not in seen_actions:
                if step != len(seen_actions):
                    return None
                seen_actions[step] = kind
                if kind == "call_tool" and tool_name is not None:
                    actual.append(tool_name)
            elif seen_actions[step] != kind:
                return None
            policy_positions[step].append(item.sequence)
            last_policy_step = max(last_policy_step, step)
            active_call = (
                (step, tool_name)
                if kind == "call_tool" and tool_name is not None
                else None
            )
            terminal_action_seen = kind in {"finish", "fail"}
            if terminal_action_seen:
                terminal_action_kind = kind
                terminal_action_payload = action_payload
            continue

        if item.event_type.startswith("tool.attempt."):
            identity = _attempt_identity(payload)
            if identity is None or active_call != identity[:2] or terminal_action_seen:
                return None
            if item.event_type == "tool.attempt.started":
                if item.status != "ok":
                    return None
                if identity in started:
                    return None
                action_identity = identity[:2]
                previous = last_attempt_by_action.get(action_identity)
                if identity[2] == 1:
                    if previous is not None:
                        return None
                elif (
                    previous is None
                    or identity[2] != previous.attempt + 1
                    or previous.status != "failed"
                    or previous.transient is not True
                ):
                    return None
                if identity[2] > (1 if case.mode == "fragile" else 2):
                    return None
                started.add(identity)
                attempt_start_positions[identity] = item.sequence
                attempt_spans[identity] = item.span_id
                continue
            if identity not in started:
                return None
            if item.span_id != attempt_spans[identity]:
                return None
            started.remove(identity)
            status = item.event_type.removeprefix("tool.attempt.")
            if item.status != ("ok" if status == "succeeded" else "error"):
                return None
            terminal_attempt = AttemptEvidence(
                action_step=identity[0],
                tool_name=identity[1],
                attempt=identity[2],
                status=cast(Any, status),
                error_code=cast(str | None, payload.get("code")),
                transient=cast(bool | None, payload.get("transient")),
            )
            attempts.append(terminal_attempt)
            last_attempt_by_action[identity[:2]] = terminal_attempt
            if status == "succeeded":
                if "output" not in payload:
                    return None
                action = frozen_by_step[identity[0]]
                if (
                    action.expected_output_digest is None
                    or _canonical_json_digest(payload["output"])
                    != action.expected_output_digest
                ):
                    return None
                pending_success = (*identity, payload["output"])
            continue

        if item.event_type in {"fault.injected", "tool.output.validation_failed"}:
            identity = _attempt_identity(payload)
            if (
                identity is None
                or active_call != identity[:2]
                or identity not in started
                or terminal_action_seen
                or item.span_id != attempt_spans[identity]
                or item.status != "error"
            ):
                return None
            try:
                if item.event_type == "fault.injected":
                    faults.append(
                        FaultEvidence(
                            action_step=identity[0],
                            tool_name=identity[1],
                            attempt=identity[2],
                            kind=cast(Any, payload["kind"]),
                        )
                    )
                else:
                    validations.append(OutputValidationEvidence.model_validate(payload))
            except (ValueError, TypeError):
                return None
            continue

        if item.event_type == "run.checkpointed":
            if pending_success is None or item.status != "ok":
                return None
            step, tool_name, attempt, output = pending_success
            try:
                context_digest = cast(str, payload["context_digest"])
                cached = payload["cached"]
            except KeyError:
                return None
            if (
                payload.get("action_step") != step
                or payload.get("attempt") != attempt
                or payload.get("current_step") != step + 1
                or not isinstance(cached, bool)
                or payload.get("output_digest") != _canonical_json_digest(output)
                or not isinstance(context_digest, str)
            ):
                return None
            context_outputs[str(step)] = output
            reconstructed_context = {
                **reconstructed_context,
                "tool_results": dict(context_outputs),
            }
            if context_digest != _canonical_json_digest(reconstructed_context):
                return None
            accepted_outputs.append(
                AcceptedOutputEvidence(
                    action_step=step,
                    tool_name=tool_name,
                    output=output,
                )
            )
            pending_success = None
            active_call = None
            continue

        if item.event_type == "tool.preflight.failed":
            if (
                active_call is None
                or item.status != "error"
                or started
                or preflight_failure is not None
            ):
                return None
            try:
                step = _payload_int(payload, "action_step")
                tool_name = cast(str, payload["tool_name"])
                code = cast(str, payload["code"])
                fingerprint = cast(str, payload["action_fingerprint"])
            except (KeyError, TypeError, ValueError):
                return None
            preflight_action = frozen_by_step.get(step)
            if (
                active_call != (step, tool_name)
                or preflight_action is None
                or fingerprint != preflight_action.action_fingerprint
                or code
                not in {
                    "unknown_tool",
                    "invalid_input",
                    "invalid_fault_plan",
                    "idempotency_key_required",
                }
            ):
                return None
            preflight_failure = (step, tool_name, code)
            must_fail_preflight = code
            continue

        if item.event_type == "run.waiting_approval":
            try:
                step = _payload_int(payload, "step")
                tool_name = cast(str, payload["tool_name"])
                fingerprint = cast(str, payload["action_fingerprint"])
            except (KeyError, TypeError, ValueError):
                return None
            if active_call != (step, tool_name) or terminal_action_seen:
                return None
            waits.append((item.sequence, step, tool_name, fingerprint))
            continue

        if item.event_type == "approval.recorded":
            try:
                step = _payload_int(payload, "action_step")
                fingerprint = cast(str, payload["action_fingerprint"])
            except (KeyError, TypeError, ValueError):
                return None
            actor = payload.get("actor")
            allow = payload.get("allow")
            if not isinstance(actor, str) or not isinstance(allow, bool):
                return None
            approval_action = frozen_by_step.get(step)
            if (
                approval_action is None
                or fingerprint != approval_action.action_fingerprint
                or active_call != (step, approval_action.tool_name)
            ):
                return None
            if allow:
                if must_record_denial is not None or denied is not None:
                    return None
            else:
                if any(previous[4] for previous in approvals):
                    return None
                must_record_denial = (step, fingerprint, actor)
            approvals.append((item.sequence, step, fingerprint, actor, allow))
            continue

        if item.event_type == "approval.denied":
            try:
                step = _payload_int(payload, "action_step")
                fingerprint = cast(str, payload["action_fingerprint"])
                actor = cast(str, payload["actor"])
            except (KeyError, TypeError, ValueError):
                return None
            denied_action = frozen_by_step.get(step)
            if (
                item.status != "error"
                or denied_action is None
                or fingerprint != denied_action.action_fingerprint
                or not isinstance(actor, str)
                or active_call != (step, denied_action.tool_name)
                or must_record_denial != (step, fingerprint, actor)
                or any(previous[4] for previous in approvals)
            ):
                return None
            denied = (step, fingerprint, actor, cast(str, payload.get("reason")))
            must_record_denial = None
            must_fail_approval_denied = cast(str, payload.get("reason"))
            continue

        if item.event_type == "tool.retry.cancelled":
            return None

        if item.event_type == "run.running":
            if item.status != "ok":
                return None
            if payload.get("from_status") == "waiting_approval":
                resumes.append(item.sequence)
            continue

        if item.event_type in {"run.succeeded", "run.failed"}:
            if started:
                return None
            if item.status != ("ok" if item.event_type == "run.succeeded" else "error"):
                return None
            if terminal_action_kind == "finish":
                if (
                    item.event_type != "run.succeeded"
                    or terminal_action_payload is None
                ):
                    return None
                expected_result = {
                    "outcome": terminal_action_payload.get("outcome"),
                    "summary": terminal_action_payload.get("summary"),
                    "evidence_refs": terminal_action_payload.get("evidence_refs"),
                }
                if payload != expected_result:
                    return None
            elif terminal_action_kind == "fail":
                if item.event_type != "run.failed" or terminal_action_payload is None:
                    return None
                expected_result = {
                    "code": terminal_action_payload.get("code"),
                    "explanation": terminal_action_payload.get("explanation"),
                }
                if payload != expected_result:
                    return None
            elif item.event_type == "run.succeeded":
                return None
            elif denied is not None:
                expected_denied = {"code": "approval_denied", "reason": denied[3]}
                if payload != expected_denied:
                    return None
                must_fail_approval_denied = None
            elif preflight_failure is not None:
                if payload != {"code": preflight_failure[2]}:
                    return None
                must_fail_preflight = None
            elif not attempts or attempts[-1].status != "failed":
                return None
            terminal_seen = True
            final_status = item.event_type.removeprefix("run.")
            final_result = dict(payload)
            continue

        return None

    if (
        started
        or pending_success is not None
        or must_fail_preflight is not None
        or must_record_denial is not None
        or must_fail_approval_denied is not None
        or final_status is None
        or final_result is None
    ):
        return None
    registry = incident_registry(IncidentBackend())
    for step, positions in policy_positions.items():
        action = frozen_by_step[step]
        definition = registry.get(action.tool_name) if action.tool_name else None
        allowed_count = (
            2
            if action.kind == "call_tool"
            and definition is not None
            and definition.requires_approval
            and any(identity[0] == step for identity in attempt_start_positions)
            else 1
        )
        if len(positions) != allowed_count:
            return None
    if not _attempt_semantics_are_supported(attempts, faults, validations):
        return None
    reconstructed = _approval_cycles_are_valid(
        case,
        frozen,
        policy_positions,
        attempt_start_positions,
        waits,
        approvals,
        resumes,
    )
    if reconstructed is None:
        return None
    return _TraceClaims(
        actual_tool_sequence=tuple(actual),
        attempts=tuple(attempts),
        faults=tuple(faults),
        validation_failures=tuple(validations),
        accepted_outputs=tuple(accepted_outputs),
        final_status=final_status,
        final_result=final_result,
        approval_reconstructed=reconstructed,
    )


def _attempt_semantics_are_supported(
    attempts: list[AttemptEvidence],
    faults: list[FaultEvidence],
    validations: list[OutputValidationEvidence],
) -> bool:
    fault_by_identity = Counter(
        (item.action_step, item.tool_name, item.attempt, item.kind) for item in faults
    )
    validation_by_identity = Counter(
        (item.action_step, item.tool_name, item.attempt) for item in validations
    )
    attempts_by_action: dict[tuple[int, str], list[AttemptEvidence]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_action[(attempt.action_step, attempt.tool_name)].append(attempt)
        if attempt.status == "succeeded":
            continue
        if attempt.status == "cancelled":
            return False
        identity = (attempt.action_step, attempt.tool_name, attempt.attempt)
        if attempt.error_code == "invalid_output":
            if attempt.transient is not False or validation_by_identity[identity] != 1:
                return False
        else:
            matching_faults = [
                kind
                for kind, (code, transient) in _FAULT_FAILURE.items()
                if fault_by_identity[(*identity, cast(Any, kind))] == 1
                and attempt.error_code == code
                and attempt.transient is transient
            ]
            if len(matching_faults) != 1:
                return False
    for action_attempts in attempts_by_action.values():
        for previous, later in pairwise(action_attempts):
            if (
                later.attempt != previous.attempt + 1
                or previous.status != "failed"
                or previous.transient is not True
            ):
                return False
    return True


def _approval_cycles_are_valid(
    case: CaseResult,
    frozen: SuiteManifestEntry,
    policy_positions: dict[int, list[int]],
    attempt_start_positions: dict[tuple[int, str, int], int],
    waits: list[tuple[int, int, str, str]],
    approvals: list[tuple[int, int, str, str, bool]],
    resumes: list[int],
) -> bool | None:
    registry = incident_registry(IncidentBackend())
    executed_steps = {step for step, _, _ in attempt_start_positions}
    approval_steps = {
        action.action_step
        for action in frozen.logical_actions
        if action.kind == "call_tool"
        and action.tool_name is not None
        and (definition := registry.get(action.tool_name)) is not None
        and definition.requires_approval
        and action.action_step in executed_steps
    }
    if not approval_steps:
        return False if not waits and not approvals and not resumes else None
    if not frozen.approval_supplied or len(approval_steps) != 1:
        return None
    step = next(iter(approval_steps))
    action = next(item for item in frozen.logical_actions if item.action_step == step)
    step_waits = [item for item in waits if item[1] == step]
    step_approvals = [item for item in approvals if item[1] == step]
    starts = [
        sequence
        for (attempt_step, _, _), sequence in attempt_start_positions.items()
        if attempt_step == step
    ]
    positions = policy_positions.get(step, [])
    if (
        len(waits) != 1
        or len(approvals) != 1
        or len(resumes) != 1
        or len(step_waits) != 1
        or len(step_approvals) != 1
        or len(positions) != 2
        or not starts
    ):
        return None
    wait_sequence, _, wait_tool, wait_fingerprint = step_waits[0]
    approval_sequence, _, approval_fingerprint, actor, allow = step_approvals[0]
    resume_sequence = resumes[0]
    if (
        wait_tool != action.tool_name
        or wait_fingerprint != action.action_fingerprint
        or approval_fingerprint != action.action_fingerprint
        or actor != "evaluation-reviewer"
        or allow is not True
        or not (
            positions[0]
            < wait_sequence
            < approval_sequence
            < resume_sequence
            < positions[1]
            < min(starts)
        )
        or case.pre_pause_write_execution_count != 0
        or case.write_execution_count != 1
    ):
        return None
    return True


def _attempt_identity(payload: dict[str, Any]) -> tuple[int, str, int] | None:
    try:
        return (
            _payload_int(payload, "action_step"),
            cast(str, payload["tool_name"]),
            _payload_int(payload, "attempt"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _payload_int(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(key)
    return value


def _terminal_outcome(status: str, result: dict[str, Any]) -> str:
    key = "outcome" if status == "succeeded" else "code"
    value = result.get(key)
    return value if isinstance(value, str) else "missing_terminal_outcome"


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


def _normalized_case_semantics(case: CaseResult) -> tuple[Any, ...]:
    """Mode-independent claims that must match when retry policy is irrelevant."""
    return (
        case.final_status,
        case.final_result,
        case.expected_outcome,
        case.observed_outcome,
        case.correct,
        case.terminal_success,
        case.actual_tool_sequence,
        case.attempt_evidence,
        case.observed_faults,
        case.output_validation_failures,
        case.accepted_outputs,
        case.approval_reconstructed,
        case.pre_pause_write_execution_count,
        case.write_execution_count,
        case.store_run_count,
    )


def _canonical_json_digest(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
