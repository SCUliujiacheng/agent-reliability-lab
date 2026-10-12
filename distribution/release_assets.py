"""Validate the complete release set and write checksums before publishing."""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path


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
