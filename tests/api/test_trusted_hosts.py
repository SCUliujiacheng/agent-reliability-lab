"""Exact HTTP Host allowlist contracts for the local API boundary."""

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_reliability_lab.api.app import create_app
from agent_reliability_lab.config import Settings

from .conftest import make_settings


def test_default_trusted_hosts_cover_documented_local_and_compose_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARL_TRUSTED_HOSTS", raising=False)

    settings = Settings.from_env(tmp_path)

    assert settings.trusted_hosts == ("localhost", "127.0.0.1", "api")


def test_trusted_hosts_can_be_configured_as_an_exact_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARL_TRUSTED_HOSTS", "Dashboard.Internal, API.Internal")

    settings = Settings.from_env(tmp_path)

    assert settings.trusted_hosts == ("dashboard.internal", "api.internal")


@pytest.mark.parametrize(
    "trusted_hosts",
    [
        (),
        ("",),
        ("*",),
        ("*.example.test",),
        ("http://localhost",),
        ("localhost:8000",),
        ("bad_host",),
        ("-localhost",),
        ("localhost-",),
        ("localhost", "localhost"),
    ],
)
def test_trusted_host_configuration_rejects_non_exact_or_invalid_values(
    tmp_path: Path, trusted_hosts: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="trusted hosts"):
        replace(make_settings(tmp_path), trusted_hosts=trusted_hosts)


def test_explicit_empty_trusted_host_environment_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARL_TRUSTED_HOSTS", "")

    with pytest.raises(ValueError, match="trusted hosts"):
        Settings.from_env(tmp_path)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "api"])
def test_documented_default_hosts_reach_health(tmp_path: Path, host: str) -> None:
    app = create_app(make_settings(tmp_path / host))

    with TestClient(app, base_url=f"http://{host}") as client:
        response = client.get("/health")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "localhost.attacker.example",
        "127.0.0.1.attacker.example",
        "localhost.",
    ],
)
def test_untrusted_or_lookalike_hosts_are_rejected(tmp_path: Path, host: str) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/health", headers={"host": host})

    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_untrusted_host_is_rejected_before_body_buffering(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app, base_url="http://localhost") as client:
        response = client.request(
            "OPTIONS",
            "/v1/runs",
            content=b"x" * 70_000,
            headers={
                "host": "attacker.example",
                "origin": "https://dashboard.example",
                "access-control-request-method": "POST",
            },
        )

    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_custom_host_replaces_instead_of_extending_defaults(tmp_path: Path) -> None:
    settings = replace(make_settings(tmp_path), trusted_hosts=("lab.internal",))
    app = create_app(settings)

    with TestClient(app, base_url="http://lab.internal") as client:
        assert client.get("/health").status_code == 200
        denied = client.get("/health", headers={"host": "localhost"})

    assert denied.status_code == 400


def test_host_matching_is_case_insensitive(tmp_path: Path) -> None:
    app = create_app(make_settings(tmp_path))

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get("/health", headers={"host": "LocalHost:8000"})

    assert response.status_code == 200
