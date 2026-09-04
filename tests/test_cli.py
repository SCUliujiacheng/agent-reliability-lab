"""Installed CLI contracts, JSON purity, and exit codes."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import agent_reliability_lab.evaluation.runner as evaluation_runner
from agent_reliability_lab.cli import app
from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.storage.models import TraceEvent
from agent_reliability_lab.storage.store import SQLiteRunStore

SUITE = Path(__file__).parent.parent / "scenarios" / "incident-response"
RUNNER = CliRunner()


def test_eval_json_stdout_is_exactly_one_object(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = RUNNER.invoke(app, ["eval", str(SUITE), "--output", str(output), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload == json.loads(output.read_text(encoding="utf-8"))
    assert result.stdout.count("\n") == 1


def test_gate_exit_codes_distinguish_failure_from_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evaluation_runner,
        "_git_provenance",
        lambda _location: ("f" * 40, False),
    )
    report_path = tmp_path / "report.json"
    evaluated = RUNNER.invoke(
        app, ["eval", str(SUITE), "--output", str(report_path), "--json"]
    )
    assert evaluated.exit_code == 0

    passed = RUNNER.invoke(app, ["gate", str(report_path), "--json"])
    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["passed"] is True

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"not": "a report", "value": NaN}', encoding="utf-8")
    failed = RUNNER.invoke(app, ["gate", str(invalid), "--json"])
    assert failed.exit_code == 2
    assert json.loads(failed.stdout)["error"]["code"] == "invalid_input"

    missing = RUNNER.invoke(app, ["gate", str(tmp_path / "missing.json"), "--json"])
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["error"]["code"] == "invalid_input"


def test_run_compare_and_export_trace_smoke(tmp_path: Path) -> None:
    database = tmp_path / "smoke.db"
    scenario = SUITE / "normal-success.yaml"
    run = RUNNER.invoke(
        app,
        ["run", str(scenario), "--database", str(database), "--json"],
    )
    assert run.exit_code == 0, run.output
    run_payload = json.loads(run.stdout)

    exported = RUNNER.invoke(
        app,
        [
            "export-trace",
            run_payload["run_id"],
            "--database",
            str(database),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["events"]

    report_path = tmp_path / "report.json"
    evaluated = RUNNER.invoke(
        app, ["eval", str(SUITE), "--output", str(report_path), "--json"]
    )
    assert evaluated.exit_code == 0
    compared = RUNNER.invoke(app, ["compare", str(report_path), "--json"])
    assert compared.exit_code == 0
    assert len(json.loads(compared.stdout)["fragile_worse_recovery_scenarios"]) >= 2


def test_export_trace_never_returns_persisted_compound_credentials(
    tmp_path: Path,
) -> None:
    """A compound credential surviving storage and CLI export must fail."""
    database = tmp_path / "redacted-export.db"
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=database)
    )
    store.create_schema()
    run = Run.new("incident-timeout", "resilient")
    store.save_run(run)
    store.append_event(
        TraceEvent.new(
            run.trace_id,
            "provider.response",
            {
                "access_token": "access-value",
                "refreshToken": "refresh-value",
                "client-secret": "client-value",
                "token_count": 17,
            },
            attributes={
                "oauth_client_secret": "attribute-value",
                "prompt_tokens": 11,
            },
        )
    )
    store.close()

    exported = RUNNER.invoke(
        app,
        ["export-trace", str(run.id), "--database", str(database), "--json"],
    )

    assert exported.exit_code == 0, exported.output
    event = json.loads(exported.stdout)["events"][0]
    assert event["payload"] == {
        "access_token": "[REDACTED]",
        "refreshToken": "[REDACTED]",
        "client-secret": "[REDACTED]",
        "token_count": 17,
    }
    assert event["attributes"] == {
        "oauth_client_secret": "[REDACTED]",
        "prompt_tokens": 11,
    }
