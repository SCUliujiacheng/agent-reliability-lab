"""Real-HTTP delivery proof for durable approval reconstruction."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).parents[2]
SCENARIO_DIR = PROJECT_ROOT / "scenarios" / "incident-response"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _running_api(data_dir: Path) -> Iterator[httpx.Client]:
    """Run the installed ASGI app in a fresh interpreter on one durable DB."""
    port = _free_loopback_port()
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("ARL_")
    }
    environment.update(
        {
            "ARL_DATA_DIR": str(data_dir),
            "ARL_DATABASE_PATH": "runs.db",
            "ARL_SCENARIO_DIR": str(SCENARIO_DIR),
            "ARL_EVALUATION_SUITE_DIR": str(SCENARIO_DIR),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_reliability_lab.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    deadline = time.monotonic() + 15.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(f"API process exited during startup:\n{output}")
            try:
                response = client.get("/health")
            except httpx.TransportError:
                time.sleep(0.05)
                continue
            if response.status_code == 200:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("API did not become healthy within 15 seconds")
        yield client
    finally:
        client.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def _events(client: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/v1/runs/{run_id}/trace", params={"limit": 100})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_more"] is False
    return list(payload["events"])


def test_real_http_host_matching_is_case_insensitive(tmp_path: Path) -> None:
    with _running_api(tmp_path / "host-boundary") as client:
        allowed = client.get("/health", headers={"host": "LOCALHOST"})
        denied = client.get("/health", headers={"host": "attacker.example"})

    assert allowed.status_code == 200
    assert denied.status_code == 400


def test_http_approval_workflow_survives_process_reconstruction(
    tmp_path: Path,
) -> None:
    """Dropping process state must not lose approval or duplicate a write."""
    data_dir = tmp_path / "durable-data"

    with _running_api(data_dir) as first:
        response = first.post(
            "/v1/runs",
            json={"scenario_id": "approval-reconstruction", "mode": "resilient"},
        )
        assert response.status_code == 201, response.text
        waiting = response.json()
        assert waiting["status"] == "waiting_approval"
        assert not any(
            event["event_type"] == "tool.attempt.succeeded"
            and event["payload"].get("tool_name") == "prepare_rollback"
            for event in _events(first, waiting["id"])
        )

    decision = {
        "actor": "demo-reviewer",
        "allow": True,
        "reason": "verified",
        "action_step": waiting["pending_approval"]["action_step"],
        "action_fingerprint": waiting["pending_approval"]["action_fingerprint"],
    }
    with _running_api(data_dir) as second, _running_api(data_dir) as competing:
        reconstructed = second.get(f"/v1/runs/{waiting['id']}")
        assert reconstructed.status_code == 200
        assert reconstructed.json()["status"] == "waiting_approval"

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                future.result()
                for future in (
                    pool.submit(
                        client.post,
                        f"/v1/runs/{waiting['id']}/approvals",
                        json=decision,
                    )
                    for client in (second, competing)
                )
            ]
        assert [response.status_code for response in responses] == [200, 200]
        assert {response.json()["status"] for response in responses} == {"succeeded"}

    with _running_api(data_dir) as third:
        replayed = third.post(f"/v1/runs/{waiting['id']}/approvals", json=decision)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["status"] == "succeeded"

        events = _events(third, waiting["id"])
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        assert sum(event["event_type"] == "approval.recorded" for event in events) == 1
        assert (
            sum(
                event["event_type"] == "tool.attempt.succeeded"
                and event["payload"].get("tool_name") == "prepare_rollback"
                for event in events
            )
            == 1
        )
        assert events[-1]["event_type"] == "run.succeeded"
        assert events[-1]["status"] == "ok"
