"""Golden-suite execution with trace-derived evidence and immutable provenance."""

import hashlib
import json
import platform
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Any, cast
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import AgentAction, CallToolAction
from agent_reliability_lab.domain.runs import Run, RunStatus
from agent_reliability_lab.domain.scenarios import FaultType, Scenario
from agent_reliability_lab.evaluation.graders import (
    aggregate_cases,
    tool_sequence_grade,
    unnecessary_call_count,
)
from agent_reliability_lab.evaluation.models import (
    AcceptedOutputEvidence,
    AttemptEvidence,
    CaseResult,
    EffectiveConfiguration,
    EvaluationMode,
    EvaluationProvenance,
    EvaluationReport,
    FaultEvidence,
    LogicalActionProjection,
    ModeComparison,
    ModeResult,
    OrderedTraceEvidence,
    OutputValidationEvidence,
    SuiteManifestEntry,
)
from agent_reliability_lab.runtime.service import RunService
from agent_reliability_lab.scenarios.loader import load_scenario, scenario_sha256
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.contracts import ToolRegistry
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import (
    IncidentBackend,
    deterministic_incident_actions,
    deterministic_incident_output,
    incident_registry,
)

CaseObserver = Callable[[CaseResult], None]
_TRANSIENT_FAULTS = {FaultType.TIMEOUT, FaultType.RATE_LIMIT, FaultType.TOOL_ERROR}


class EvaluationInfrastructureError(RuntimeError):
    """Raised when suite or trace evidence cannot support benchmark claims."""


def build_suite_manifest(suite: Path) -> tuple[SuiteManifestEntry, ...]:
    """Read a POSIX-path-sorted manifest using exact scenario file bytes."""
    root = suite.resolve()
    if not root.is_dir():
        raise EvaluationInfrastructureError(f"suite is not a directory: {suite}")
    entries: list[SuiteManifestEntry] = []
    seen_ids: set[str] = set()
    for path in sorted(
        root.rglob("*.yaml"), key=lambda value: value.relative_to(root).as_posix()
    ):
        scenario = load_scenario(path)
        if scenario.id in seen_ids:
            raise EvaluationInfrastructureError(
                f"duplicate scenario id in suite: {scenario.id}"
            )
        seen_ids.add(scenario.id)
        entries.append(
            SuiteManifestEntry(
                relative_path=path.relative_to(root).as_posix(),
                scenario_id=scenario.id,
                version=scenario.version,
                scenario_sha256=scenario_sha256(path),
                initial_context=cast(dict[str, JsonValue], scenario.initial_context),
                logical_actions=logical_action_projection(scenario),
                expected_tool_sequence=scenario.expected_tool_sequence,
                expected_outcome=scenario.expected_outcome,
                declared_faults=_declared_fault_evidence(scenario),
                approval_supplied=scenario.approval_supplied,
            )
        )
    if not entries:
        raise EvaluationInfrastructureError("suite contains no YAML scenarios")
    return tuple(entries)


