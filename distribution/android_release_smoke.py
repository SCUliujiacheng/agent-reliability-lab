"""Install the signed APK, open the actual release WebView, then exercise an update."""

import json
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def adb(*arguments):
    return subprocess.check_output(["adb", *arguments], text=True, timeout=300)


def wait_for_ui(package):
    deadline = time.monotonic() + 90
    last = ""
    while time.monotonic() < deadline:
        try:
            adb("shell", "uiautomator", "dump", "/sdcard/download-smoke.xml")
            last = adb("shell", "cat", "/sdcard/download-smoke.xml")
            tree = ET.fromstring(last)
            if ui_ready(tree, package):
                return
        except (subprocess.CalledProcessError, ET.ParseError):
            pass
        time.sleep(2)
    raise RuntimeError(f"No populated release WebView for {package}: {last[:500]}")


def ui_ready(tree, package):
    # Native loading/error labels sit alongside an empty WebView. Never count them.
    all_text = " ".join(node.get("text", "") for node in tree.iter("node")).lower()
    if any(
        message in all_text
        for message in [
            "err_connection",
            "web page not available",
            "无法启动",
            "启动失败",
            "service could not start",
            "could not open",
            "webpage not available",
        ]
    ):
        raise RuntimeError("Release WebView displayed a startup error")
    for view in tree.iter("node"):
        if (
            view.get("class") == "android.webkit.WebView"
            and view.get("package") == package
        ):
            descendants = [node for node in view.iter("node") if node is not view]
            labels = [node.get("text", "").strip() for node in descendants]
            if len(" ".join(filter(None, labels))) > 20 and any(
                node.get("clickable") == "true"
                or node.get("class") == "android.widget.EditText"
                for node in descendants
            ):
                return True
    return False


def main():
    config = json.loads((ROOT / "distribution/app.json").read_text(encoding="utf-8"))
    package = config["android_application_id"]
    apk = ROOT / "android/app/build/outputs/apk/release/app-release.apk"
    tests = (
        ROOT
        / "android/app/build/outputs/apk/androidTest/release/app-release-androidTest.apk"
    )
    adb("install", "-r", str(apk))
    adb("install", "-r", str(tests))

    def instrument(*arguments):
        output = adb(
            "shell",
            "am",
            "instrument",
            "-w",
            "-r",
            *arguments,
            package + ".test/androidx.test.runner.AndroidJUnitRunner",
        )
        if (
            "FAILURES" in output
            or "INSTRUMENTATION_FAILED" in output
            or not re.search(r"OK \([1-9][0-9]* tests?\)", output)
        ):
            raise RuntimeError("Release instrumentation failed:\n" + output[-6000:])
        print(output)

    instrument()
    upgrade = [
        "-e",
        "class",
        "dev.portableapp.shell.OfflineAppTest#upgradePreservesPersistedUserData",
    ]
    instrument(*upgrade, "-e", "upgradePhase", "seed")
    # Preserve a real nonempty ledger/private marker across a signed in-place update.
    adb("install", "-r", str(apk))
    instrument(*upgrade, "-e", "upgradePhase", "verify")
    adb("shell", "am", "force-stop", package)
    adb(
        "shell",
        "am",
        "start",
        "-W",
        "-n",
        package + "/dev.portableapp.shell.MainActivity",
    )
    wait_for_ui(package)
    print(
        json.dumps(
            {
                "release_install_and_update": "passed",
                "package": package,
                "version": config["version"],
            }
        )
    )


if __name__ == "__main__":
    main()
