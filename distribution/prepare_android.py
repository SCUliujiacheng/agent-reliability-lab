"""Build an explicit resource allowlist, never copying the user's working data."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from agent_reliability_lab.scenarios.loader import load_scenario, scenario_sha256

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from distribution.resource_staging import reset_resources


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prepare_resources(project_root: Path, resource_dir: Path) -> None:
    project_root, resource_dir = (
        Path(project_root).resolve(),
        Path(resource_dir).resolve(),
    )
    web = project_root / "web" / "dist"
    if not (web / "index.html").is_file():
        raise FileNotFoundError("Build the dashboard first: npm --prefix web run build")
    resource_dir = reset_resources(project_root, resource_dir)
    data = resource_dir / "data"
    scenario_output = data / "scenarios"
    scenario_output.mkdir(parents=True, exist_ok=True)
    scenarios = []
    for source in sorted(
        (project_root / "scenarios" / "incident-response").glob("*.yaml")
    ):
        scenarios.append(
            {
                "source_name": source.name,
                "source_sha256": scenario_sha256(source),
                "scenario": load_scenario(source).model_dump(mode="json"),
            }
        )
        shutil.copyfile(source, scenario_output / source.name)
    if len(scenarios) != 6:
        raise ValueError(
            "The portable edition requires the six frozen incident scenarios"
        )
    catalog = {
        "format": "arl-scenarios/v1",
        "suite_hash": hashlib.sha256(canonical(scenarios).encode()).hexdigest(),
        "scenarios": scenarios,
    }
    (data / "scenarios.json").write_text(canonical(catalog), encoding="utf-8")
    shutil.copyfile(
        project_root / "benchmarks" / "baseline-report.json",
        data / "baseline-report.json",
    )
    shutil.copytree(web, resource_dir / "web", dirs_exist_ok=True)
    shutil.copyfile(
        project_root / "distribution" / "app.json", resource_dir / "app.json"
    )
    licenses = resource_dir / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(project_root / "LICENSE", licenses / "Agent-Reliability-Lab.txt")
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        revision = "unavailable"
        dirty = False
    metadata = json.loads((resource_dir / "app.json").read_text(encoding="utf-8"))
    metadata["build_revision"] = revision
    (resource_dir / "app.json").write_text(canonical(metadata), encoding="utf-8")
    (resource_dir / "build.json").write_text(
        canonical(
            {
                "git_revision": revision,
                "git_dirty": dirty,
                "version": metadata["version"],
                "mobile_engine": "mobile/v1",
                "suite_hash": catalog["suite_hash"],
            }
        ),
        encoding="utf-8",
    )


def prepare_mobile_resources(project_root: Path, resource_dir: Path) -> None:
    prepare_resources(project_root, resource_dir)
    index = resource_dir / "web" / "index.html"
    html = index.read_text(encoding="utf-8")
    index.write_text(
        html.replace("</head>", '<meta name="arl-runtime" content="mobile"></head>', 1),
        encoding="utf-8",
    )


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    generated = root / "android" / "app" / "build" / "generated"
    parser.add_argument("--python-dir", type=Path, default=generated / "python")
    parser.add_argument(
        "--assets-dir", type=Path, default=generated / "assets" / "application"
    )
    args = parser.parse_args()
    host = root / "distribution" / "portable_host.py"
    if not host.is_file():
        parser.error("The shared distribution/portable_host.py is required")
    prepare_mobile_resources(root, args.assets_dir)
    python_dir = reset_resources(root, args.python_dir)
    for name in ("portable_host.py", "mobile_app.py"):
        shutil.copyfile(root / "distribution" / name, python_dir / name)


if __name__ == "__main__":
    main()
