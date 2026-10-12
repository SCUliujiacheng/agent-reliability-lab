"""Collect actual release license bytes; never fetch or infer license text at build time.

Use after application resource staging, before PyInstaller or Gradle packages it.
The manifest describes license evidence, not a complete dependency/SBOM audit.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import sys
from pathlib import Path, PurePosixPath


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_relative(value):
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise RuntimeError(f"Unsafe license path: {value!r}")
    return path


def _license_name(name):
    return bool(
        re.match(r"^(licen[sc]e|copying|notice)(?:$|[._-])", name, re.IGNORECASE)
    )


def _license_files(directory):
    """Keep top-level notices and full license directories, excluding dependencies."""
    found = []
    for item in sorted(directory.iterdir()):
        if item.is_file() and _license_name(item.name):
            found.append(item)
        elif item.is_dir() and item.name.lower() in {"licenses", "licences"}:
            found.extend(sorted(path for path in item.rglob("*") if path.is_file()))
    return found


def _copy(source, output, relative):
    relative = _safe_relative(relative).as_posix()
    if not source.is_file() or not source.stat().st_size:
        raise RuntimeError(f"Missing or empty license: {source.name}")
    destination = output / relative
    if not destination.resolve().is_relative_to(output.resolve()):
        raise RuntimeError("Unsafe license destination path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
    return {
        "path": relative,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }


def _android_runtime(project, output):
    vendored = project / "licenses"
    sources = _read_json(vendored / "sources.json")
    if (
        sources.get("schema_version") != 1
        or sources.get("chaquopy_target") != "3.13.9-0"
    ):
        raise RuntimeError("Unsupported Android runtime license sources")
    versions = {item["name"]: item["version"] for item in sources["components"]}
    if versions != {"CPython": "3.13.9", "Chaquopy": "17.0.0"}:
        raise RuntimeError("Android runtime version differs from vendored licenses")
    result = []
    for item in sources["components"]:
        path = vendored / _safe_relative(item["file"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError(
                f"Vendored license SHA-256 checksum mismatch: {item['name']}"
            )
        record = _copy(path, output, "runtime/" + item["file"])
        result.append(
            {
                "name": item["name"],
                "version": item["version"],
                "source": item["source"],
                "license_files": [record],
            }
        )
    return result


def _desktop_runtime(output):
    prefix = Path(sys.base_prefix)
    minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    directories = [prefix, prefix / "lib" / minor, prefix / "Lib"]
    files = []
    for directory in directories:
        if directory.is_dir():
            files.extend(
                path
                for path in sorted(directory.iterdir())
                if path.is_file() and path.name.upper().startswith("LICENSE")
            )
    if not files:
        raise RuntimeError("Actual desktop Python runtime license is missing")
    version = platform.python_version()
    return {
        "name": "CPython",
        "version": version,
        "source": f"https://github.com/python/cpython/tree/v{version}",
        "license_files": [
            _copy(
                path, output, "runtime/CPython/" + path.relative_to(prefix).as_posix()
            )
            for path in files
        ],
    }


def _python_packages(output):
    """Include the installed packaging environment's distribution notices, as before."""
    components = []
    for package in sorted(
        importlib.metadata.distributions(), key=lambda p: p.metadata["Name"].lower()
    ):
        files = []
        for item in sorted(package.files or [], key=str):
            # Only metadata license records, not editable-source or arbitrary package files.
            metadata_indices = [
                index
                for index, part in enumerate(item.parts)
                if part.endswith(".dist-info")
            ]
            if not metadata_indices:
                continue
            if not any(
                _license_name(part) or part.lower() in {"licenses", "licences"}
                for part in item.parts[metadata_indices[-1] + 1 :]
            ):
                continue
            source = Path(package.locate_file(item))
            if source.is_file():
                files.append(_copy(source, output, "python/" + item.as_posix()))
        if files:
            name = package.metadata["Name"]
            components.append(
                {
                    "name": name,
                    "version": package.version,
                    "source": f"https://pypi.org/project/{name}/{package.version}/",
                    "license_files": files,
                }
            )
    return components


