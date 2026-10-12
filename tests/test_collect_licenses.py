"""Packaging must retain actual license bytes and fail closed on missing evidence."""

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def collector():
    source = ROOT / "distribution/collect_licenses.py"
    assert source.is_file(), "The packaging license collector is not implemented"
    spec = importlib.util.spec_from_file_location("license_collector", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "distribution").mkdir(parents=True)
    (root / "distribution/app.json").write_text(
        json.dumps({"slug": "fixture-app", "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "LICENSE").write_bytes(b"Project license\r\nDo not normalize me.\r\n")
    if (ROOT / "licenses").is_dir():
        shutil.copytree(ROOT / "licenses", root / "licenses")
    frontend = root / "frontend"
    frontend.mkdir()
    packages = {"": {"name": "fixture-web", "version": "1.0.0"}}
    for name in ["react", "react-dom", "scheduler", "dev-only"]:
        package = frontend / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": name, "version": "1.2.3"}), encoding="utf-8"
        )
        if name != "dev-only":
            (package / "LICENSE").write_bytes(
                f"{name} copyright\r\nLicense terms.\n".encode()
            )
        packages[f"node_modules/{name}"] = {
            "version": "1.2.3",
            "resolved": f"https://registry.npmjs.org/{name}/-/{name}-1.2.3.tgz",
            "dev": name == "dev-only",
        }
    (frontend / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8"
    )
    return root


def test_android_includes_runtime_and_all_production_npm_license_bytes(
    collector, project, tmp_path
):
    resources = tmp_path / "resources"
    manifest = collector.collect_licenses(project, resources, target="android")
    components = {item["name"]: item for item in manifest["components"]}
    assert set(components) == {
        "fixture-app",
        "CPython",
        "Chaquopy",
        "react",
        "react-dom",
        "scheduler",
    }
    assert components["CPython"]["version"] == "3.13.9"
    assert components["Chaquopy"]["version"] == "17.0.0"
    for name in ["react", "react-dom", "scheduler"]:
        component = components[name]
        assert component["version"] == "1.2.3"
        record = component["license_files"][0]
        actual = (resources / "licenses" / record["path"]).read_bytes()
        assert actual == f"{name} copyright\r\nLicense terms.\n".encode()
        assert record["sha256"] == hashlib.sha256(actual).hexdigest()
        assert component["source"].startswith("https://registry.npmjs.org/")
    assert collector.verify_licenses(resources / "licenses") == manifest
    serialized = (resources / "licenses/components.json").read_text(encoding="utf-8")
    assert str(project) not in serialized


def test_missing_production_npm_license_blocks_packaging(collector, project, tmp_path):
    (project / "frontend/node_modules/react-dom/LICENSE").unlink()
    with pytest.raises(RuntimeError, match="react-dom.*license|license.*react-dom"):
        collector.collect_licenses(project, tmp_path / "resources", target="android")


def test_installed_npm_version_must_match_lockfile(collector, project, tmp_path):
    (project / "frontend/node_modules/react/package.json").write_text(
        '{"name":"react","version":"9.9.9"}', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="version"):
        collector.collect_licenses(project, tmp_path / "resources", target="android")


def test_desktop_copies_runtime_license_from_unix_stdlib_location(
    collector, project, tmp_path, monkeypatch
):
    prefix = tmp_path / "runtime"
    library = (
        prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    )
    library.mkdir(parents=True)
    (library / "LICENSE.txt").write_bytes(b"Actual runtime terms\r\n")
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    manifest = collector.collect_licenses(
        project, tmp_path / "resources", target="desktop"
    )
    component = next(
        item for item in manifest["components"] if item["name"] == "CPython"
    )
    record = component["license_files"][0]
    assert (
        tmp_path / "resources/licenses" / record["path"]
    ).read_bytes() == b"Actual runtime terms\r\n"
    assert not any(item["name"] == "Chaquopy" for item in manifest["components"])


def test_desktop_missing_actual_runtime_license_blocks_packaging(
    collector, project, tmp_path, monkeypatch
):
    prefix = tmp_path / "empty-runtime"
    prefix.mkdir()
    monkeypatch.setattr(sys, "base_prefix", str(prefix))
    with pytest.raises(RuntimeError, match="runtime.*license|license.*runtime"):
        collector.collect_licenses(project, tmp_path / "resources", target="desktop")


def test_changed_vendored_runtime_license_is_rejected(collector, project, tmp_path):
    (project / "licenses/CPython-3.13.9-LICENSE.txt").write_text(
        "truncated", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="checksum|SHA-256"):
        collector.collect_licenses(project, tmp_path / "resources", target="android")


def test_release_verifier_rejects_truncated_packaged_license(
    collector, project, tmp_path
):
    licenses = tmp_path / "resources/licenses"
    manifest = collector.collect_licenses(project, licenses.parent, target="android")
    item = manifest["components"][0]["license_files"][0]
    (licenses / item["path"]).write_bytes(b"truncated")
    with pytest.raises(RuntimeError, match="checksum|SHA-256"):
        collector.verify_licenses(licenses)


def test_release_verifier_rejects_path_escape(collector, project, tmp_path):
    licenses = tmp_path / "resources/licenses"
    manifest = collector.collect_licenses(project, licenses.parent, target="android")
    manifest["components"][0]["license_files"][0]["path"] = "../../outside.txt"
    (licenses / "components.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="path"):
        collector.verify_licenses(licenses)


def test_projects_without_a_frontend_still_receive_runtime_notices(
    collector, project, tmp_path
):
    shutil.rmtree(project / "frontend")
    manifest = collector.collect_licenses(
        project, tmp_path / "resources", target="android"
    )
    assert {item["name"] for item in manifest["components"]} == {
        "fixture-app",
        "CPython",
        "Chaquopy",
    }
