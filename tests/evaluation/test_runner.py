"""Golden-suite execution, evidence, and provenance tests."""

import json
from pathlib import Path

import pytest

from agent_reliability_lab.config import Settings
from agent_reliability_lab.domain.runs import Run
from agent_reliability_lab.evaluation.gate import enforce_gate
from agent_reliability_lab.evaluation.graders import aggregate_cases
from agent_reliability_lab.evaluation.models import EvaluationReport
from agent_reliability_lab.evaluation.runner import (
    EvaluationInfrastructureError,
    accepted_output_evidence,
    build_suite_manifest,
    compare_modes,
    count_invalid_accepted_outputs,
    run_evaluation,
    stable_report_projection,
    suite_sha256,
)
from agent_reliability_lab.scenarios.loader import load_scenario
from agent_reliability_lab.storage.store import SQLiteRunStore
from agent_reliability_lab.tools.incident import IncidentBackend, incident_registry

SUITE = Path(__file__).parents[2] / "scenarios" / "incident-response"


def test_suite_hash_uses_exact_bytes_and_sorted_posix_relative_paths(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    (suite / "nested").mkdir(parents=True)
    first = suite / "z.yaml"
    second = suite / "nested" / "a.yaml"
    first.write_text(
        "id: z\nversion: 1\nactions: [{type: fail, code: x, explanation: x}]\nexpected_outcome: x\n",
        encoding="utf-8",
    )
    second.write_text(
        "id: a\nversion: 1\nactions: [{type: fail, code: x, explanation: x}]\nexpected_outcome: x\n",
        encoding="utf-8",
    )

    manifest = build_suite_manifest(suite)
    original = suite_sha256(manifest)
    first.write_bytes(first.read_bytes() + b"\n")

    assert [entry.relative_path for entry in manifest] == ["nested/a.yaml", "z.yaml"]
    assert suite_sha256(build_suite_manifest(suite)) != original


@pytest.mark.asyncio
async def test_frozen_suite_produces_credible_fragile_resilient_contrast() -> None:
    report = await run_evaluation(SUITE, modes=("fragile", "resilient"))

    resilient = report.modes["resilient"]
    fragile = report.modes["fragile"]
    assert resilient.metrics.task_correctness_rate >= 0.80
    assert resilient.metrics.recovery_rate is not None
    assert resilient.metrics.recovery_rate >= 0.75
    assert resilient.metrics.invalid_output_rate == 0.0
    comparison = compare_modes(report)
    assert set(comparison.fragile_worse_recovery_scenarios) >= {
        "timeout-recovery",
        "rate-limit-recovery",
    }
    assert fragile.metrics.recovered_transient_fault_count == 0


@pytest.mark.asyncio
async def test_fault_evidence_retries_and_safe_failures_are_measured_from_trace() -> (
    None
):
    report = await run_evaluation(SUITE, modes=("fragile", "resilient"))
    cases = {case.scenario_id: case for case in report.modes["resilient"].cases}

    timeout = cases["timeout-recovery"]
    assert timeout.declared_faults == timeout.observed_faults
    assert timeout.attempt_count == 4
    assert timeout.retry_attempt_count == 1
    assert timeout.recovered_transient_fault_count == 1
    assert {fault.action_step for fault in timeout.observed_faults} == {1}
    assert [
        (attempt.attempt, attempt.status)
        for attempt in timeout.attempt_evidence
        if attempt.action_step == 1
    ] == [
        (1, "failed"),
        (2, "succeeded"),
    ]

    malformed = cases["malformed-output-rejected"]
    assert malformed.malformed_fault_injected_count == 1
    assert malformed.invalid_output_detected_count == 1
    assert malformed.invalid_output_rejected_count == 1
    assert malformed.invalid_output_accepted_count == 0
    assert malformed.accepted_output_count == 1
    assert len(malformed.output_validation_failures) == 1
    assert malformed.output_validation_failures[0].source == "output_model"
    assert malformed.correct is True

    permanent = cases["permanent-invalid-input"]
    assert permanent.attempt_count == 0
    assert permanent.observed_outcome == "invalid_input"
    assert permanent.correct is True


@pytest.mark.asyncio
async def test_approval_case_reconstructs_and_executes_write_once() -> None:
    report = await run_evaluation(SUITE, modes=("resilient",))
    approval = next(
        case
        for case in report.modes["resilient"].cases
        if case.scenario_id == "approval-reconstruction"
    )

    assert approval.approval_reconstructed is True
    assert approval.pre_pause_write_execution_count == 0
    assert approval.write_execution_count == 1
    assert approval.logical_tool_call_count == 2


@pytest.mark.asyncio
async def test_accepted_context_outputs_are_independently_schema_validated(
    tmp_path: Path,
) -> None:
    report = await run_evaluation(SUITE, modes=("fragile", "resilient"))
    resilient = report.modes["resilient"]
    normal = next(
        case for case in resilient.cases if case.scenario_id == "normal-success"
    )
    scenario = load_scenario(SUITE / "normal-success.yaml")
    backend = IncidentBackend()
    registry = incident_registry(backend)
    polluted_run = Run.new(scenario.id, "resilient").model_copy(
        update={"context": {"tool_results": {"2": {"not": "a deployment"}}}}
    )
    store = SQLiteRunStore.from_settings(
        Settings(data_dir=tmp_path, database_path=tmp_path / "polluted.db")
    )
    store.create_schema()
    polluted_run = store.save_run(polluted_run)
    reconstructed = store.get_run(polluted_run.id)
    assert reconstructed is not None
    evidence = accepted_output_evidence(reconstructed, scenario, registry)
    assert count_invalid_accepted_outputs(evidence, registry) == 1
    store.close()

    polluted = normal.model_copy(
        update={
            "accepted_outputs": (
                *normal.accepted_outputs[:2],
                *evidence,
            ),
            "invalid_output_accepted_count": 1,
        }
    )
    cases = tuple(
        polluted if case.scenario_id == normal.scenario_id else case
        for case in resilient.cases
    )
    changed_mode = resilient.model_copy(
        update={"cases": cases, "metrics": aggregate_cases(cases)}
    )
    polluted_report = report.model_copy(
        update={"modes": {**report.modes, "resilient": changed_mode}}
    )
    polluted_report = polluted_report.model_copy(
        update={"comparison": compare_modes(polluted_report)}
    )

    result = enforce_gate(polluted_report)

    assert result.comparable is True
    assert result.passed is False
    assert "invalid_output_rate" in result.failures


@pytest.mark.asyncio
async def test_each_case_uses_a_fresh_store_and_report_contains_no_absolute_paths(
    tmp_path: Path,
) -> None:
    report = await run_evaluation(SUITE, modes=("resilient",))
    serialized = report.model_dump_json()

    assert all(case.store_run_count == 1 for case in report.modes["resilient"].cases)
    assert str(SUITE.resolve()) not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert report.provenance.token_estimator == "scripted-no-provider-v1"
    assert all(
        case.estimated_input_tokens == 0 for case in report.modes["resilient"].cases
    )


@pytest.mark.asyncio
async def test_mid_run_suite_mutation_is_an_infrastructure_error(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    for source in sorted(SUITE.glob("*.yaml"))[:2]:
        (suite / source.name).write_bytes(source.read_bytes())
    changed = False

    def mutate_after_first_case(_: object) -> None:
        nonlocal changed
        if not changed:
            target = next(suite.glob("*.yaml"))
            target.write_bytes(target.read_bytes() + b"\n")
            changed = True

    with pytest.raises(EvaluationInfrastructureError, match="suite changed"):
        await run_evaluation(
            suite,
            modes=("resilient",),
            case_observer=mutate_after_first_case,
        )


@pytest.mark.asyncio
async def test_report_round_trip_is_strict_and_stable_projection_omits_volatile_fields() -> (
    None
):
    first = await run_evaluation(SUITE, modes=("resilient",))
    second = await run_evaluation(SUITE, modes=("resilient",))

    assert stable_report_projection(first) == stable_report_projection(second)
    payload = json.loads(first.model_dump_json())
    payload["surprise"] = True
    with pytest.raises(ValueError):
        EvaluationReport.model_validate(payload)
