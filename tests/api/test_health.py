"""Health, middleware, CORS, and event-loop safety contracts."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi import FastAPI

from agent_reliability_lab.api.services import (
    EvaluationService,
    RunApplicationService,
)
from agent_reliability_lab.config import Settings
from agent_reliability_lab.storage.store import SQLiteRunStore

from .conftest import assert_error, make_settings


def test_health_reports_database_readiness(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ready"}


def test_failed_catalog_startup_leaves_database_path_removable(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from agent_reliability_lab.api.app import create_app

    empty_scenario_dir = tmp_path / "empty-scenarios"
    empty_scenario_dir.mkdir()
    settings = replace(
        make_settings(tmp_path / "failed-startup"),
        scenario_dir=empty_scenario_dir,
        evaluation_suites=(("incident-response", empty_scenario_dir),),
    )

    with (
        pytest.raises(ValueError, match="scenario catalog is empty"),
        TestClient(create_app(settings)),
    ):
        pytest.fail("an empty catalog must fail during startup")

    try:
        settings.database_path.unlink(missing_ok=True)
    except PermissionError as error:
        pytest.fail(f"failed startup leaked the SQLite handle: {error}")
    assert not settings.database_path.exists()


def test_successful_lifespan_closes_store_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from agent_reliability_lab.api.app import create_app

    close_count = 0
    original_close = SQLiteRunStore.close

    def counted_close(store: SQLiteRunStore) -> None:
        nonlocal close_count
        close_count += 1
        original_close(store)

    monkeypatch.setattr(SQLiteRunStore, "close", counted_close)
    settings = make_settings(tmp_path / "successful-startup")

    with TestClient(create_app(settings)) as api:
        assert api.get("/health").status_code == 200

    assert close_count == 1
    settings.database_path.unlink()
    assert not settings.database_path.exists()


def test_health_failure_uses_stable_secret_free_envelope(client, app) -> None:
    def fail_ping() -> None:
        raise RuntimeError("api-test-secret must not escape")

    app.state.container.store.ping = fail_ping
    response = client.get("/health")

    assert_error(response, 503, "service_unavailable")
    assert "api-test-secret" not in response.text


def test_unknown_path_and_method_use_stable_envelopes(client) -> None:
    assert_error(client.get("/v1/not-a-route"), 404, "not_found")
    assert_error(client.put("/health"), 405, "method_not_allowed")


def test_validation_error_does_not_echo_raw_secret_input(client) -> None:
    response = client.post(
        "/v1/runs",
        json={"scenario_id": "api-test-secret", "mode": "not-a-mode"},
    )

    assert_error(response, 422, "validation_error")
    assert "api-test-secret" not in response.text
    assert "not-a-mode" not in response.text


def test_generic_failure_does_not_expose_exception_or_secret(client, app) -> None:
    def explode():
        raise RuntimeError("api-test-secret provider body")

    app.state.container.catalog.list = explode
    response = client.get("/v1/scenarios")

    assert_error(response, 500, "internal_error")
    assert "api-test-secret" not in response.text
    assert "provider body" not in response.text


def test_json_body_is_bounded_at_64_kib(client) -> None:
    response = client.post(
        "/v1/runs",
        json={"scenario_id": "normal-success", "mode": "resilient", "x": "a" * 70_000},
    )

    assert_error(response, 413, "request_too_large")


@pytest.mark.asyncio
async def test_streaming_body_cannot_bypass_false_content_length(
    app: FastAPI,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(70):
            yield b"x" * 1024

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as api:
            response = await api.post(
                "/v1/runs",
                content=chunks(),
                headers={"content-type": "application/json", "content-length": "1"},
            )

    assert_error(response, 413, "request_too_large")


def test_default_is_same_origin_and_allowlist_is_exact(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from agent_reliability_lab.api.app import create_app

    default_app = create_app(make_settings(tmp_path / "default"))
    with TestClient(default_app) as default_client:
        default = default_client.options(
            "/v1/runs",
            headers={
                "origin": "https://dashboard.example",
                "access-control-request-method": "POST",
            },
        )
    assert "access-control-allow-origin" not in default.headers

    allowed_app = create_app(
        make_settings(tmp_path / "allowed", cors_origins=("https://dashboard.example",))
    )
    with TestClient(allowed_app) as allowed_client:
        allowed = allowed_client.options(
            "/v1/runs",
            headers={
                "origin": "https://dashboard.example",
                "access-control-request-method": "POST",
            },
        )
        denied = allowed_client.options(
            "/v1/runs",
            headers={
                "origin": "https://evil.example",
                "access-control-request-method": "POST",
            },
        )
    assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example"
    assert "access-control-allow-origin" not in denied.headers
    assert allowed.headers["access-control-allow-origin"] != "*"


def test_allowed_origin_is_preserved_on_declared_oversize_error(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from agent_reliability_lab.api.app import create_app

    origin = "https://dashboard.example"
    app = create_app(make_settings(tmp_path, cors_origins=(origin,)))
    with TestClient(app) as api:
        response = api.post(
            "/v1/runs",
            json={
                "scenario_id": "normal-success",
                "mode": "resilient",
                "padding": "x" * 70_000,
            },
            headers={"origin": origin},
        )

    assert_error(response, 413, "request_too_large")
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_allowed_origin_is_preserved_on_streamed_oversize_error(
    tmp_path,
) -> None:
    from agent_reliability_lab.api.app import create_app

    origin = "https://dashboard.example"
    app = create_app(make_settings(tmp_path, cors_origins=(origin,)))

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(70):
            yield b"x" * 1024

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as api:
            response = await api.post(
                "/v1/runs",
                content=chunks(),
                headers={
                    "content-type": "application/json",
                    "content-length": "1",
                    "origin": origin,
                },
            )

    assert_error(response, 413, "request_too_large")
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    ("service_type", "method_name", "path", "payload"),
    [
        (
            RunApplicationService,
            "start",
            "/v1/runs",
            {"scenario_id": "normal-success", "mode": "resilient"},
        ),
        (
            EvaluationService,
            "create",
            "/v1/evaluations",
            {"suite": "incident-response"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_blocking_commands_do_not_block_health_on_same_event_loop(
    monkeypatch,
    settings: Settings,
    service_type: type,
    method_name: str,
    path: str,
    payload: dict[str, str],
) -> None:
    from agent_reliability_lab.api.app import create_app

    started = Event()
    release = Event()

    def blocked(*_args, **_kwargs):
        started.set()
        if not release.wait(timeout=3):
            raise TimeoutError("test barrier timed out")
        raise RuntimeError("expected blocked command exit")

    monkeypatch.setattr(service_type, method_name, blocked)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as api:
            command = asyncio.create_task(api.post(path, json=payload))
            assert await asyncio.to_thread(started.wait, 1)
            health = await asyncio.wait_for(api.get("/health"), timeout=0.75)
            assert health.status_code == 200
            release.set()
            await command
