"""Offline incident experiments using only Python's standard library.

This is a bounded single-process mobile engine, not the desktop lease/gate engine.
All tools are deterministic synthetic fixtures. A tool result, simulated write,
ordered events and checkpoint share a SQLite transaction. No external side effect
is performed, so an interrupted transaction can safely execute again on resume.
"""

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from uuid import UUID, uuid4


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(UTC).isoformat()


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _invalid(message="The request does not match the supported input contract."):
    raise ApiError(422, "validation_error", message)


def _shape(body, required, optional=()):
    if not isinstance(body, dict) or not set(required) <= body.keys():
        _invalid()
    if body.keys() - set(required) - set(optional):
        _invalid()


def _text(value, maximum=128):
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _identifier(value):
    return _text(value) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", value)


def _query(query, allowed):
    if set(query) - set(allowed):
        _invalid("Unknown query parameter.")
    result = {}
    for name, (default, lower, upper) in allowed.items():
        values = query.get(name, [str(default)])
        if len(values) != 1 or not re.fullmatch(r"[0-9]{1,10}", values[0]):
            _invalid("Query parameters must be bounded integers.")
        value = int(values[0])
        if not lower <= value <= upper:
            _invalid("Query parameter is outside the supported range.")
        result[name] = value
    return result


def _tool_input(name, arguments):
    if name in {"get_service_health", "search_recent_logs", "get_deployment"}:
        return arguments == {}
    if name == "prepare_rollback":
        return (
            isinstance(arguments, dict)
            and set(arguments) == {"deployment_id"}
            and _text(arguments["deployment_id"])
        )
    return False


def _tool_output(name, arguments):
    """The same four synthetic tool operations as tools/incident.py."""
    if name == "get_service_health":
        return {"service": "checkout", "status": "degraded"}
    if name == "search_recent_logs":
        return {
            "entries": [
                {"level": "ERROR", "message": "checkout timeout contacting ledger"}
            ]
        }
    if name == "get_deployment":
        return {"deployment_id": "deploy-2026-09-04-001", "version": "2026.09.04.1"}
    if name == "prepare_rollback":
        return {"deployment_id": arguments["deployment_id"], "prepared": True}
    raise ApiError(
        422, "unknown_tool", "This tool is not in the offline incident catalog."
    )


def _valid_output(name, output):
    if not isinstance(output, dict):
        return False
    if name == "get_service_health":
        return set(output) == {"service", "status"} and all(
            isinstance(v, str) for v in output.values()
        )
    if name == "get_deployment":
        return set(output) == {"deployment_id", "version"} and all(
            isinstance(v, str) for v in output.values()
        )
    if name == "prepare_rollback":
        return (
            set(output) == {"deployment_id", "prepared"}
            and isinstance(output["deployment_id"], str)
            and type(output["prepared"]) is bool
        )
    if name == "search_recent_logs":
        return (
            set(output) == {"entries"}
            and isinstance(output["entries"], list)
            and all(
                isinstance(item, dict)
                and set(item) == {"level", "message"}
                and all(isinstance(v, str) for v in item.values())
                for item in output["entries"]
            )
        )
    return False


