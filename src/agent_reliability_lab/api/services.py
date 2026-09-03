"""Application services that isolate synchronous HTTP work from domain internals."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from agent_reliability_lab.api.errors import ApiError
from agent_reliability_lab.api.schemas import (
    EvaluationCaseResponse,
    EvaluationListResponse,
    EvaluationModeResponse,
    EvaluationProvenanceResponse,
    EvaluationResponse,
    HealthResponse,
    RunListResponse,
    RunResponse,
    RunResultResponse,
    ScenarioFaultResponse,
    ScenarioListResponse,
    ScenarioResponse,
    TraceEventResponse,
    TracePageResponse,
    TracePayloadResponse,
)
from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.actions import CallToolAction
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.domain.scenarios import Scenario
from agent_reliability_lab.evaluation.models import EvaluationReport
from agent_reliability_lab.evaluation.runner import run_evaluation
from agent_reliability_lab.runtime.service import (
    RunConflictError,
    RunNotFoundError,
    RunService,
)
from agent_reliability_lab.scenarios.loader import load_scenario
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry


class ScenarioCatalog:
    """Immutable server-owned scenario and suite lookup by public identifier."""

    def __init__(
        self,
        scenario_dir: Path,
        evaluation_suites: tuple[tuple[str, Path], ...],
    ) -> None:
        scenarios: dict[str, Scenario] = {}
        for path in sorted(scenario_dir.glob("*.yaml")):
            scenario = load_scenario(path)
            if scenario.id in scenarios:
                raise ValueError(f"duplicate scenario id: {scenario.id}")
            scenarios[scenario.id] = scenario
        if not scenarios:
            raise ValueError("scenario catalog is empty")
        self._scenarios = scenarios
        self._suites = dict(evaluation_suites)

    def load(self, scenario_id: str) -> Scenario:
        try:
            return self._scenarios[scenario_id]
        except KeyError as error:
            raise ApiError(
                404, "scenario_not_found", "Scenario was not found."
            ) from error

    def suite(self, suite_name: str) -> Path:
        try:
            return self._suites[suite_name]
        except KeyError as error:
            raise ApiError(404, "suite_not_found", "Suite was not found.") from error

    def list(self) -> ScenarioListResponse:
        registry = incident_registry(IncidentBackend())
        items: list[ScenarioResponse] = []
        for scenario in sorted(self._scenarios.values(), key=lambda item: item.id):
            approval_required = any(
                isinstance(action, CallToolAction)
                and (definition := registry.get(action.tool_name)) is not None
                and definition.requires_approval
                for action in scenario.actions
            )
            items.append(
                ScenarioResponse(
                    id=scenario.id,
                    expected_outcome=scenario.expected_outcome,
                    expected_tool_sequence=scenario.expected_tool_sequence,
                    faults=tuple(
                        ScenarioFaultResponse(
                            tool_name=fault.tool_name,
                            attempt=fault.attempt,
                            type=fault.type.value,
                        )
                        for fault in scenario.faults
                    ),
                    approval_required=approval_required,
                )
            )
        return ScenarioListResponse(items=items)


class _RunLocks:
    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[UUID, Lock] = {}

    @contextmanager
    def hold(self, run_id: UUID) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(run_id, Lock())
        with lock:
            yield


class RunQueryService:
    def __init__(self, store: SQLiteRunStore) -> None:
        self._store = store

    def get(self, run_id: UUID) -> RunResponse:
        run = self._required(run_id)
        counts = self._store.count_events_by_trace((run.trace_id,))
        return _run_response(run, counts[run.trace_id])

    def list(self, *, limit: int) -> RunListResponse:
        runs = self._store.list_runs(limit=limit)
        counts = self._store.count_events_by_trace(tuple(run.trace_id for run in runs))
        return RunListResponse(
            items=[_run_response(run, counts[run.trace_id]) for run in runs]
        )

    def trace(
        self, run_id: UUID, *, after_sequence: int, limit: int
    ) -> TracePageResponse:
        run = self._required(run_id)
        events, has_more = self._store.list_events_page(
            run.trace_id, after_sequence=after_sequence, limit=limit
        )
        cursor = events[-1].sequence if events else after_sequence
        return TracePageResponse(
            events=[_trace_response(event) for event in events],
            next_after_sequence=cursor or after_sequence,
            has_more=has_more,
        )

    def _required(self, run_id: UUID) -> Run:
        run = self._store.get_run(run_id)
        if run is None:
            raise ApiError(404, "run_not_found", "Run was not found.")
        return run


class RunApplicationService:
    """Sync command facade; FastAPI executes its methods in a worker thread."""

    def __init__(
        self,
        store: SQLiteRunStore,
        catalog: ScenarioCatalog,
        queries: RunQueryService,
        locks: _RunLocks,
        secret_values: frozenset[str],
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._queries = queries
        self._locks = locks
        self._secret_values = secret_values

    def start(
        self, scenario_id: str, mode: Literal["fragile", "resilient"]
    ) -> RunResponse:
        run = asyncio.run(self._runtime().start(scenario_id, mode))
        return self._queries.get(run.id)

    def approve(
        self,
        run_id: UUID,
        *,
        actor: str,
        allow: bool,
        reason: str | None,
    ) -> RunResponse:
        with self._locks.hold(run_id):
            try:
                run = asyncio.run(
                    self._runtime().approve(
                        run_id, actor=actor, allow=allow, reason=reason
                    )
                )
            except RunNotFoundError as error:
                raise ApiError(404, "run_not_found", "Run was not found.") from error
            except RunConflictError as error:
                raise ApiError(
                    409, "invalid_transition", "Run cannot accept this operation."
                ) from error
            except ValueError as error:
                raise ApiError(
                    409, "approval_conflict", "Approval decision conflicts."
                ) from error
        return self._queries.get(run.id)

    def resume(self, run_id: UUID) -> RunResponse:
        with self._locks.hold(run_id):
            try:
                run = asyncio.run(self._runtime().resume(run_id))
            except RunNotFoundError as error:
                raise ApiError(404, "run_not_found", "Run was not found.") from error
            except RunConflictError as error:
                raise ApiError(
                    409, "invalid_transition", "Run cannot be resumed."
                ) from error
        return self._queries.get(run.id)

    def _runtime(self) -> RunService:
        recorder = TraceRecorder(self._store, set(self._secret_values))
        backend = IncidentBackend()
        gateway = ToolGateway(
            self._store,
            recorder,
            incident_registry(backend),
            incident_backend=backend,
        )
        return RunService(self._store, recorder, gateway, self._catalog.load)


class EvaluationService:
    """Bounded sync facade over the CPU/SQLite-heavy async evaluator."""

    def __init__(
        self,
        store: SQLiteRunStore,
        catalog: ScenarioCatalog,
        semaphore: BoundedSemaphore,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._semaphore = semaphore

    def create(self, suite_name: str) -> EvaluationResponse:
        suite = self._catalog.suite(suite_name)
        if not self._semaphore.acquire(blocking=False):
            raise ApiError(
                409,
                "evaluation_in_progress",
                "Another evaluation is already running.",
            )
        try:
            report = asyncio.run(run_evaluation(suite, modes=("fragile", "resilient")))
            self._store.save_evaluation_report(report, suite_name=suite_name)
            return _evaluation_response(report)
        finally:
            self._semaphore.release()

    def get(self, evaluation_id: UUID) -> EvaluationResponse:
        report = self._store.get_evaluation_report(evaluation_id)
        if report is None:
            raise ApiError(404, "evaluation_not_found", "Evaluation was not found.")
        return _evaluation_response(report)

    def list(self, *, limit: int) -> EvaluationListResponse:
        return EvaluationListResponse(
            items=[
                _evaluation_response(report)
                for report in self._store.list_evaluation_reports(limit=limit)
            ]
        )


class HealthService:
    def __init__(self, store: SQLiteRunStore) -> None:
        self._store = store

    def ready(self) -> HealthResponse:
        try:
            self._store.ping()
        except Exception as error:
            raise ApiError(
                503, "service_unavailable", "Service is not ready."
            ) from error
        return HealthResponse(status="ready", database="ready")


class ApiContainer:
    """Lifespan-owned immutable catalog/store plus bounded service facades."""

    def __init__(self, settings: Settings) -> None:
        scenario_dir = (
            settings.scenario_dir
            or (Path.cwd() / "scenarios" / "incident-response").resolve()
        )
        suites = settings.evaluation_suites or (("incident-response", scenario_dir),)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SQLiteRunStore.from_settings(
            settings, secret_values=set(settings.secret_values)
        )
        self.store.create_schema()
        self.catalog = ScenarioCatalog(scenario_dir, suites)
        self.queries = RunQueryService(self.store)
        self.runs = RunApplicationService(
            self.store,
            self.catalog,
            self.queries,
            _RunLocks(),
            settings.secret_values,
        )
        self.evaluations = EvaluationService(
            self.store, self.catalog, BoundedSemaphore(1)
        )
        self.health = HealthService(self.store)


def _run_response(run: Run, attempt_count: int) -> RunResponse:
    return RunResponse(
        id=run.id,
        trace_id=run.trace_id,
        scenario_id=run.scenario_id,
        mode=run.mode,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        duration_ms=max(0.0, (run.updated_at - run.created_at).total_seconds() * 1000),
        approval_required=run.pending_approval,
        attempt_count=attempt_count,
        result=_run_result(run.result),
    )


def _run_result(value: dict[str, object] | None) -> RunResultResponse | None:
    if value is None:
        return None
    refs = value.get("evidence_refs")
    return RunResultResponse(
        outcome=_optional_string(value.get("outcome")),
        summary=_optional_string(value.get("summary")),
        code=_optional_string(value.get("code")),
        reason=_optional_string(value.get("reason")),
        explanation=_optional_string(value.get("explanation")),
        evidence_refs=tuple(item for item in refs if isinstance(item, str))
        if isinstance(refs, list | tuple)
        else (),
    )


_TRACE_STRING_KEYS = (
    "tool_name",
    "code",
    "kind",
    "source",
    "actor",
    "outcome",
    "summary",
    "reason",
    "from_status",
)
_TRACE_INTEGER_KEYS = ("attempt", "after_attempt", "action_step", "step")
_TRACE_BOOLEAN_KEYS = ("transient", "cached", "allow")


def _trace_response(event: TraceEvent) -> TraceEventResponse:
    raw = event.payload if isinstance(event.payload, Mapping) else {}
    safe: dict[str, JsonValue] = {}
    for key in _TRACE_STRING_KEYS:
        value = raw.get(key)
        if isinstance(value, str):
            safe[key] = value
    for key in _TRACE_INTEGER_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            safe[key] = value
    for key in _TRACE_BOOLEAN_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            safe[key] = value
    delay = raw.get("retry_delay_seconds")
    if isinstance(delay, int | float) and not isinstance(delay, bool):
        safe["retry_delay_seconds"] = float(delay)
    if event.sequence is None:  # pragma: no cover - persisted events are sequenced.
        raise RuntimeError("persisted event has no sequence")
    return TraceEventResponse(
        id=event.id,
        trace_id=event.trace_id,
        span_id=event.span_id,
        parent_span_id=event.parent_span_id,
        sequence=event.sequence,
        event_type=event.event_type,
        payload=TracePayloadResponse.model_validate(safe),
        duration_ms=event.duration_ms,
        status=event.status,
        created_at=event.created_at,
    )


def _evaluation_response(report: EvaluationReport) -> EvaluationResponse:
    modes: dict[Literal["fragile", "resilient"], EvaluationModeResponse] = {}
    for mode, result in report.modes.items():
        modes[mode] = EvaluationModeResponse(
            mode=mode,
            metrics=result.metrics,
            cases=tuple(
                EvaluationCaseResponse(
                    scenario_id=case.scenario_id,
                    mode=case.mode,
                    run_id=case.run_id,
                    final_status=case.final_status,
                    expected_outcome=case.expected_outcome,
                    observed_outcome=case.observed_outcome,
                    correct=case.correct,
                    attempt_count=case.attempt_count,
                    retry_attempt_count=case.retry_attempt_count,
                )
                for case in result.cases
            ),
        )
    provenance = report.provenance
    return EvaluationResponse(
        evaluation_id=report.evaluation_id,
        generated_at=report.generated_at,
        provenance=EvaluationProvenanceResponse(
            report_version=provenance.report_version,
            grader_version=provenance.grader_version,
            suite_hash=provenance.suite_hash,
            git_revision=provenance.git_revision,
            git_dirty=provenance.git_dirty,
            package_version=provenance.package_version,
        ),
        modes=modes,
        comparison=report.comparison,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


__all__ = [
    "ApiContainer",
    "EvaluationService",
    "RunApplicationService",
    "ScenarioCatalog",
]
