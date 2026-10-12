"""Safely reset an explicitly owned generated-output directory.

A nonempty directory is replaceable only after this helper has marked it.
Source roots, their ancestors, and directory links are never reset.
"""

import json
import shutil
from pathlib import Path

MARKER_NAME = ".generated-resources.json"
OWNERSHIP = {"owner": "codex-offline-distribution", "schema": 1}


def reset_resources(project_root: Path, resource_dir: Path) -> Path:
    """Return an empty marked output directory without touching unowned files."""
    source = Path(project_root).resolve()
    requested = Path(resource_dir).absolute()
    if requested.is_symlink() or requested.is_junction():
        raise ValueError("generated output must not be a symbolic link or junction")
    target = requested.resolve()
    if target == source or target in source.parents or target == target.parent:
        raise ValueError("generated output must not be the source root or an ancestor")
    if target.exists() and not target.is_dir():
        raise ValueError("generated output must be a directory")

    marker = target / MARKER_NAME
    entries = list(target.iterdir()) if target.exists() else []
    if entries:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("refusing to reset a nonempty unmarked output directory")
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(
                "refusing to reset an output with an invalid ownership marker"
            ) from error
        if owner != OWNERSHIP:
            raise ValueError(
                "refusing to reset an output with an unrelated ownership marker"
            )
        # The resolved target was checked above and contains our exact ownership
        # marker. Remove only its contents. Never follow child links or junctions.
        for child in entries:
            if child.is_symlink():
                child.unlink()
            elif child.is_junction():
                child.rmdir()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        target.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(OWNERSHIP) + "\n", encoding="utf-8")
    return target
