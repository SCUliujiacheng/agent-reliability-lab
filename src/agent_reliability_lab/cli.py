"""Thin command-line adapter for runs, evaluations, gates, and trace export."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID

import typer
from pydantic import ValidationError

from agent_reliability_lab.config import Settings
from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.models import EvaluationReport
from agent_reliability_lab.evaluation.runner import (
    compare_modes,
    run_evaluation,
)
from agent_reliability_lab.runtime.service import RunService
from agent_reliability_lab.scenarios.loader import load_scenario
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.telemetry.recorder import TraceRecorder
from agent_reliability_lab.tools.gateway import ToolGateway
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@app.command("run")
def run_command(
    scenario_path: Annotated[Path, typer.Argument()],
    mode: Annotated[str, typer.Option()] = "resilient",
    database: Annotated[Path, typer.Option()] = Path(".arl-data/runs.db"),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run one frozen scenario through the durable runtime."""
    try:
        if mode not in {"fragile", "resilient"}:
            raise ValueError("mode must be fragile or resilient")
        scenario = load_scenario(scenario_path)
        service = _persistent_service(database, scenario)
        run = asyncio.run(service.start(scenario.id, mode))  # type: ignore[arg-type]
        if run.pending_approval and scenario.approval_supplied:
            if run.pending_action_fingerprint is None:
                raise RuntimeError("waiting run has no approval fingerprint")
            service = _persistent_service(database, scenario)
            run = asyncio.run(
                service.approve(
                    run.id,
                    actor="cli-reviewer",
                    allow=True,
                    expected_action_step=run.current_step,
                    expected_action_fingerprint=run.pending_action_fingerprint,
                )
            )
        payload = {
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "scenario_id": run.scenario_id,
            "mode": run.mode,
            "status": run.status,
            "result": run.result,
        }
        _emit(payload, json_output, f"{run.scenario_id}: {run.status}")
        if run.status == "failed":
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as error:  # noqa: BLE001 - CLI translates domain/I/O errors.
        _fail(error, json_output)


@app.command("eval")
def eval_command(
    suite: Annotated[Path, typer.Argument()],
    output: Annotated[Path | None, typer.Option()] = None,
    baseline_output: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Evaluate the same frozen suite in fragile and resilient modes."""
    try:
        report = asyncio.run(run_evaluation(suite))
        if output is not None:
            _write_report(output, report)
        if baseline_output is not None:
            _write_report(baseline_output, report)
        payload = report.model_dump(mode="json")
        _emit(
            payload,
            json_output,
            _evaluation_table(report),
        )
    except Exception as error:  # noqa: BLE001 - CLI translates domain/I/O errors.
        _fail(error, json_output)


@app.command("compare")
def compare_command(
    report_path: Annotated[Path, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Display resilient-minus-fragile metrics from one strict report."""
    try:
        comparison = compare_modes(_read_report(report_path))
        _emit(
            comparison.model_dump(mode="json"),
            json_output,
            json.dumps(comparison.model_dump(mode="json"), indent=2),
        )
    except Exception as error:  # noqa: BLE001 - CLI translates domain/I/O errors.
        _fail(error, json_output)


@app.command("gate")
def gate_command(
    report_path: Annotated[Path, typer.Argument()],
    baseline: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Enforce exact reliability and optional baseline regression thresholds."""
    try:
        result = enforce_gate(
            _read_report(report_path),
            baseline=_read_report(baseline) if baseline is not None else None,
        )
        _emit(
            result.model_dump(mode="json"),
            json_output,
            "PASS" if result.passed else f"FAIL: {result.failures}",
        )
        if result.infrastructure_errors:
            raise typer.Exit(2)
        if not result.passed:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as error:  # noqa: BLE001 - CLI translates domain/I/O errors.
        _fail(error, json_output)


@app.command("export-trace")
def export_trace_command(
    run_id: Annotated[str, typer.Argument()],
    database: Annotated[Path, typer.Option()] = Path(".arl-data/runs.db"),
    output: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Export ordered sanitized trace events for one persisted run."""
    try:
        store = _store(database)
        run = store.get_run(UUID(run_id))
        if run is None:
            raise ValueError("run not found")
        payload = {
            "run_id": str(run.id),
            "trace_id": str(run.trace_id),
            "events": [
                event.model_dump(mode="json")
                for event in store.list_events(run.trace_id)
            ],
        }
        if output is not None:
            _write_json(output, payload)
        _emit(payload, json_output, f"exported {len(payload['events'])} events")
    except Exception as error:  # noqa: BLE001 - CLI translates domain/I/O errors.
        _fail(error, json_output)


def _persistent_service(database: Path, scenario: Any) -> RunService:
    store = _store(database)
    recorder = TraceRecorder(store)
    backend = IncidentBackend()
    gateway = ToolGateway(store, recorder, incident_registry(backend))
    return RunService(store, recorder, gateway, {scenario.id: scenario}.__getitem__)


def _store(database: Path) -> SQLiteRunStore:
    resolved = database.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=resolved.parent, database_path=resolved)
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    return store


def _read_report(path: Path) -> EvaluationReport:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: _raise_non_finite(value),
        )
        return EvaluationReport.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ValueError(f"invalid evaluation report: {path}") from error


def _raise_non_finite(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON value: {value}")


def _write_report(path: Path, report: EvaluationReport) -> None:
    _write_json(path, report.model_dump(mode="json"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _emit(payload: object, json_output: bool, human: str) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    else:
        typer.echo(human)


def _fail(error: Exception, json_output: bool) -> NoReturn:
    payload = {
        "error": {
            "code": "invalid_input",
            "message": str(error),
            "details": {},
        }
    }
    if json_output:
        _emit(payload, True, "")
    else:
        typer.echo(f"error: {error}", err=True)
    raise typer.Exit(2)


def _evaluation_table(report: EvaluationReport) -> str:
    lines = ["mode      correctness  recovery  invalid accepted"]
    for mode, result in report.modes.items():
        metrics = result.metrics
        recovery = (
            "n/a" if metrics.recovery_rate is None else f"{metrics.recovery_rate:.3f}"
        )
        lines.append(
            f"{mode:<10}{metrics.task_correctness_rate:<13.3f}"
            f"{recovery:<10}{metrics.invalid_output_accepted_count}"
        )
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    app()