class MobileApp:
    def __init__(self, data_dir, resource_dir):
        resources = Path(resource_dir).resolve()
        catalog = json.loads(
            (resources / "data" / "scenarios.json").read_text(encoding="utf-8")
        )
        if (
            catalog.get("format") != "arl-scenarios/v1"
            or _digest(catalog["scenarios"]) != catalog["suite_hash"]
        ):
            raise ValueError("The packaged scenario catalog is incomplete or corrupt")
        self.scenarios = {
            item["scenario"]["id"]: item["scenario"] for item in catalog["scenarios"]
        }
        if len(self.scenarios) != 6:
            raise ValueError("The offline edition requires the six frozen scenarios")
        self.suite_hash = catalog["suite_hash"]
        self.metadata = json.loads(
            (resources / "build.json").read_text(encoding="utf-8")
        )
        data = Path(data_dir).resolve()
        data.mkdir(parents=True, exist_ok=True)
        self.database = data / "agentlab-mobile.db"
        self.lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(run_id, sequence));
                CREATE TABLE IF NOT EXISTS decisions (
                    run_id TEXT NOT NULL REFERENCES runs(id), action_step INTEGER NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(run_id, action_step));
                CREATE TABLE IF NOT EXISTS effects (
                    run_id TEXT NOT NULL REFERENCES runs(id), action_step INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(run_id, action_step));
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self, db):
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def handle(self, method, path, query, body, headers):
        # Authentication, origin, byte limits and static files belong to portable_host.
        try:
            with self.lock, self._db() as db:
                status, result = self._route(db, method, path, query, body)
        except ApiError as error:
            status, result = (
                error.status,
                {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "details": {},
                    }
                },
            )
        except (sqlite3.DatabaseError, ValueError, KeyError, TypeError):
            status, result = (
                409,
                {
                    "error": {
                        "code": "evidence_invalid",
                        "message": "Stored evidence or application data is inconsistent.",
                        "details": {},
                    }
                },
            )
        response_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        }
        if path == "/v1/export" and status == 200:
            response_headers["Content-Disposition"] = (
                f'attachment; filename="trace-{result["run"]["id"]}.json"'
            )
        return (
            status,
            response_headers,
            _json(result).encode("utf-8"),
        )

    def _route(self, db, method, path, query, body):
        if path == "/v1/export" and method == "GET":
            values = query.get("run_id", [])
            if set(query) != {"run_id"} or len(values) != 1:
                _invalid("One run identifier is required.")
            try:
                if str(UUID(values[0])) != values[0]:
                    _invalid("Malformed run identifier.")
            except (ValueError, TypeError):
                _invalid("Malformed run identifier.")
            run = self._load(db, values[0])
            return 200, {
                "run": self._public(db, run),
                "events": [
                    self._public_event(event) for event in self._events(db, run["id"])
                ],
            }
        if path == "/health" and method == "GET":
            db.execute("SELECT 1").fetchone()
            return 200, {"status": "ready", "database": "ready"}
        if path == "/v1/scenarios" and method == "GET":
            _query(query, {})
            return 200, {
                "items": [
                    {
                        "id": scenario["id"],
                        "expected_outcome": scenario["expected_outcome"],
                        "expected_tool_sequence": scenario["expected_tool_sequence"],
                        "faults": scenario["faults"],
                        "approval_required": any(
                            a.get("tool_name") == "prepare_rollback"
                            for a in scenario["actions"]
                        ),
                    }
                    for scenario in sorted(
                        self.scenarios.values(), key=lambda item: item["id"]
                    )
                ]
            }
        if path == "/v1/runs":
            if method == "GET":
                values = _query(query, {"limit": (25, 1, 100)})
                rows = db.execute(
                    "SELECT body FROM runs ORDER BY created_at DESC, id DESC LIMIT ?",
                    (values["limit"],),
                )
                return 200, {
                    "items": [self._public(db, json.loads(row[0])) for row in rows]
                }
            if method == "POST":
                _query(query, {})
                _shape(body, ("scenario_id", "mode"))
                if not _identifier(body["scenario_id"]) or body["mode"] not in (
                    "fragile",
                    "resilient",
                ):
                    _invalid()
                return 201, self._public(
                    db, self._start(db, body["scenario_id"], body["mode"])
                )
        if path == "/v1/evaluations":
            if method == "POST":
                _query(query, {})
                _shape(body, ("suite",))
                if not _identifier(body["suite"]):
                    _invalid()
                if body["suite"] != "incident-response":
                    raise ApiError(
                        404, "suite_not_found", "The offline suite was not found."
                    )
                return 201, self._evaluate(db)
            if method == "GET":
                values = _query(query, {"limit": (10, 1, 100)})
                rows = db.execute(
                    "SELECT body FROM evaluations ORDER BY created_at DESC LIMIT ?",
                    (values["limit"],),
                ).fetchall()
                return 200, {
                    "items": [self._report(db, json.loads(row[0])) for row in rows]
                }
        match = re.fullmatch(
            r"/v1/(runs|evaluations)/([0-9a-f-]{36})(?:/(trace|approvals|resume))?",
            path,
        )
        if match:
            collection, object_id, action = match.groups()
            if str(UUID(object_id)) != object_id:
                _invalid("Malformed identifier.")
            if collection == "evaluations" and action is None and method == "GET":
                _query(query, {})
                row = db.execute(
                    "SELECT body FROM evaluations WHERE id=?", (object_id,)
                ).fetchone()
                if row is None:
                    raise ApiError(
                        404, "evaluation_not_found", "Evaluation was not found."
                    )
                return 200, self._report(db, json.loads(row[0]))
            if collection == "runs":
                run = self._load(db, object_id)
                if action is None and method == "GET":
                    _query(query, {})
                    return 200, self._public(db, run)
                if action == "trace" and method == "GET":
                    values = _query(
                        query,
                        {"after_sequence": (0, 0, 2**31 - 1), "limit": (100, 1, 100)},
                    )
                    after, limit = values["after_sequence"], values["limit"]
                    events = self._events(db, object_id)
                    page = [event for event in events if event["sequence"] > after][
                        :limit
                    ]
                    return 200, {
                        "events": [self._public_event(event) for event in page],
                        "next_after_sequence": page[-1]["sequence"] if page else after,
                        "has_more": bool(page and page[-1]["sequence"] < len(events)),
                    }
                if action == "approvals" and method == "POST":
                    _query(query, {})
                    return 200, self._public(db, self._approve(db, object_id, body))
                if action == "resume" and method == "POST":
                    _query(query, {})
                    if body not in (None, {}):
                        _invalid()
                    if run["status"] in {"failed", "succeeded"}:
                        raise ApiError(
                            409, "run_conflict", "A terminal run cannot be resumed."
                        )
                    return 200, self._public(db, self._advance(db, object_id))
        raise ApiError(
            404, "not_found", "This endpoint is not available in the offline edition."
        )

    def _load(self, db, run_id):
        row = db.execute("SELECT body FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ApiError(404, "run_not_found", "Run was not found.")
        return json.loads(row[0])

    def _save(self, db, run):
        run["updated_at"] = _now()
        db.execute("UPDATE runs SET body=? WHERE id=?", (_json(run), run["id"]))

    def _event(self, db, run, event_type, payload, status="ok"):
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence), 0)+1 FROM events WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
        event = {
            "id": str(uuid4()),
            "trace_id": run["trace_id"],
            "span_id": str(uuid4()),
            "parent_span_id": run["trace_id"],
            "sequence": sequence,
            "event_type": event_type,
            "payload": payload,
            "duration_ms": None,
            "status": status,
            "created_at": _now(),
        }
        db.execute(
            "INSERT INTO events VALUES(?,?,?)", (run["id"], sequence, _json(event))
        )

    def _events(self, db, run_id):
        events = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT body FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]
        if [event["sequence"] for event in events] != list(range(1, len(events) + 1)):
            raise ApiError(
                409, "evidence_invalid", "The trace has missing or unordered events."
            )
        return events

    def _public(self, db, run):
        result = {
            key: run[key]
            for key in (
                "id",
                "trace_id",
                "scenario_id",
                "mode",
                "status",
                "created_at",
                "updated_at",
            )
        }
        result["duration_ms"] = max(
            0.0,
            (
                datetime.fromisoformat(run["updated_at"])
                - datetime.fromisoformat(run["created_at"])
            ).total_seconds()
            * 1000,
        )
        result["attempt_count"] = sum(
            event["event_type"] == "tool.attempt.started"
            for event in self._events(db, run["id"])
        )
        result["approval_required"] = run.get("pending_approval") is not None
        if run.get("pending_approval") is not None:
            result["pending_approval"] = run["pending_approval"]
        if run.get("result") is not None:
            result["result"] = {
                **run["result"],
                "evidence_refs": run["result"].get("evidence_refs", []),
            }
        return result

    @staticmethod
    def _public_event(event):
        allowed = {
            "tool_name",
            "attempt",
            "after_attempt",
            "action_step",
            "step",
            "code",
            "kind",
            "transient",
            "cached",
            "retry_delay_seconds",
            "source",
            "actor",
            "allow",
            "outcome",
            "summary",
            "reason",
            "from_status",
        }
        return {
            **event,
            "payload": {
                key: value for key, value in event["payload"].items() if key in allowed
            },
        }

    def _start(self, db, scenario_id, mode):
        if scenario_id not in self.scenarios:
            raise ApiError(404, "scenario_not_found", "Scenario was not found.")
        now = _now()
        run = {
            "id": str(uuid4()),
            "trace_id": str(uuid4()),
            "scenario_id": scenario_id,
            "mode": mode,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "current_step": 0,
            "next_attempt": 1,
            "last_announced_step": -1,
            "pending_approval": None,
            "result": None,
        }
        with self._transaction(db):
            db.execute("INSERT INTO runs VALUES(?,?,?)", (run["id"], now, _json(run)))
            self._event(db, run, "run.created", {"mode": mode})
        return self._advance(db, run["id"])

    def _fail(self, db, run, code, reason=None):
        run.update(status="failed", pending_approval=None, result={"code": code})
        if reason is not None:
            run["result"]["reason"] = reason
        self._event(db, run, "run.failed", run["result"], "error")

    def _advance(self, db, run_id):
        # Commit after every policy/action attempt. Restart resumes the durable cursor.
        for _ in range(64):
            with self._transaction(db):
                run = self._load(db, run_id)
                if run["status"] in {"failed", "succeeded", "waiting_approval"}:
                    return run
                if run["status"] == "queued":
                    run["status"] = "running"
                    self._event(db, run, "run.running", {})
                scenario = self.scenarios[run["scenario_id"]]
                step = run["current_step"]
                if step >= len(scenario["actions"]):
                    self._fail(db, run, "scenario_exhausted")
                else:
                    action = scenario["actions"][step]
                    if run["last_announced_step"] != step:
                        self._event(
                            db, run, "policy.action", {**action, "action_step": step}
                        )
                        run["last_announced_step"] = step
                    if action["type"] == "finish":
                        run.update(
                            status="succeeded",
                            result={
                                key: action[key]
                                for key in ("outcome", "summary", "evidence_refs")
                            },
                        )
                        self._event(db, run, "run.succeeded", run["result"])
                    elif action["type"] == "fail":
                        self._fail(db, run, action["code"])
                    else:
                        self._attempt(db, run, scenario, action)
                self._save(db, run)
        raise ApiError(
            409, "action_budget_exceeded", "The offline action budget was exhausted."
        )

    def _attempt(self, db, run, scenario, action):
        name, arguments, step = (
            action["tool_name"],
            action["arguments"],
            run["current_step"],
        )
        if not _tool_input(name, arguments):
            self._event(
                db,
                run,
                "tool.preflight.failed",
                {"tool_name": name, "action_step": step, "code": "invalid_input"},
                "error",
            )
            self._fail(db, run, "invalid_input")
            return
        fingerprint = _digest(action)
        if name == "prepare_rollback":
            row = db.execute(
                "SELECT body FROM decisions WHERE run_id=? AND action_step=?",
                (run["id"], step),
            ).fetchone()
            if row is None:
                run["status"] = "waiting_approval"
                run["pending_approval"] = {
                    "action_step": step,
                    "action_fingerprint": fingerprint,
                    "tool_name": name,
                    "arguments": arguments,
                }
                self._event(
                    db,
                    run,
                    "run.waiting_approval",
                    {"action_step": step, "tool_name": name},
                )
                return
            approval = json.loads(row[0])
            if (
                approval["action_fingerprint"] != fingerprint
                or approval["allow"] is not True
            ):
                self._fail(db, run, "approval_mismatch")
                return
        attempt = run["next_attempt"]
        context = {"tool_name": name, "action_step": step, "attempt": attempt}
        self._event(db, run, "tool.attempt.started", context)
        fault = next(
            (
                item
                for item in scenario["faults"]
                if item["tool_name"] == name and item["attempt"] == attempt
            ),
            None,
        )
        kind = fault["type"] if fault else None
        if fault:
            self._event(db, run, "fault.injected", {**context, "kind": kind}, "error")
        code = {
            "timeout": "tool_timeout",
            "rate_limit": "rate_limit",
            "tool_error": "tool_error",
        }.get(kind)
        transient = code is not None
        raw = (
            None
            if transient
            else (
                {"__arl_malformed_output__": True}
                if kind == "malformed_output"
                else _tool_output(name, arguments)
            )
        )
        if not transient and not _valid_output(name, raw):
            code = "invalid_output"
            self._event(
                db,
                run,
                "tool.output.validation_failed",
                {**context, "code": code, "source": "output_model"},
                "error",
            )
        if code:
            retry = transient and run["mode"] == "resilient" and attempt < 3
            self._event(
                db,
                run,
                "tool.attempt.failed",
                {**context, "code": code, "transient": transient},
                "error",
            )
            if retry:
                run["next_attempt"] += 1
            else:
                self._fail(db, run, code)
            return
        if name == "prepare_rollback":
            existing = db.execute(
                "SELECT fingerprint,body FROM effects WHERE run_id=? AND action_step=?",
                (run["id"], step),
            ).fetchone()
            if existing and existing[0] != fingerprint:
                raise ApiError(
                    409,
                    "run_conflict",
                    "The stored write belongs to a different action.",
                )
            if existing:
                raw = json.loads(existing[1])
            else:
                db.execute(
                    "INSERT INTO effects VALUES(?,?,?,?)",
                    (run["id"], step, fingerprint, _json(raw)),
                )
        self._event(db, run, "tool.attempt.succeeded", {**context, "output": raw})
        run.update(current_step=step + 1, next_attempt=1, pending_approval=None)
        self._event(db, run, "run.checkpointed", {"step": step + 1})

    def _approve(self, db, run_id, body):
        _shape(
            body, ("actor", "allow", "action_step", "action_fingerprint"), ("reason",)
        )
        if (
            not _text(body["actor"])
            or type(body["allow"]) is not bool
            or type(body["action_step"]) is not int
            or not 0 <= body["action_step"] < 100
            or not isinstance(body["action_fingerprint"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", body["action_fingerprint"])
            or (
                body.get("reason") is not None
                and (not isinstance(body["reason"], str) or len(body["reason"]) > 500)
            )
        ):
            _invalid()
        normalized = {**body, "reason": body.get("reason")}
        with self._transaction(db):
            run = self._load(db, run_id)
            previous = db.execute(
                "SELECT body FROM decisions WHERE run_id=? AND action_step=?",
                (run_id, body["action_step"]),
            ).fetchone()
            if previous:
                if json.loads(previous[0]) != normalized:
                    raise ApiError(
                        409,
                        "run_conflict",
                        "A different decision already exists for this action.",
                    )
            else:
                pending = run.get("pending_approval")
                if (
                    run["status"] != "waiting_approval"
                    or pending is None
                    or pending["action_step"] != body["action_step"]
                    or pending["action_fingerprint"] != body["action_fingerprint"]
                ):
                    raise ApiError(
                        409,
                        "run_conflict",
                        "The approval does not match the current pending action.",
                    )
                db.execute(
                    "INSERT INTO decisions VALUES(?,?,?)",
                    (run_id, body["action_step"], _json(normalized)),
                )
                self._event(db, run, "approval.recorded", normalized)
                run["pending_approval"] = None
                if body["allow"]:
                    run["status"] = "queued"
                else:
                    self._event(db, run, "approval.denied", normalized, "error")
                    self._fail(db, run, "approval_denied", body.get("reason"))
                self._save(db, run)
        return self._advance(db, run_id)

    def _evidence(self, db, run):
        events = self._events(db, run["id"])
        return _digest({"run": run, "events": events})

    def _evaluate(self, db):
        evaluation = {"evaluation_id": str(uuid4()), "generated_at": _now(), "runs": []}
        for mode in ("fragile", "resilient"):
            for scenario in sorted(
                self.scenarios.values(), key=lambda item: item["id"]
            ):
                run = self._start(db, scenario["id"], mode)
                if (
                    run["status"] == "waiting_approval"
                    and scenario["approval_supplied"]
                ):
                    # Reload from durable storage before supplying the suite's declared approval.
                    with self._db() as reopened:
                        run = self._load(reopened, run["id"])
                        pending = run["pending_approval"]
                        run = self._approve(
                            reopened,
                            run["id"],
                            {
                                "actor": "offline-evaluator",
                                "allow": True,
                                "action_step": pending["action_step"],
                                "action_fingerprint": pending["action_fingerprint"],
                                "reason": "Approval supplied by the frozen synthetic suite",
                            },
                        )
                evaluation["runs"].append(
                    {"id": run["id"], "evidence_hash": self._evidence(db, run)}
                )
        with self._transaction(db):
            db.execute(
                "INSERT INTO evaluations VALUES(?,?,?)",
                (
                    evaluation["evaluation_id"],
                    evaluation["generated_at"],
                    _json(evaluation),
                ),
            )
        return self._report(db, evaluation)

    def _case(self, db, run):
        scenario = self.scenarios[run["scenario_id"]]
        events = self._events(db, run["id"])
        by_type = lambda kind: [
            event["payload"] for event in events if event["event_type"] == kind
        ]
        actions = by_type("policy.action")
        actual = [item["tool_name"] for item in actions if item["type"] == "call_tool"]
        started, successes, failures = (
            by_type("tool.attempt.started"),
            by_type("tool.attempt.succeeded"),
            by_type("tool.attempt.failed"),
        )
        identities = lambda items: Counter(
            (item["action_step"], item["tool_name"], item["attempt"]) for item in items
        )
        if identities(started) != identities(successes + failures) or any(
            count != 1 for count in identities(started).values()
        ):
            raise ApiError(
                409, "evidence_invalid", "The tool attempt evidence is incomplete."
            )
        observed = (run.get("result") or {}).get(
            "outcome", (run.get("result") or {}).get("code", run["status"])
        )
        correct = observed == scenario["expected_outcome"]
        faults = by_type("fault.injected")
        expected_faults = Counter(
            (item["tool_name"], item["attempt"], item["type"])
            for item in scenario["faults"]
        )
        if (
            Counter(
                (item["tool_name"], item["attempt"], item["kind"]) for item in faults
            )
            != expected_faults
        ):
            raise ApiError(
                409, "evidence_invalid", "The declared fault evidence is incomplete."
            )
        transient = [
            item
            for item in faults
            if item["kind"] in {"timeout", "rate_limit", "tool_error"}
        ]
        recovered = sum(
            correct
            and any(
                success["action_step"] == fault["action_step"]
                and success["tool_name"] == fault["tool_name"]
                and success["attempt"] > fault["attempt"]
                for success in successes
            )
            for fault in transient
        )
        retries = sum(
            max(0, count - 1)
            for count in Counter(item["action_step"] for item in started).values()
        )
        expected = scenario["expected_tool_sequence"]
        previous = [0] * (len(actual) + 1)
        for name in expected:
            current = [0]
            for index, other in enumerate(actual, 1):
                current.append(
                    previous[index - 1] + 1
                    if name == other
                    else max(previous[index], current[-1])
                )
            previous = current
        score = (
            Fraction(previous[-1], max(len(expected), len(actual)))
            if expected or actual
            else Fraction(1)
        )
        public = {
            "scenario_id": run["scenario_id"],
            "mode": run["mode"],
            "run_id": run["id"],
            "final_status": run["status"],
            "expected_outcome": scenario["expected_outcome"],
            "observed_outcome": observed,
            "correct": correct,
            "attempt_count": len(started),
            "retry_attempt_count": retries,
        }
        return {
            "public": public,
            "sequence_score": score,
            "recovered": recovered,
            "transient": len(transient),
            "accepted": len(successes),
            "invalid": sum(
                not _valid_output(item["tool_name"], item["output"])
                for item in successes
            ),
            "rejected": len(by_type("tool.output.validation_failed")),
            "malformed": sum(item["kind"] == "malformed_output" for item in faults),
            "unnecessary": sum((Counter(actual) - Counter(expected)).values()),
            "latency": self._public(db, run)["duration_ms"],
        }

    def _report(self, db, evaluation):
        cases = {"fragile": [], "resilient": []}
        for reference in evaluation["runs"]:
            run = self._load(db, reference["id"])
            if self._evidence(db, run) != reference["evidence_hash"]:
                raise ApiError(
                    409, "evidence_invalid", "The evaluation's run evidence changed."
                )
            cases[run["mode"]].append(self._case(db, run))
        modes = {}
        for mode, items in cases.items():
            if len(items) != len(self.scenarios) or {
                item["public"]["scenario_id"] for item in items
            } != set(self.scenarios):
                raise ApiError(
                    409, "evidence_invalid", "The evaluation is missing scenario cases."
                )
            count = len(items)
            total = lambda key, source=items: sum(item[key] for item in source)
            correct = sum(item["public"]["correct"] for item in items)
            latency = sorted(item["latency"] for item in items)
            metrics = {
                "case_correct_count": correct,
                "case_count": count,
                "task_correctness_rate": correct / count,
                "terminal_success_count": sum(
                    item["public"]["final_status"] == "succeeded" for item in items
                ),
                "tool_sequence_accuracy": float(
                    sum((item["sequence_score"] for item in items), Fraction()) / count
                ),
                "tool_sequence_case_count": count,
                "recovered_transient_fault_count": total("recovered"),
                "transient_fault_count": total("transient"),
                "recovery_rate": total("recovered") / total("transient")
                if total("transient")
                else None,
                "invalid_output_accepted_count": total("invalid"),
                "accepted_output_count": total("accepted"),
                "invalid_output_rate": total("invalid") / total("accepted")
                if total("accepted")
                else 0.0,
                "invalid_output_detected_count": total("rejected"),
                "invalid_output_rejected_count": total("rejected"),
                "malformed_fault_injected_count": total("malformed"),
                "unnecessary_call_count": total("unnecessary"),
                "retry_attempt_count": sum(
                    item["public"]["retry_attempt_count"] for item in items
                ),
                "p50_latency_ms": latency[math.ceil(0.50 * count) - 1],
                "p95_latency_ms": latency[math.ceil(0.95 * count) - 1],
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "estimated_cost_usd": "0",
            }
            modes[mode] = {
                "mode": mode,
                "metrics": metrics,
                "cases": [item["public"] for item in items],
            }
        fragile, resilient = modes["fragile"]["metrics"], modes["resilient"]["metrics"]
        comparison = {
            f"{name}_delta": resilient[name] - fragile[name]
            for name in (
                "task_correctness_rate",
                "tool_sequence_accuracy",
                "invalid_output_rate",
                "unnecessary_call_count",
                "retry_attempt_count",
                "p50_latency_ms",
                "p95_latency_ms",
            )
        }
        comparison["recovery_rate_delta"] = (
            resilient["recovery_rate"] - fragile["recovery_rate"]
            if fragile["recovery_rate"] is not None
            and resilient["recovery_rate"] is not None
            else None
        )
        comparison["fragile_worse_recovery_scenarios"] = [
            right["public"]["scenario_id"]
            for left, right in zip(cases["fragile"], cases["resilient"], strict=True)
            if right["recovered"] > left["recovered"]
        ]
        return {
            "evaluation_id": evaluation["evaluation_id"],
            "generated_at": evaluation["generated_at"],
            "provenance": {
                "report_version": "mobile/v1",
                "grader_version": "mobile-exact/v1",
                "suite_hash": self.suite_hash,
                "git_revision": self.metadata["git_revision"],
                "git_dirty": self.metadata.get("git_dirty", False),
                "package_version": self.metadata["version"],
            },
            "modes": modes,
            "comparison": comparison,
        }


def create_app(data_dir: str, resource_dir: str):
    return MobileApp(data_dir, resource_dir)
