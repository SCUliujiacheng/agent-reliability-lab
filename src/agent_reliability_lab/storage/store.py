"""Transactional SQLite persistence for runs and audit records."""

import json
from typing import Any
from uuid import UUID

from pydantic import JsonValue, TypeAdapter
from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import TraceEvent

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class Base(DeclarativeBase):
    """Base declarative mapping for persistent reliability-lab records."""


class RunRow(Base):
    """SQL representation of an immutable run snapshot."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class EventRow(Base):
    """Ordered trace event belonging to a persisted run trace."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trace_id: Mapped[str] = mapped_column(
        ForeignKey("runs.trace_id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ToolResultRow(Base):
    """Cached, idempotent tool result for one run."""

    __tablename__ = "tool_results"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class ApprovalRow(Base):
    """Human approval decision linked to a run."""

    __tablename__ = "approvals"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class EvaluationRow(Base):
    """Named evaluation outcome linked to a run."""

    __tablename__ = "evaluations"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    evaluation_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)


class SQLiteRunStore:
    """Synchronous, durable store with atomic writes and trace ordering."""

    def __init__(self, database_url: str) -> None:
        self._engine = create_engine(database_url)
        self._session = sessionmaker(self._engine)
        event.listen(self._engine, "connect", _configure_sqlite_connection)

    def create_schema(self) -> None:
        """Create all persistence tables when they do not already exist."""
        Base.metadata.create_all(self._engine)

    def save_run(self, run: Run) -> None:
        """Persist the latest immutable snapshot of a run."""
        with self._session.begin() as session:
            session.merge(
                RunRow(id=str(run.id), trace_id=str(run.trace_id), payload=_dump(run))
            )

    def get_run(self, run_id: UUID) -> Run | None:
        """Retrieve a run snapshot by id."""
        with self._session() as session:
            row = session.get(RunRow, str(run_id))
            return Run.model_validate_json(row.payload) if row is not None else None

    def append_event(self, event_value: TraceEvent) -> TraceEvent:
        """Atomically append an event and assign its trace-local sequence number."""
        with self._session.begin() as session:
            next_sequence = session.scalar(
                select(func.coalesce(func.max(EventRow.sequence), 0) + 1).where(
                    EventRow.trace_id == str(event_value.trace_id)
                )
            )
            sequence = int(next_sequence or 1)
            stored = event_value.model_copy(update={"sequence": sequence})
            session.add(
                EventRow(
                    id=str(stored.id),
                    trace_id=str(stored.trace_id),
                    sequence=sequence,
                    payload=_dump(stored),
                )
            )
        return stored

    def list_events(self, trace_id: UUID) -> list[TraceEvent]:
        """Return trace events in their committed order."""
        with self._session() as session:
            rows = session.scalars(
                select(EventRow)
                .where(EventRow.trace_id == str(trace_id))
                .order_by(EventRow.sequence)
            )
            return [TraceEvent.model_validate_json(row.payload) for row in rows]

    def save_tool_result(
        self, run_id: UUID, idempotency_key: str, result: JsonValue
    ) -> None:
        """Save a tool result for later idempotent lookup."""
        with self._session.begin() as session:
            session.merge(
                ToolResultRow(
                    run_id=str(run_id),
                    idempotency_key=idempotency_key,
                    payload=_dump(result),
                )
            )

    def get_tool_result(self, run_id: UUID, idempotency_key: str) -> JsonValue | None:
        """Return a cached tool result, if one exists."""
        with self._session() as session:
            row = session.get(ToolResultRow, (str(run_id), idempotency_key))
            return _load(row.payload) if row is not None else None

    def record_approval(
        self, run_id: UUID, approval_id: str, decision: str, details: JsonValue
    ) -> None:
        """Persist a human decision and its structured audit details."""
        with self._session.begin() as session:
            session.merge(
                ApprovalRow(
                    run_id=str(run_id),
                    approval_id=approval_id,
                    payload=_dump({"decision": decision, "details": details}),
                )
            )

    def get_approval(self, run_id: UUID, approval_id: str) -> JsonValue | None:
        """Return a recorded approval decision, if one exists."""
        with self._session() as session:
            row = session.get(ApprovalRow, (str(run_id), approval_id))
            return _load(row.payload) if row is not None else None

    def save_evaluation(
        self, run_id: UUID, evaluation_name: str, result: JsonValue
    ) -> None:
        """Persist a named evaluation result."""
        with self._session.begin() as session:
            session.merge(
                EvaluationRow(
                    run_id=str(run_id),
                    evaluation_name=evaluation_name,
                    payload=_dump(result),
                )
            )

    def get_evaluation(self, run_id: UUID, evaluation_name: str) -> JsonValue | None:
        """Return a named evaluation result, if one exists."""
        with self._session() as session:
            row = session.get(EvaluationRow, (str(run_id), evaluation_name))
            return _load(row.payload) if row is not None else None


def _configure_sqlite_connection(connection: Any, _: Any) -> None:
    """Enable SQLite durability and referential integrity for every connection."""
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def _dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load(payload: str) -> JsonValue:
    return _JSON_VALUE.validate_python(json.loads(payload))
