"""Regression tests for security-critical dependency floors."""

from importlib.metadata import version

from packaging.version import Version


def test_security_critical_dependencies_meet_patched_versions() -> None:
    """The lockfile must not reintroduce versions covered by known advisories."""

    minimum_versions = {
        "fastapi": "0.141.1",
        "starlette": "1.3.1",
        "pytest": "9.0.3",
    }

    for package, required in minimum_versions.items():
        installed = Version(version(package))
        assert installed >= Version(required), (
            f"{package} {installed} is below the patched floor {required}"
        )
