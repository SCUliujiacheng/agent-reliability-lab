"""Static contracts for container-only behavior that local CI cannot execute."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SYNTAX_IMAGE = "docker/dockerfile:1.7"


def _nginx_named_location(name: str) -> str:
    config = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*location\s+{re.escape(name)}\s*\{{(?P<body>.*?)^\s{{4}}\}}",
        config,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing Nginx location {name}"
    return match.group("body")


def test_nginx_oversized_body_uses_the_stable_json_error_contract() -> None:
    """Catch removal of the proxy-side JSON 413 boundary."""

    config = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert re.search(
        r"^\s*error_page\s+413\s+=\s+@request_too_large;", config, re.MULTILINE
    )

    handler = _nginx_named_location("@request_too_large")
    assert re.search(r"^\s*default_type\s+application/json;", handler, re.MULTILINE)
    response = re.search(
        r"^\s*return\s+413\s+'(?P<body>\{.+\})';", handler, re.MULTILINE
    )
    assert response is not None
    assert json.loads(response.group("body")) == {
        "error": {
            "code": "request_too_large",
            "message": "Request body exceeds the configured limit.",
            "details": {"limit_bytes": 65_536},
        }
    }

    assert re.search(
        r'^\s*add_header\s+X-Content-Type-Options\s+"nosniff"\s+always;',
        config,
        re.MULTILINE,
    )
    assert re.search(
        r'^\s*add_header\s+Referrer-Policy\s+"no-referrer"\s+always;',
        config,
        re.MULTILINE,
    )


def test_nginx_rejects_unknown_hosts_before_the_application_server() -> None:
    """Catch reopening the loopback UI to arbitrary Host routing."""

    config = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    default_server = re.search(
        r"server\s*\{(?P<body>.*?listen\s+8080\s+default_server;.*?)\}",
        config,
        flags=re.DOTALL,
    )
    assert default_server is not None
    assert re.search(
        r"^\s*listen\s+\[::\]:8080\s+default_server;",
        default_server.group("body"),
        re.MULTILINE,
    )
    assert re.search(
        r"^\s*server_name\s+\"\";", default_server.group("body"), re.MULTILINE
    )
    assert re.search(r"^\s*return\s+444;", default_server.group("body"), re.MULTILINE)

    assert re.search(
        r"^\s*server_name\s+localhost\s+127\.0\.0\.1;", config, re.MULTILINE
    )
    assert re.search(r"^\s*server_name\s+_;", config, re.MULTILINE) is None


def test_nginx_denies_framing_on_success_and_named_413_responses() -> None:
    """Keep clickjacking controls at the server level so errors inherit them."""

    config = (ROOT / "web" / "nginx.conf").read_text(encoding="utf-8")
    assert re.search(
        r'^\s*add_header\s+Content-Security-Policy\s+"frame-ancestors \'none\'"\s+always;',
        config,
        re.MULTILINE,
    )
    assert re.search(
        r'^\s*add_header\s+X-Frame-Options\s+"DENY"\s+always;',
        config,
        re.MULTILINE,
    )

    handler = _nginx_named_location("@request_too_large")
    assert "add_header" not in handler


def test_dockerfile_syntax_frontends_share_one_immutable_digest() -> None:
    """Prevent either BuildKit frontend from drifting behind a mutable tag."""

    digests: list[str] = []
    for relative_path in ("Dockerfile", "web/Dockerfile"):
        first_line = (ROOT / relative_path).read_text(encoding="utf-8").splitlines()[0]
        match = re.fullmatch(
            rf"# syntax={re.escape(SYNTAX_IMAGE)}@sha256:(?P<digest>[0-9a-f]{{64}})",
            first_line,
        )
        assert match is not None, f"{relative_path} must pin the syntax frontend"
        digests.append(match.group("digest"))

    assert len(set(digests)) == 1


def test_compose_smoke_exercises_the_proxy_oversized_body_contract() -> None:
    """Catch removal of the real Compose boundary check from GitHub Actions."""

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["containers"]["steps"]
    smoke = next(
        step
        for step in steps
        if step.get("name") == "Exercise same-origin HTTP and SQLite persistence"
    )["run"]

    assert "oversized-request.json" in smoke
    assert "oversized-response.json" in smoke
    assert "oversized-response.headers" in smoke
    assert "70_000" in smoke
    assert "HTTP 413" in smoke
    assert 'get_content_type() == "application/json"' in smoke
    assert '["error"]["code"] == "request_too_large"' in smoke
    assert 'headers["Content-Security-Policy"] == "frame-ancestors \'none\'"' in smoke
    assert 'headers["X-Frame-Options"] == "DENY"' in smoke
