"""Build and test on the target OS; a ZIP is emitted only after a frozen smoke test."""

import argparse
import hashlib
import importlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    config = json.loads((ROOT / "distribution/app.json").read_text(encoding="utf-8"))
    slug, version = config["slug"], config["version"]
    tag = os.environ.get("GITHUB_REF", "")
    if tag.startswith("refs/tags/") and tag != "refs/tags/v" + version:
        raise RuntimeError("Tag and application version differ")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    (ROOT / "build").mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="bundle-", dir=ROOT / "build"))
    # Keep staging under ignored build rather than the source tree root.
    resources = stage / "resources"
    importlib.import_module("distribution.prepare_android").prepare_resources(
        ROOT, resources
    )
    info = {
        "slug": slug,
        "version": version,
        "revision": revision,
        "platform": platform.system(),
        "machine": platform.machine(),
    }
    (resources / "build-info.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8"
    )
    config["build_revision"] = revision
    (resources / "app.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    importlib.import_module("distribution.collect_licenses").collect_licenses(
        ROOT, resources, target="desktop"
    )
    if args.prepare_only:
        print(resources)
        return
    package_name = config.get(
        "python_package", slug.replace("-zh", "").replace("-", "_")
    )
    distribution_name = config.get(
        "python_distribution", package_name.replace("_", "-")
    )
    dist = stage / "dist"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        slug,
        "--distpath",
        str(dist),
        "--workpath",
        str(stage / "work"),
        "--specpath",
        str(stage),
        "--paths",
        str(ROOT),
        "--paths",
        str(ROOT / "src"),
        "--add-data",
        f"{resources}{os.pathsep}resources",
        "--hidden-import",
        config["desktop_module"],
        "--collect-submodules",
        package_name,
        "--copy-metadata",
        distribution_name,
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
    ]
    if sys.platform == "darwin":
        command += [
            "--windowed",
            "--osx-bundle-identifier",
            config["android_application_id"] + ".desktop",
        ]
    command.append(str(ROOT / "distribution/desktop_launcher.py"))
    run(command, cwd=ROOT)
    if sys.platform == "darwin":
        bundle = dist / (slug + ".app")
        executable = bundle / "Contents/MacOS" / slug
        target = "macos-" + ("arm64" if platform.machine() == "arm64" else "x64")
        run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    else:
        bundle = dist / slug
        executable = bundle / (slug + (".exe" if sys.platform == "win32" else ""))
        target = "windows-x64" if sys.platform == "win32" else "linux-x64"
    with tempfile.TemporaryDirectory(prefix="download smoke 中文 ") as separate:
        copied = Path(separate) / bundle.name
        shutil.copytree(bundle, copied, symlinks=True)
        smoke = Path(separate) / "smoke.json"
        copied_exe = copied / executable.relative_to(bundle)
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "PYTHONPATH",
                "PYTHONHOME",
                "VIRTUAL_ENV",
                "LEDGER_DB_PATH",
                "LEDGER_DEV",
            }
        }
        run(
            [str(copied_exe), "--smoke-test", "--smoke-output", str(smoke)],
            cwd=separate,
            env=environment,
            timeout=180,
        )
        report = json.loads(smoke.read_text(encoding="utf-8"))
        assert (
            report["frozen"]
            and report["version"] == version
            and report["status"] == "ok"
        )
    output = ROOT / "dist/releases"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{slug}-{version}-{target}.zip"
    if sys.platform == "darwin":
        # ditto preserves PyInstaller's symlinks and executable modes inside .app.
        run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(bundle),
                str(archive),
            ]
        )
    else:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for item in bundle.rglob("*"):
                if item.is_file():
                    zipped.write(item, item.relative_to(bundle.parent))
    (output / (archive.name + ".smoke.json")).write_text(
        json.dumps(info | report, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "archive": str(archive),
                "sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
