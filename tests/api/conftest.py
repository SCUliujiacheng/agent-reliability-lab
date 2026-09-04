"""Shared fixtures for the public HTTP contract."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app
from agent_reliability_lab.config import Settings

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO_DIR = PROJECT_ROOT / "scenarios" / "incident-response"

FORBIDDEN_HTTP_KEYS = {
    "context",
    "execution_owner",
    "execution_lease_expires_at",
    "version",
    "pending_action",
    "pending_action_fingerprint",
    "action_fingerprint",
    "idempotency_key",
}


def make_settings(
    root: Path,
    *,
    cors_origins: tuple[str, ...] = (),
    max_request_body_bytes: int = 64 * 1024,
) -> Settings:
    data_dir = root.resolve()
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "runs.db",
        scenario_dir=SCENARIO_DIR.resolve(),
        evaluation_suites=(("incident-response", SCENARIO_DIR.resolve()),),
        cors_origins=cors_origins,
        max_request_body_bytes=max_request_body_bytes,
        secret_values=frozenset({"api-test-secret"}),
    )


def assert_safe_http_json(value: Any) -> None:
    """Recursively prove internal runtime keys never cross the HTTP boundary."""
    if isinstance(value, dict):
        assert FORBIDDEN_HTTP_KEYS.isdisjoint(value)
        for child in value.values():
            assert_safe_http_json(child)
    elif isinstance(value, list):
        for child in value:
            assert_safe_http_json(child)


def assert_error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message", "details"}
    assert response.json()["error"]["code"] == code


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(
        app,
        base_url="http://localhost",
        raise_server_exceptions=False,
    ) as api_client:
        yield api_client


def create_run(
    client: TestClient,
    scenario_id: str = "normal-success",
    mode: str = "resilient",
) -> dict[str, Any]:
    response = client.post("/v1/runs", json={"scenario_id": scenario_id, "mode": mode})
    assert response.status_code == 201, response.text
    return response.json()
