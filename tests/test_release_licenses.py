import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def archive_with_licenses(target="android", *, change=None):
    text = b"The original upstream copyright and license text.\n"
    names = {"CPython": "3.13.9" if target == "android" else "3.12.13"}
    if target == "android":
        names["Chaquopy"] = "17.0.0"
    names.update({"react": "19.0.0", "react-dom": "19.0.0"})
    manifest = {
        "schema_version": 1,
        "target": target,
        "components": [
            {
                "name": name,
                "version": version,
                "source": "https://example.test/upstream/LICENSE",
                "license_files": [
                    {
                        "path": name + "/LICENSE",
                        "sha256": hashlib.sha256(text).hexdigest(),
                    }
                ],
            }
            for name, version in names.items()
        ],
    }
    if change:
        change(manifest)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("resources/licenses/components.json", json.dumps(manifest))
        for name in names:
            archive.writestr("resources/licenses/" + name + "/LICENSE", text)
    stream.seek(0)
    return zipfile.ZipFile(stream)


@pytest.mark.parametrize("target", ["android", "desktop"])
def test_release_checks_embedded_license_contents(target):
    from distribution.release_assets import validate_licenses

    with archive_with_licenses(target) as archive:
        validate_licenses(archive, "resources/licenses/", target, has_frontend=True)


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m.update(target="other"),
        lambda m: m["components"].pop(0),
        lambda m: m["components"].pop(1),
        lambda m: m["components"].pop(-1),
        lambda m: m["components"][0].update(version="3.12.13"),
        lambda m: m["components"][0]["license_files"][0].update(sha256="0" * 64),
        lambda m: m["components"][0]["license_files"][0].update(path="../LICENSE"),
        lambda m: m["components"][0].update(license_files=[]),
    ],
)
def test_release_rejects_missing_or_mismatched_required_notices(change):
    from distribution.release_assets import validate_licenses

    with archive_with_licenses(change=change) as archive, pytest.raises(RuntimeError):
        validate_licenses(archive, "resources/licenses/", "android", has_frontend=True)
