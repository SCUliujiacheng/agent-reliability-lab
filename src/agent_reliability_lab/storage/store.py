"""Transactional SQLite persistence for runs and audit records."""

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import (
    ToolClaim,
    ToolClaimState,
    ToolFailureDisposition,
    TraceEvent,
)
from agent_reliability_lab.storage.sanitization import sanitize_payload

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ConcurrentUpdateError(RuntimeError):
    """Raised when a stale run snapshot loses a compare-and-swap update."""


class Base(DeclarativeBase):
    """Base declarative mapping for persistence records."""


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class TraceCounterRow(Base):
    __tablename__ = "trace_counters"
    trace_id: Mapped[str] = mapped_column(ForeignKey("runs.trace_id"), primary_key=True)
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("trace_id", "sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(ForeignKey("runs.trace_id"), nullable=False)
    span_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)


class ToolResultRow(Base):
    __tablename__ = "tool_results"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_disposition: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    allow: Mapped[bool] = mapped_column(nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(40), nullable=False)


class EvaluationRow(Base):
    __tablename__ = "evaluations"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    evaluation_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class SQLiteRunStore:
    """Synchronous SQLite store with transactional concurrency boundaries."""

    def __init__(
        self, settings: Settings, *, secret_values: set[str] | None = None
    ) -> None:
        """Construct a store only from settings that confine its database path."""
        if not isinstance(settings, Settings):
            raise TypeError("SQLiteRunStore requires Settings; use from_settings")
        database_url = _database_url(settings)
        self._engine: Engine = create_engine(database_url)
        self._session = sessionmaker(self._engine)
        self._secret_values = secret_values or set()
        event.listen(self._engine, "connect", _configure_sqlite_connection)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, secret_values: set[str] | None = None
    ) -> "SQLiteRunStore":
        """Construct a SQLite store from settings validated by the same boundary."""
        return cls(settings, secret_values=secret_values)

    def create_schema(self) -> None:
        Base.metadata.create_all(self._engine)

    def save_run(self, run: Run, *, expected_version: int | None = None) -> Run:
        """Persist and return the canonical immutable run carrying its DB version."""
        with self._session.begin() as session:
            existing = session.get(RunRow, str(run.id))
            if existing is None:
                persisted = run.model_copy(update={"version": 1})
                session.add(
                    RunRow(
                        id=str(run.id),
                        trace_id=str(run.trace_id),
                        version=1,
                        payload=_dump(persisted),
                    )
                )
                return persisted
            if expected_version is None:
                raise ConcurrentUpdateError(
                    "expected_version is required to update a run"
                )
            persisted = run.model_copy(update={"version": expected_version + 1})
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(RunRow)
                    .where(RunRow.id == str(run.id), RunRow.version == expected_version)
                    .values(
                        payload=_dump(persisted),
                        trace_id=str(run.trace_id),
                        version=expected_version + 1,
                    )
                ),
            )
            if result.rowcount != 1:
                raise ConcurrentUpdateError("stale run version")
            return persisted

    def get_run(self, run_id: UUID) -> Run | None:
        with self._session() as session:
            row = session.get(RunRow, str(run_id))
            return Run.model_validate_json(row.payload) if row is not None else None

    def append_event(self, event_value: TraceEvent) -> TraceEvent:
        """Allocate a unique trace sequence and persist a sanitized event."""
        clean = event_value.model_copy(
            update={
                "payload": sanitize_payload(event_value.payload, self._secret_values),
                "attributes": sanitize_payload(
                    event_value.attributes, self._secret_values
                ),
            }
        )
        with self._session.begin() as session:
            next_sequence = session.execute(
                insert(TraceCounterRow)
                .values(trace_id=str(clean.trace_id), next_sequence=1)
                .on_conflict_do_update(
                    index_elements=[TraceCounterRow.trace_id],
                    set_={"next_sequence": TraceCounterRow.next_sequence + 1},
                )
                .returning(TraceCounterRow.next_sequence)
            ).scalar_one()
            stored = clean.model_copy(update={"sequence": next_sequence})
            session.add(
                EventRow(
                    id=str(stored.id),
                    trace_id=str(stored.trace_id),
                    span_id=str(stored.span_id),
                    parent_span_id=str(stored.parent_span_id)
                    if stored.parent_span_id
                    else None,
                    sequence=next_sequence,
                    event_type=stored.event_type,
                    payload=_dump(stored.payload),
                    attributes=_dump(stored.attributes),
                    duration_ms=stored.duration_ms,
                    status=stored.status,
                    created_at=stored.created_at.isoformat(),
                )
            )
        return stored

    def list_events(self, trace_id: UUID) -> list[TraceEvent]:
        with self._session() as session:
            rows = session.scalars(
                select(EventRow)
                .where(EventRow.trace_id == str(trace_id))
                .order_by(EventRow.sequence, EventRow.id)
            )
            return [
                TraceEvent(
                    id=UUID(row.id),
                    trace_id=UUID(row.trace_id),
                    span_id=UUID(row.span_id),
                    parent_span_id=UUID(row.parent_span_id)
                    if row.parent_span_id
                    else None,
                    sequence=row.sequence,
                    event_type=row.event_type,
                    payload=_load(row.payload),
                    attributes=_load(row.attributes),
                    duration_ms=row.duration_ms,
                    status=row.status,
                    created_at=datetime.fromisoformat(row.created_at),
                )
                for row in rows
            ]

    def get_tool_claim(self, run_id: UUID, idempotency_key: str) -> ToolClaim:
        """Return typed status, including an explicit absent state."""
        with self._session() as session:
            row = session.get(ToolResultRow, (str(run_id), idempotency_key))
            return _tool_claim(row)

    def claim_tool_execution(
        self,
        run_id: UUID,
        idempotency_key: str,
        *,
        owner_token: str,
        request_fingerprint: str,
        allow_reclaim: bool = True,
    ) -> ToolClaim:
        """Atomically acquire matching retryable work, never a different request."""
        if not request_fingerprint:
            raise ValueError("request_fingerprint is required")
        with self._session.begin() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    insert(ToolResultRow)
                    .values(
                        run_id=str(run_id),
                        idempotency_key=idempotency_key,
                        state="claimed",
                        owner_token=owner_token,
                        request_fingerprint=request_fingerprint,
                    )
                    .on_conflict_do_nothing()
                ),
            )
            if result.rowcount == 1:
                return ToolClaim(
                    state=ToolClaimState.CLAIMED,
                    owner_token=owner_token,
                    request_fingerprint=request_fingerprint,
                )
            row = session.get(ToolResultRow, (str(run_id), idempotency_key))
            if row is not None and row.request_fingerprint != request_fingerprint:
                return ToolClaim(
                    state=ToolClaimState.CONFLICT,
                    request_fingerprint=row.request_fingerprint,
                )
            if (
                row is not None
                and row.state == ToolClaimState.FAILED
                and row.failure_disposition == ToolFailureDisposition.RETRYABLE
                and allow_reclaim
            ):
                reclaimed = cast(
                    CursorResult[Any],
                    session.execute(
                        update(ToolResultRow)
                        .where(
                            ToolResultRow.run_id == str(run_id),
                            ToolResultRow.idempotency_key == idempotency_key,
                            ToolResultRow.state == ToolClaimState.FAILED,
                            ToolResultRow.failure_disposition
                            == ToolFailureDisposition.RETRYABLE,
                        )
                        .values(
                            state=ToolClaimState.CLAIMED,
                            owner_token=owner_token,
                            payload=None,
                            error=None,
                            failure_disposition=None,
                        )
                    ),
                )
                if reclaimed.rowcount == 1:
                    return ToolClaim(
                        state=ToolClaimState.CLAIMED, owner_token=owner_token
                    )
                row = session.get(ToolResultRow, (str(run_id), idempotency_key))
            return _tool_claim(row)

    def complete_tool_result(
        self,
        run_id: UUID,
        idempotency_key: str,
        result: JsonValue,
        *,
        owner_token: str,
        failure_disposition: ToolFailureDisposition = ToolFailureDisposition.RETRYABLE,
    ) -> None:
        """Complete a claim only when its owning worker still controls it."""
        try:
            payload = _dump(sanitize_payload(result, self._secret_values))
        except (TypeError, ValueError):
            self.fail_tool_execution(
                run_id,
                idempotency_key,
                owner_token=owner_token,
                error="result serialization failed",
                disposition=failure_disposition,
            )
            raise
        with self._session.begin() as session:
            update_result = cast(
                CursorResult[Any],
                session.execute(
                    update(ToolResultRow)
                    .where(
                        ToolResultRow.run_id == str(run_id),
                        ToolResultRow.idempotency_key == idempotency_key,
                        ToolResultRow.state == "claimed",
                        ToolResultRow.owner_token == owner_token,
                    )
                    .values(state="completed", payload=payload)
                ),
            )
            if update_result.rowcount != 1:
                raise ValueError(
                    "tool execution was not claimed or is already completed"
                )

    def fail_tool_execution(
        self,
        run_id: UUID,
        idempotency_key: str,
        *,
        owner_token: str,
        error: str,
        disposition: ToolFailureDisposition = ToolFailureDisposition.RETRYABLE,
    ) -> None:
        """Mark a claim with the explicit safety disposition for later callers."""
        with self._session.begin() as session:
            released = cast(
                CursorResult[Any],
                session.execute(
                    update(ToolResultRow)
                    .where(
                        ToolResultRow.run_id == str(run_id),
                        ToolResultRow.idempotency_key == idempotency_key,
                        ToolResultRow.state == ToolClaimState.CLAIMED,
                        ToolResultRow.owner_token == owner_token,
                    )
                    .values(
                        state=ToolClaimState.FAILED,
                        error=error,
                        failure_disposition=disposition,
                    )
                ),
            )
            if released.rowcount != 1:
                raise ValueError("tool claim is not owned by this worker")

    def save_tool_result(
        self, run_id: UUID, idempotency_key: str, result: JsonValue
    ) -> bool:
        """Compatibility helper that saves only when this caller wins the claim."""
        owner_token = uuid4().hex
        claim = self.claim_tool_execution(
            run_id,
            idempotency_key,
            owner_token=owner_token,
            request_fingerprint=f"compatibility:{idempotency_key}",
        )
        if (
            claim.state is not ToolClaimState.CLAIMED
            or claim.owner_token != owner_token
        ):
            return False
        try:
            self.complete_tool_result(
                run_id, idempotency_key, result, owner_token=owner_token
            )
        except ValueError:
            status = self.get_tool_claim(run_id, idempotency_key)
            if (
                status.state is ToolClaimState.COMPLETED
                or status.owner_token != owner_token
            ):
                return False
            raise
        return True

    def get_tool_result(self, run_id: UUID, idempotency_key: str) -> JsonValue | None:
        claim = self.get_tool_claim(run_id, idempotency_key)
        return claim.result if claim.state is ToolClaimState.COMPLETED else None

    def record_approval(
        self, run_id: UUID, *, actor: str, allow: bool, reason: str | None = None
    ) -> None:
        """Persist one immutable approval decision for a run."""
        with self._session.begin() as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    insert(ApprovalRow)
                    .values(
                        run_id=str(run_id),
                        actor=actor,
                        allow=allow,
                        reason=reason,
                        recorded_at=datetime.now(UTC).isoformat(),
                    )
                    .on_conflict_do_nothing()
                ),
            )
            if result.rowcount != 1:
                raise ValueError("approval decision is already recorded")

    def get_approval(self, run_id: UUID) -> dict[str, object] | None:
        with self._session() as session:
            row = session.get(ApprovalRow, str(run_id))
            if row is None:
                return None
            return {
                "actor": row.actor,
                "allow": row.allow,
                "decision": "approved" if row.allow else "denied",
                "reason": row.reason,
                "recorded_at": row.recorded_at,
            }

    def save_evaluation(
        self, run_id: UUID, evaluation_name: str, result: JsonValue
    ) -> None:
        with self._session.begin() as session:
            session.merge(
                EvaluationRow(
                    run_id=str(run_id),
                    evaluation_name=evaluation_name,
                    payload=_dump(result),
                )
            )

    def get_evaluation(self, run_id: UUID, evaluation_name: str) -> JsonValue | None:
        with self._session() as session:
            row = session.get(EvaluationRow, (str(run_id), evaluation_name))
            return _load(row.payload) if row is not None else None


def _configure_sqlite_connection(connection: Any, _: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


def _database_url(settings: Settings) -> str:
    data_dir = settings.data_dir.resolve()
    database_path = settings.database_path.resolve()
    try:
        database_path.relative_to(data_dir)
    except ValueError as error:
        raise ValueError("database path must be inside the data directory") from error
    if database_path.suffix not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("database path must use a SQLite file extension")
    return f"sqlite:///{database_path.as_posix()}"


def _tool_claim(row: ToolResultRow | None) -> ToolClaim:
    if row is None:
        return ToolClaim(state=ToolClaimState.ABSENT)
    return ToolClaim(
        state=ToolClaimState(row.state),
        owner_token=row.owner_token,
        result=_load(row.payload) if row.payload is not None else None,
        error=row.error,
        request_fingerprint=row.request_fingerprint,
        failure_disposition=ToolFailureDisposition(row.failure_disposition)
        if row.failure_disposition is not None
        else None,
    )


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _load(payload: str) -> JsonValue:
    return _JSON_VALUE.validate_python(json.loads(payload))
