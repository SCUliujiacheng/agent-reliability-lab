"""Immutable evaluation report and public projection contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app
from agent_reliability_lab.evaluation.models import EvaluationReport
from agent_reliability_lab.storage.store import SQLiteRunStore

from .conftest import (
    PROJECT_ROOT,
    assert_error,
    assert_safe_http_json,
)


def _report(
    *, evaluation_id: int = 3, generated_at: datetime | None = None
) -> EvaluationReport:
    report = EvaluationReport.model_validate_json(
        (PROJECT_ROOT / "benchmarks" / "baseline-report.json").read_text(
            encoding="utf-8"
        )
    )
    return report.model_copy(
        update={
            "evaluation_id": UUID(int=evaluation_id),
            "generated_at": generated_at or datetime(2026, 9, 4, tzinfo=UTC),
        }
    )


def test_create_persists_fixed_two_mode_report_and_returns_safe_projection(
    client, monkeypatch
) -> None:
    expected = _report()
    captured: dict[str, object] = {}

    async def fake_run(suite: Path, modes=("fragile", "resilient")):
        captured["suite"] = suite
        captured["modes"] = tuple(modes)
        return expected

    monkeypatch.setattr("agent_reliability_lab.api.services.run_evaluation", fake_run)
    created = client.post("/v1/evaluations", json={"suite": "incident-response"})
    fetched = client.get(f"/v1/evaluations/{expected.evaluation_id}")

    assert created.status_code == 201
    assert fetched.status_code == 200
    assert created.json() == fetched.json()
    assert captured["modes"] == ("fragile", "resilient")
    assert Path(captured["suite"]).resolve().name == "incident-response"
    assert set(created.json()["modes"]) == {"fragile", "resilient"}
    assert_safe_http_json(created.json())
    assert "api-test-secret" not in created.text


def test_evaluation_survives_app_reconstruction(settings, monkeypatch) -> None:
    expected = _report()

    async def fake_run(*_args, **_kwargs):
        return expected

    monkeypatch.setattr("agent_reliability_lab.api.services.run_evaluation", fake_run)
    first_app = create_app(settings)
    with TestClient(first_app) as first:
        created = first.post("/v1/evaluations", json={"suite": "incident-response"})
        assert created.status_code == 201

    second_app = create_app(settings)
    with TestClient(second_app) as second:
        fetched = second.get(f"/v1/evaluations/{expected.evaluation_id}")
        assert fetched.status_code == 200
        assert fetched.json() == created.json()


def test_evaluation_report_ids_are_immutable(settings) -> None:
    store = SQLiteRunStore.from_settings(settings)
    store.create_schema()
    original = _report()
    store.save_evaluation_report(original, suite_name="incident-response")
    store.save_evaluation_report(original, suite_name="incident-response")
    changed = original.model_copy(
        update={"generated_at": datetime(2026, 9, 5, tzinfo=UTC)}
    )

    with pytest.raises(ValueError, match="immutable"):
        store.save_evaluation_report(changed, suite_name="incident-response")

    assert store.get_evaluation_report(original.evaluation_id) == original
    store.close()


def test_latest_evaluation_has_stable_generated_at_and_id_tie_break(
    client, app
) -> None:
    same_time = datetime(2026, 9, 4, tzinfo=UTC)
    first = _report(evaluation_id=1, generated_at=same_time)
    second = _report(evaluation_id=2, generated_at=same_time)
    app.state.container.store.save_evaluation_report(
        first, suite_name="incident-response"
    )
    app.state.container.store.save_evaluation_report(
        second, suite_name="incident-response"
    )

    response = client.get("/v1/evaluations?limit=2")

    assert response.status_code == 200
    assert [item["evaluation_id"] for item in response.json()["items"]] == [
        str(second.evaluation_id),
        str(first.evaluation_id),
    ]
    assert_safe_http_json(response.json())


@pytest.mark.parametrize(
    "suite",
    [
        "../incident-response",
        "/tmp/incident-response",
        "C:\\tmp\\incident-response",
        "https://example.test/suite",
        "incident-response\x00escape",
    ],
)
def test_evaluation_accepts_only_catalog_suite_names(client, suite: str) -> None:
    assert_error(
        client.post("/v1/evaluations", json={"suite": suite}),
        422,
        "validation_error",
    )


def test_unknown_suite_and_extra_evaluation_controls_are_rejected(client) -> None:
    assert_error(
        client.post("/v1/evaluations", json={"suite": "missing-suite"}),
        404,
        "suite_not_found",
    )
    assert_error(
        client.post(
            "/v1/evaluations",
            json={"suite": "incident-response", "modes": ["resilient"]},
        ),
        422,
        "validation_error",
    )


def test_concurrent_heavy_evaluation_is_rejected_with_409(client, monkeypatch) -> None:
    started = Event()
    release = Event()

    async def blocked(*_args, **_kwargs):
        started.set()
        if not release.wait(timeout=3):
            raise TimeoutError("evaluation test barrier timed out")
        return _report()

    monkeypatch.setattr("agent_reliability_lab.api.services.run_evaluation", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.post,
            "/v1/evaluations",
            json={"suite": "incident-response"},
        )
        assert started.wait(timeout=1)
        second = client.post("/v1/evaluations", json={"suite": "incident-response"})
        release.set()
        first_response = first.result()

    assert first_response.status_code == 201
    assert_error(second, 409, "evaluation_in_progress")


def test_evaluation_errors_and_list_bounds_are_stable(client) -> None:
    assert_error(
        client.get("/v1/evaluations/00000000-0000-0000-0000-000000000000"),
        404,
        "evaluation_not_found",
    )
    assert_error(client.get("/v1/evaluations/not-a-uuid"), 422, "validation_error")
    assert_error(client.get("/v1/evaluations?limit=0"), 422, "validation_error")
    assert_error(client.get("/v1/evaluations?limit=101"), 422, "validation_error")
