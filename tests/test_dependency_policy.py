"""Regression tests for security-critical dependency floors."""

import json
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version


def test_security_critical_dependencies_meet_patched_versions() -> None:
    """The lockfile must not reintroduce versions covered by known advisories."""

    minimum_versions = {
        "anyio": "4.14.2",
        "fastapi": "0.141.1",
        "starlette": "1.3.1",
        "pytest": "9.0.3",
    }

    for package, required in minimum_versions.items():
        installed = Version(version(package))
        assert installed >= Version(required), (
            f"{package} {installed} is below the patched floor {required}"
        )


@pytest.mark.parametrize(
    ("package", "minimum", "advisory"),
    [
        ("source-map-js", "1.2.2", "GHSA-68fv-2mgg-jv7q"),
        ("brace-expansion", "5.0.12", "GHSA-q2hr-2g5m-vwhr"),
        # This release also fixes the cache, retry and dump interceptor advisories.
        ("undici", "7.29.1", "GHSA-w293-vg96-wgc3"),
    ],
    ids=["source-map-js", "brace-expansion", "undici"],
)
def test_frontend_lockfile_excludes_vulnerable_versions(
    package: str, minimum: str, advisory: str
) -> None:
    """Check every copy, including a transitive dependency nested by npm."""
    lockfile = Path(__file__).resolve().parents[1] / "web" / "package-lock.json"
    packages = json.loads(lockfile.read_text(encoding="utf-8"))["packages"]
    suffix = f"node_modules/{package}"

    for location, metadata in packages.items():
        if location == suffix or location.endswith(f"/{suffix}"):
            locked = Version(metadata["version"])
            assert locked >= Version(minimum), (
                f"{location} {locked} is below the patched floor {minimum} ({advisory})"
            )
