"""Validate the complete release set and write checksums before publishing."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path, PurePosixPath


def validate_licenses(archive, base, target, *, has_frontend):
    """Check notices from the actual final archive, not only a source manifest."""
    try:
        manifest = json.loads(archive.read(base + "components.json"))
        if manifest["schema_version"] != 1 or manifest["target"] != target:
            raise ValueError("Wrong license manifest format or target")
        components = manifest["components"]
        names = {item["name"] for item in components}
        required = {"CPython"} | ({"Chaquopy"} if target == "android" else set())
        if has_frontend:
            required |= {"react", "react-dom"}
        if not required <= names:
            raise ValueError("Missing runtime or frontend notices")
        for item in components:
            if not item["version"] or not item["source"] or not item["license_files"]:
                raise ValueError("Incomplete component provenance")
            if item["name"] == "CPython":
                valid_version = (
                    item["version"] == "3.13.9"
                    if target == "android"
                    else item["version"].startswith("3.12.")
                )
                if not valid_version:
                    raise ValueError(
                        "License does not match the bundled CPython version"
                    )
            if item["name"] == "Chaquopy" and item["version"] != "17.0.0":
                raise ValueError("License does not match Chaquopy")
            for notice in item["license_files"]:
                path = PurePosixPath(notice["path"])
                if path.is_absolute() or ".." in path.parts or "\\" in notice["path"]:
                    raise ValueError("Invalid notice path")
                payload = archive.read(base + notice["path"])
                if (
                    not payload.strip()
                    or hashlib.sha256(payload).hexdigest() != notice["sha256"]
                ):
                    raise ValueError("Missing, empty or changed license text")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError(f"Invalid bundled licenses: {base}: {exc}") from exc


def main():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "distribution/app.json").read_text(encoding="utf-8"))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    folder = root / "dist/releases"
    prefix = f"{config['slug']}-{config['version']}"
    expected = [
        prefix + "-" + suffix
        for suffix in [
            "windows-x64.zip",
            "macos-arm64.zip",
            "macos-x64.zip",
            "android.apk",
        ]
    ]
    manifest = {"version": config["version"], "revision": revision, "assets": []}
    sums = []
    for name in expected:
        asset = folder / name
        if not asset.is_file() or asset.stat().st_size < 1000000:
            raise RuntimeError(f"Missing or suspiciously small build: {name}")
        with zipfile.ZipFile(asset) as archive:
            if archive.testzip() is not None:
                raise RuntimeError(f"Corrupt archive: {name}")
            names = archive.namelist()
            if any(
                n.endswith((".db", ".p12", ".jks", ".keystore")) or "/.env" in n
                for n in names
            ):
                raise RuntimeError(f"Private data in bundle: {name}")
            if name.endswith(".zip"):
                candidates = [
                    n for n in names if n.endswith("resources/build-info.json")
                ]
                if not candidates or not any(
                    json.loads(archive.read(n)).get("revision") == revision
                    for n in candidates
                ):
                    raise RuntimeError(f"Wrong source revision: {name}")
            else:
                metadata = json.loads(archive.read("assets/application/app.json"))
                if (
                    metadata.get("version") != config["version"]
                    or metadata.get("slug") != config["slug"]
                    or metadata.get("build_revision") != revision
                ):
                    raise RuntimeError(f"Wrong APK version or source revision: {name}")
            target = "desktop" if name.endswith(".zip") else "android"
            suffix = (
                "resources/licenses/components.json"
                if target == "desktop"
                else "assets/application/licenses/components.json"
            )
            manifests = [entry for entry in names if entry.endswith(suffix)]
            if len(manifests) != 1:
                raise RuntimeError(f"Missing or ambiguous runtime notices: {name}")
            validate_licenses(
                archive,
                manifests[0].removesuffix("components.json"),
                target,
                has_frontend=(root / "frontend/package.json").exists()
                or (root / "web/package.json").exists(),
            )
        with asset.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        sums.append(f"{digest}  {name}\n")
        manifest["assets"].append(
            {"name": name, "sha256": digest, "bytes": asset.stat().st_size}
        )
    (folder / "SHA256SUMS.txt").write_text("".join(sums), encoding="utf-8")
    (folder / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