def suite_sha256(manifest: Sequence[SuiteManifestEntry]) -> str:
    """Hash canonical JSON for the exact-byte suite manifest."""
    canonical = json.dumps(
        [
            entry.model_dump(mode="json")
            for entry in sorted(manifest, key=lambda item: item.relative_path)
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _validate_supported_frozen_suite(
    manifest: Sequence[SuiteManifestEntry],
) -> None:
    """Fail closed unless every scenario exactly matches a built-in projection."""
    for entry in manifest:
        expected = deterministic_incident_actions(entry.scenario_id)
        actual = tuple(action.action_payload for action in entry.logical_actions)
        if expected is None or actual != expected:
            raise EvaluationInfrastructureError(
                f"unsupported frozen scenario projection: {entry.scenario_id}"
            )


def canonical_action_fingerprint(action: AgentAction | dict[str, Any]) -> str:
    """Hash one complete frozen action using canonical JSON."""
    payload = action.model_dump(mode="json") if not isinstance(action, dict) else action
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def logical_action_projection(
    scenario: Scenario,
) -> tuple[LogicalActionProjection, ...]:
    """Project the exact ordered scripted actions that every mode must share."""
    return tuple(
        LogicalActionProjection(
            action_step=step,
            kind=action.type,
            tool_name=action.tool_name if isinstance(action, CallToolAction) else None,
            action_payload=cast(dict[str, JsonValue], action.model_dump(mode="json")),
            action_fingerprint=canonical_action_fingerprint(action),
            expected_output_digest=_expected_output_digest(action),
        )
        for step, action in enumerate(scenario.actions)
    )


def _expected_output_digest(action: AgentAction) -> str | None:
    if not isinstance(action, CallToolAction):
        return None
    output = deterministic_incident_output(action.tool_name, action.arguments)
    if output is None:
        return None
    canonical = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def trace_evidence_digest(evidence: Sequence[OrderedTraceEvidence]) -> str:
    """Digest the persisted ordered evidence projection, not opaque trace metadata."""
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in evidence],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def run_evaluation(
    suite: Path,
    modes: Sequence[EvaluationMode] = ("fragile", "resilient"),
    *,
    case_observer: CaseObserver | None = None,
) -> EvaluationReport:
    """Run identical frozen scenarios in requested modes with isolated stores."""
    requested_modes = tuple(modes)
    if not requested_modes or len(set(requested_modes)) != len(requested_modes):
        raise EvaluationInfrastructureError(
            "evaluation modes must be unique and non-empty"
        )
    if any(mode not in {"fragile", "resilient"} for mode in requested_modes):
        raise EvaluationInfrastructureError("unknown evaluation mode")

    root = suite.resolve()
    manifest = build_suite_manifest(root)
    _validate_supported_frozen_suite(manifest)
    initial_hash = suite_sha256(manifest)
    scenarios = [load_scenario(root / entry.relative_path) for entry in manifest]
    for scenario in scenarios:
        _validate_fault_targets(scenario)
    revision, dirty = _git_provenance(root)

    mode_results: dict[EvaluationMode, ModeResult] = {}
    for mode in requested_modes:
        cases: list[CaseResult] = []
        for entry, scenario in zip(manifest, scenarios, strict=True):
            case = await _run_case(entry, scenario, mode)
            cases.append(case)
            if case_observer is not None:
                case_observer(case)
            _assert_suite_unchanged(root, manifest, initial_hash)
        case_tuple = tuple(cases)
        mode_results[mode] = ModeResult(
            mode=mode,
            cases=case_tuple,
            metrics=aggregate_cases(case_tuple),
        )
    _assert_suite_unchanged(root, manifest, initial_hash)

    provenance = EvaluationProvenance(
        suite_hash=initial_hash,
        suite_manifest=manifest,
        git_revision=revision,
        git_dirty=dirty,
        effective_configuration=EffectiveConfiguration(),
        python_version=platform.python_version(),
        package_version=_package_version(),
    )
    report = EvaluationReport(
        evaluation_id=uuid4(),
        generated_at=datetime.now(UTC),
        provenance=provenance,
        modes=mode_results,
    )
    if {"fragile", "resilient"}.issubset(mode_results):
        report = report.model_copy(update={"comparison": compare_modes(report)})
    return report


def compare_modes(report: EvaluationReport) -> ModeComparison:
    """Return resilient-minus-fragile deltas and exact recovery-case losses."""
    fragile = report.modes.get("fragile")
    resilient = report.modes.get("resilient")
    if fragile is None or resilient is None:
        raise EvaluationInfrastructureError("comparison requires both modes")
    fragile_by_id = {case.scenario_id: case for case in fragile.cases}
    resilient_by_id = {case.scenario_id: case for case in resilient.cases}
    if set(fragile_by_id) != set(resilient_by_id):
        raise EvaluationInfrastructureError("mode scenario sets differ")
    worse = tuple(
        sorted(
            scenario_id
            for scenario_id, resilient_case in resilient_by_id.items()
            if resilient_case.recovered_transient_fault_count > 0
            and fragile_by_id[scenario_id].correct is False
            and resilient_case.correct is True
        )
    )
    left = fragile.metrics
    right = resilient.metrics
    recovery_delta = (
        None
        if left.recovery_rate is None or right.recovery_rate is None
        else right.recovery_rate - left.recovery_rate
    )
    return ModeComparison(
        task_correctness_rate_delta=(
            right.task_correctness_rate - left.task_correctness_rate
        ),
        recovery_rate_delta=recovery_delta,
        tool_sequence_accuracy_delta=(
            right.tool_sequence_accuracy - left.tool_sequence_accuracy
        ),
        invalid_output_rate_delta=(
            right.invalid_output_rate - left.invalid_output_rate
        ),
        unnecessary_call_count_delta=(
            right.unnecessary_call_count - left.unnecessary_call_count
        ),
        retry_attempt_count_delta=(
            right.retry_attempt_count - left.retry_attempt_count
        ),
        p50_latency_ms_delta=right.p50_latency_ms - left.p50_latency_ms,
        p95_latency_ms_delta=right.p95_latency_ms - left.p95_latency_ms,
        fragile_worse_recovery_scenarios=worse,
    )


def stable_report_projection(report: EvaluationReport) -> dict[str, object]:
    """Remove volatile identities/timing while retaining measured correctness evidence."""
    payload = report.model_dump(mode="json")
    payload.pop("evaluation_id", None)
    payload.pop("generated_at", None)
    provenance = cast(dict[str, object], payload["provenance"])
    provenance.pop("git_revision", None)
    provenance.pop("git_dirty", None)
    modes = cast(dict[str, dict[str, object]], payload["modes"])
    for result in modes.values():
        metrics = cast(dict[str, object], result["metrics"])
        metrics.pop("p50_latency_ms", None)
        metrics.pop("p95_latency_ms", None)
        for case in cast(list[dict[str, object]], result["cases"]):
            case.pop("run_id", None)
            case.pop("trace_id", None)
            case.pop("trace_digest", None)
            case.pop("latency_ns", None)
            for event in cast(list[dict[str, object]], case["trace_evidence"]):
                event.pop("event_id", None)
                event.pop("trace_id", None)
                event.pop("span_id", None)
                event.pop("parent_span_id", None)
    comparison = payload.get("comparison")
    if isinstance(comparison, dict):
        comparison.pop("p50_latency_ms_delta", None)
        comparison.pop("p95_latency_ms_delta", None)
    return cast(dict[str, object], payload)


async def _run_case(
    entry: SuiteManifestEntry, scenario: Scenario, mode: EvaluationMode
) -> CaseResult:
    stores: list[SQLiteRunStore] = []
    with TemporaryDirectory(prefix="arl-evaluation-") as temporary:
        database_path = Path(temporary) / "case.db"
        try:
            service, backend, registry = _build_service(database_path, scenario)
            stores.append(service.store)
            started = perf_counter_ns()
            run = await service.start(scenario.id, mode)
            approval_reconstructed = False
            pre_pause_write_execution_count = backend.rollback_preparations
            write_execution_count = pre_pause_write_execution_count
            if run.status is RunStatus.WAITING_APPROVAL and scenario.approval_supplied:
                if run.pending_action_fingerprint is None:
                    raise EvaluationInfrastructureError(
                        f"waiting run has no approval fingerprint: {scenario.id}"
                    )
                approval_step = run.current_step
                approval_fingerprint = run.pending_action_fingerprint
                approval_reconstructed = True
                if pre_pause_write_execution_count != 0:
                    raise EvaluationInfrastructureError(
                        f"approval wrote before pause: {scenario.id}"
                    )
                service, reconstructed_backend, registry = _build_service(
                    database_path, scenario
                )
                stores.append(service.store)
                run = await service.approve(
                    run.id,
                    actor="evaluation-reviewer",
                    allow=True,
                    expected_action_step=approval_step,
                    expected_action_fingerprint=approval_fingerprint,
                    reason="frozen suite approval",
                )
                write_execution_count = (
                    pre_pause_write_execution_count
                    + reconstructed_backend.rollback_preparations
                )
                repeated = await service.approve(
                    run.id,
                    actor="evaluation-reviewer",
                    allow=True,
                    expected_action_step=approval_step,
                    expected_action_fingerprint=approval_fingerprint,
                    reason="frozen suite approval",
                )
                total_effects = (
                    pre_pause_write_execution_count
                    + reconstructed_backend.rollback_preparations
                )
                if repeated != run or total_effects != 1:
                    raise EvaluationInfrastructureError(
                        f"approval was not exactly once: {scenario.id}"
                    )
            latency_ns = perf_counter_ns() - started
            events = service.store.list_events(run.trace_id)
            result = _grade_case(
                entry,
                scenario,
                mode,
                run,
                events,
                registry,
                latency_ns=latency_ns,
                approval_reconstructed=approval_reconstructed,
                pre_pause_write_execution_count=pre_pause_write_execution_count,
                write_execution_count=write_execution_count,
                store_run_count=len(service.list()),
            )
        finally:
            for store in stores:
                store.close()
        return result


def _build_service(
    database_path: Path, scenario: Scenario
) -> tuple[RunService, IncidentBackend, ToolRegistry]:
    settings = Settings(
        data_dir=database_path.parent.resolve(),
        database_path=database_path.resolve(),
    )
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    recorder = TraceRecorder(store)
    backend = IncidentBackend()
    registry = incident_registry(backend)

    async def no_delay(_: float) -> None:
        return None

    gateway = ToolGateway(
        store,
        recorder,
        registry,
        sleeper=no_delay,
        incident_backend=backend,
    )
    return (
        RunService(store, recorder, gateway, {scenario.id: scenario}.__getitem__),
        backend,
        registry,
    )


def _grade_case(
    entry: SuiteManifestEntry,
    scenario: Scenario,
    mode: EvaluationMode,
    run: Run,
    events: Sequence[TraceEvent],
    registry: ToolRegistry,
    *,
    latency_ns: int,
    approval_reconstructed: bool,
    pre_pause_write_execution_count: int,
    write_execution_count: int,
    store_run_count: int,
) -> CaseResult:
    trace_evidence = _ordered_trace_evidence(events)
    logical_actions = logical_action_projection(scenario)
    actual_sequence = _logical_tool_sequence(events)
    sequence = tool_sequence_grade(scenario.expected_tool_sequence, actual_sequence)
    declared = _declared_fault_evidence(scenario)
    observed = _observed_fault_evidence(events)
    if Counter(declared) != Counter(observed):
        raise EvaluationInfrastructureError(
            f"fault evidence mismatch for {scenario.id}/{mode}"
        )
    observed_outcome = _observed_outcome(run)
    correct = observed_outcome == scenario.expected_outcome
    attempt_evidence = _attempt_evidence(events)
    accepted_outputs = accepted_output_evidence(run, scenario, registry)
    validation_failures = _output_validation_evidence(events)
    invalid_accepted = count_invalid_accepted_outputs(accepted_outputs, registry)
    transient_declared = [
        fault
        for fault, rule in zip(declared, scenario.faults, strict=True)
        if rule.type in _TRANSIENT_FAULTS
    ]
    recovered = sum(
        _fault_recovered(fault, attempt_evidence, correct)
        for fault in transient_declared
    )
    retries_by_step: dict[int, int] = defaultdict(int)
    for attempt in attempt_evidence:
        retries_by_step[attempt.action_step] += 1
    retry_attempts = sum(max(0, count - 1) for count in retries_by_step.values())
    malformed = sum(fault.kind == "malformed_output" for fault in observed)
    return CaseResult(
        scenario_id=scenario.id,
        scenario_version=scenario.version,
        scenario_path=entry.relative_path,
        scenario_sha256=entry.scenario_sha256,
        mode=mode,
        run_id=run.id,
        trace_id=run.trace_id,
        trace_digest=trace_evidence_digest(trace_evidence),
        trace_evidence=trace_evidence,
        logical_actions=logical_actions,
        final_status=cast(Any, run.status.value),
        final_result=cast(dict[str, JsonValue] | None, run.result),
        expected_outcome=scenario.expected_outcome,
        observed_outcome=observed_outcome,
        correct=correct,
        terminal_success=run.status is RunStatus.SUCCEEDED,
        expected_tool_sequence=scenario.expected_tool_sequence,
        actual_tool_sequence=actual_sequence,
        sequence_match_count=sequence.match_count,
        sequence_denominator=sequence.denominator,
        unnecessary_call_count=unnecessary_call_count(
            scenario.expected_tool_sequence, actual_sequence
        ),
        logical_tool_call_count=len(actual_sequence),
        attempt_evidence=attempt_evidence,
        attempt_count=len(attempt_evidence),
        retry_attempt_count=retry_attempts,
        declared_faults=declared,
        observed_faults=observed,
        verified_transient_fault_count=len(transient_declared),
        recovered_transient_fault_count=recovered,
        accepted_outputs=accepted_outputs,
        accepted_output_count=len(accepted_outputs),
        invalid_output_accepted_count=invalid_accepted,
        invalid_output_detected_count=len(validation_failures),
        invalid_output_rejected_count=len(validation_failures),
        malformed_fault_injected_count=malformed,
        output_validation_failures=validation_failures,
        latency_ns=latency_ns,
        estimated_input_tokens=0,
        estimated_output_tokens=0,
        estimated_cost_usd=Decimal(0),
        approval_reconstructed=approval_reconstructed,
        pre_pause_write_execution_count=pre_pause_write_execution_count,
        write_execution_count=write_execution_count,
        store_run_count=store_run_count,
    )


def _validate_fault_targets(scenario: Scenario) -> None:
    seen: set[tuple[int, str, int, FaultType]] = set()
    for rule in scenario.faults:
        steps = [
            step
            for step, action in enumerate(scenario.actions)
            if isinstance(action, CallToolAction) and action.tool_name == rule.tool_name
        ]
        if len(steps) != 1:
            raise EvaluationInfrastructureError(
                f"fault target must match exactly one action: {scenario.id}/{rule.tool_name}"
            )
        identity = (steps[0], rule.tool_name, rule.attempt, rule.type)
        if identity in seen:
            raise EvaluationInfrastructureError(
                f"duplicate fault rule: {scenario.id}/{rule.tool_name}"
            )
        seen.add(identity)
        if rule.attempt > 1:
            raise EvaluationInfrastructureError(
                f"fault unreachable in fragile mode: {scenario.id}/{rule.tool_name}"
            )


def _declared_fault_evidence(scenario: Scenario) -> tuple[FaultEvidence, ...]:
    evidence: list[FaultEvidence] = []
    for rule in scenario.faults:
        step = next(
            index
            for index, action in enumerate(scenario.actions)
            if isinstance(action, CallToolAction) and action.tool_name == rule.tool_name
        )
        evidence.append(
            FaultEvidence(
                action_step=step,
                tool_name=rule.tool_name,
                attempt=rule.attempt,
                kind=cast(Any, rule.type.value),
            )
        )
    return tuple(evidence)


def _observed_fault_evidence(events: Sequence[TraceEvent]) -> tuple[FaultEvidence, ...]:
    return tuple(
        FaultEvidence(
            action_step=cast(int, _payload(event)["action_step"]),
            tool_name=cast(str, _payload(event)["tool_name"]),
            attempt=cast(int, _payload(event)["attempt"]),
            kind=cast(Any, _payload(event)["kind"]),
        )
        for event in _events(events, "fault.injected")
    )


def _logical_tool_sequence(events: Sequence[TraceEvent]) -> tuple[str, ...]:
    by_step: dict[int, str] = {}
    for event in _events(events, "policy.action"):
        payload = _payload(event)
        if payload.get("type") == "call_tool":
            step = cast(int, payload["action_step"])
            name = cast(str, payload["tool_name"])
            previous = by_step.setdefault(step, name)
            if previous != name:
                raise EvaluationInfrastructureError(
                    "logical action changed at one step"
                )
    return tuple(by_step[step] for step in sorted(by_step))


def _fault_recovered(
    fault: FaultEvidence, attempts: Sequence[AttemptEvidence], correct: bool
) -> int:
    return int(
        correct
        and any(
            attempt.action_step == fault.action_step
            and attempt.tool_name == fault.tool_name
            and attempt.attempt > fault.attempt
            and attempt.status == "succeeded"
            for attempt in attempts
        )
    )


def _attempt_evidence(events: Sequence[TraceEvent]) -> tuple[AttemptEvidence, ...]:
    started = [
        (
            cast(int, _payload(event)["action_step"]),
            cast(str, _payload(event)["tool_name"]),
            cast(int, _payload(event)["attempt"]),
        )
        for event in _events(events, "tool.attempt.started")
    ]
    terminal_events = [
        event
        for event in events
        if event.event_type
        in {
            "tool.attempt.succeeded",
            "tool.attempt.failed",
            "tool.attempt.cancelled",
        }
    ]
    evidence: list[AttemptEvidence] = []
    identities: list[tuple[int, str, int]] = []
    for event in terminal_events:
        payload = _payload(event)
        identity = (
            cast(int, payload["action_step"]),
            cast(str, payload["tool_name"]),
            cast(int, payload["attempt"]),
        )
        identities.append(identity)
        status = event.event_type.removeprefix("tool.attempt.")
        evidence.append(
            AttemptEvidence(
                action_step=identity[0],
                tool_name=identity[1],
                attempt=identity[2],
                status=cast(Any, status),
                error_code=cast(str | None, payload.get("code")),
                transient=cast(bool | None, payload.get("transient")),
            )
        )
    if Counter(started) != Counter(identities):
        raise EvaluationInfrastructureError("attempt trace evidence is incomplete")
    return tuple(evidence)


def _output_validation_evidence(
    events: Sequence[TraceEvent],
) -> tuple[OutputValidationEvidence, ...]:
    return tuple(
        OutputValidationEvidence.model_validate(_payload(event))
        for event in _events(events, "tool.output.validation_failed")
    )


def accepted_output_evidence(
    run: Run, scenario: Scenario, registry: ToolRegistry
) -> tuple[AcceptedOutputEvidence, ...]:
    results = run.context.get("tool_results", {})
    if not isinstance(results, dict):
        return ()
    accepted: list[AcceptedOutputEvidence] = []
    for raw_step, output in results.items():
        try:
            step = int(raw_step)
            action = scenario.actions[step]
        except (TypeError, ValueError, IndexError):
            raise EvaluationInfrastructureError(
                "run context has a non-numeric tool step"
            ) from None
        if (
            not isinstance(action, CallToolAction)
            or registry.get(action.tool_name) is None
        ):
            raise EvaluationInfrastructureError(
                "run context result has no registered tool"
            )
        accepted.append(
            AcceptedOutputEvidence(
                action_step=step,
                tool_name=action.tool_name,
                output=cast(JsonValue, output),
            )
        )
    return tuple(sorted(accepted, key=lambda item: item.action_step))


def count_invalid_accepted_outputs(
    accepted_outputs: Sequence[AcceptedOutputEvidence], registry: ToolRegistry
) -> int:
    """Independently validate durable context values against registered schemas."""
    invalid = 0
    for accepted in accepted_outputs:
        definition = registry.get(accepted.tool_name)
        if definition is None:
            invalid += 1
            continue
        try:
            definition.output_model.model_validate(accepted.output)
        except ValidationError:
            invalid += 1
    return invalid


def _observed_outcome(run: Run) -> str:
    result = run.result or {}
    if run.status is RunStatus.SUCCEEDED:
        value = result.get("outcome")
    elif run.status is RunStatus.FAILED:
        value = result.get("code")
    else:
        value = run.status.value
    return value if isinstance(value, str) else "missing_terminal_outcome"


def _events(events: Sequence[TraceEvent], event_type: str) -> list[TraceEvent]:
    return [event for event in events if event.event_type == event_type]


def _payload(event: TraceEvent) -> dict[str, Any]:
    if not isinstance(event.payload, dict):
        raise EvaluationInfrastructureError(
            f"non-object trace payload: {event.event_type}"
        )
    return cast(dict[str, Any], event.payload)


_EVIDENCE_EVENT_TYPES = {
    "run.running",
    "policy.action",
    "tool.attempt.started",
    "fault.injected",
    "tool.output.validation_failed",
    "tool.preflight.failed",
    "tool.attempt.failed",
    "tool.attempt.succeeded",
    "tool.attempt.cancelled",
    "run.waiting_approval",
    "approval.recorded",
    "approval.denied",
    "run.checkpointed",
    "tool.retry.cancelled",
    "run.succeeded",
    "run.failed",
}


def _ordered_trace_evidence(
    events: Sequence[TraceEvent],
) -> tuple[OrderedTraceEvidence, ...]:
    evidence: list[OrderedTraceEvidence] = []
    for event in events:
        if event.event_type not in _EVIDENCE_EVENT_TYPES:
            continue
        if event.sequence is None:
            raise EvaluationInfrastructureError("trace event has no durable sequence")
        evidence.append(
            OrderedTraceEvidence(
                event_id=event.id,
                trace_id=event.trace_id,
                span_id=event.span_id,
                parent_span_id=event.parent_span_id,
                status=event.status,
                sequence=event.sequence,
                event_type=cast(Any, event.event_type),
                payload=cast(dict[str, JsonValue], _payload(event)),
            )
        )
    return tuple(evidence)


def _assert_suite_unchanged(
    root: Path,
    initial_manifest: Sequence[SuiteManifestEntry],
    initial_hash: str,
) -> None:
    try:
        current_manifest = build_suite_manifest(root)
    except Exception as error:
        raise EvaluationInfrastructureError(
            "suite changed during evaluation"
        ) from error
    if (
        tuple(initial_manifest) != current_manifest
        or suite_sha256(current_manifest) != initial_hash
    ):
        raise EvaluationInfrastructureError("suite changed during evaluation")


def _git_provenance(location: Path) -> tuple[str, bool]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=location,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=location,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return revision or "unavailable", bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unavailable", False


def _package_version() -> str:
    try:
        return version("agent-reliability-lab")
    except PackageNotFoundError:
        return "unavailable"


__all__ = [
    "EvaluationInfrastructureError",
    "accepted_output_evidence",
    "build_suite_manifest",
    "canonical_action_fingerprint",
    "compare_modes",
    "count_invalid_accepted_outputs",
    "logical_action_projection",
    "run_evaluation",
    "stable_report_projection",
    "suite_sha256",
    "trace_evidence_digest",
]
