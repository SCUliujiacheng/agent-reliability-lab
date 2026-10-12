"""Verify both APKs use the pinned persistent release certificate."""

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    expected = json.loads((ROOT / "distribution/android-signing.json").read_text())[
        "certificate_sha256"
    ]
    executable = (
        Path(os.environ["ANDROID_HOME"])
        / "build-tools/36.0.0"
        / ("apksigner.bat" if os.name == "nt" else "apksigner")
    )
    for relative in [
        "release/app-release.apk",
        "androidTest/release/app-release-androidTest.apk",
    ]:
        apk = ROOT / "android/app/build/outputs/apk" / relative
        output = subprocess.check_output(
            [str(executable), "verify", "--verbose", "--print-certs", str(apk)],
            text=True,
        )
        digests = re.findall(
            r"Signer #\d+ certificate SHA-256 digest: ([0-9a-f]+)", output
        )
        if digests != [expected]:
            raise RuntimeError(
                "APK certificate differs from the pinned persistent release key"
            )
        print(apk.name + ": certificate and APK signature verified")


if __name__ == "__main__":
    main()