def _npm_packages(project, output, frontend_dir):
    if frontend_dir is not None:
        roots = [Path(frontend_dir)]
    else:
        roots = [
            project / name for name in ["frontend", "web"] if (project / name).is_dir()
        ]
    components = []
    for frontend in roots:
        lock = _read_json(frontend / "package-lock.json")
        if lock.get("lockfileVersion", 0) < 2 or not isinstance(
            lock.get("packages"), dict
        ):
            raise RuntimeError("An npm lockfile with package records is required")
        for relative, locked in sorted(lock["packages"].items()):
            if not relative or locked.get("dev"):
                continue
            if locked.get("link"):
                raise RuntimeError(
                    f"Production npm workspace link needs explicit license handling: {relative}"
                )
            package_dir = frontend / _safe_relative(relative)
            if not package_dir.resolve().is_relative_to(frontend.resolve()):
                raise RuntimeError("Unsafe npm package path")
            if not package_dir.exists() and locked.get("optional"):
                continue  # An uninstalled platform-optional package is not bundled.
            installed = _read_json(package_dir / "package.json")
            name, version = installed["name"], installed["version"]
            if version != locked.get("version"):
                raise RuntimeError(
                    f"Installed npm version differs from lockfile: {name}"
                )
            files = _license_files(package_dir)
            if not files:
                raise RuntimeError(f"Production npm package {name} has no license file")
            source = locked.get("resolved")
            if not isinstance(source, str) or not source.startswith("https://"):
                raise RuntimeError(
                    f"Production npm package {name} lacks an HTTPS source"
                )
            records = [
                _copy(
                    path,
                    output,
                    f"npm/{frontend.name}/{relative}/{path.relative_to(package_dir).as_posix()}",
                )
                for path in files
            ]
            components.append(
                {
                    "name": name,
                    "version": version,
                    "source": source,
                    "license_files": records,
                }
            )
    return components


def collect_licenses(project_root, resource_dir, *, target, frontend_dir=None):
    """Write resources/licenses/components.json and byte-identical license files."""
    if target not in {"android", "desktop"}:
        raise ValueError("target must be android or desktop")
    project = Path(project_root)
    output = Path(resource_dir) / "licenses"
    output.mkdir(parents=True, exist_ok=True)
    config = _read_json(project / "distribution/app.json")
    components = [
        {
            "name": config["slug"],
            "version": config["version"],
            "source": "project:LICENSE",
            "license_files": [_copy(project / "LICENSE", output, "LICENSE")],
        }
    ]
    if target == "android":
        components.extend(_android_runtime(project, output))
    else:
        components.append(_desktop_runtime(output))
        components.extend(_python_packages(output))
    components.extend(_npm_packages(project, output, frontend_dir))
    manifest = {
        "schema_version": 1,
        "target": target,
        "components": sorted(
            components, key=lambda item: (item["name"], item["version"])
        ),
    }
    (output / "components.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    verify_licenses(output)
    return manifest


def verify_licenses(license_dir):
    """Validate collected license paths and hashes before archiving or publishing."""
    directory = Path(license_dir)
    manifest = _read_json(directory / "components.json")
    if manifest.get("schema_version") != 1 or manifest.get("target") not in {
        "android",
        "desktop",
    }:
        raise RuntimeError("Invalid license manifest schema or target")
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise RuntimeError("Empty license components")
    required = (
        {"CPython", "Chaquopy"} if manifest["target"] == "android" else {"CPython"}
    )
    if not required.issubset({item.get("name") for item in components}):
        raise RuntimeError("Required runtime license components missing")
    for component in components:
        if not all(
            isinstance(component.get(field), str) and component[field]
            for field in ["name", "version", "source"]
        ):
            raise RuntimeError("License component provenance missing")
        if not component.get("license_files"):
            raise RuntimeError("License component has no files")
        for item in component["license_files"]:
            path = directory / _safe_relative(item["path"])
            if not path.resolve().is_relative_to(directory.resolve()):
                raise RuntimeError("Unsafe license path")
            if not path.is_file() or not path.stat().st_size:
                raise RuntimeError(f"Missing or empty packaged license: {item['path']}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise RuntimeError(
                    f"Packaged license SHA-256 checksum mismatch: {item['path']}"
                )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--resource-dir", type=Path, required=True)
    parser.add_argument("--target", choices=["android", "desktop"], required=True)
    args = parser.parse_args()
    manifest = collect_licenses(
        args.project_root, args.resource_dir, target=args.target
    )
    print(
        json.dumps({"target": args.target, "components": len(manifest["components"])})
    )


if __name__ == "__main__":
    main()
