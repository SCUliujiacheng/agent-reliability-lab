# Application downloads

Open the [v0.2.0 release](https://github.com/SCUliujiacheng/agent-reliability-lab/releases/tag/v0.2.0) and choose an asset for your device. The source archives at the bottom of GitHub's release page are for development.

| Device | Asset | Start |
| --- | --- | --- |
| Windows x64 | ZIP ending in `windows-x64.zip` | Extract the whole ZIP, then run `agent-reliability-lab.exe`. |
| Apple silicon Mac | ZIP ending in `macos-arm64.zip` | Extract and open the `.app`. |
| Intel Mac | ZIP ending in `macos-x64.zip` | Extract and open the `.app`. |
| Android 7.0 or newer, 64-bit | APK | Install the APK, then open Agent Reliability Lab. |

These first binary builds are prereleases. Desktop builds do not carry a commercial Windows publisher certificate or Apple notarization. Build and emulator checks do not establish compatibility with every physical device. Release checksums let you compare a downloaded file with the published build.

## What runs locally

The desktop app starts the existing FastAPI/Pydantic/SQLAlchemy engine and opens the dashboard in your browser. A small launch page has a Quit button; closing a dashboard tab alone does not stop the process. Each launch uses a private localhost session. Python, the dashboard, six scenario files and the relevant licenses are bundled.

Android executes all six synthetic scenarios in a standard-library Python runner embedded in the app. A timeout or rate-limit rule produces an actual failed attempt; resilient mode then retries. Invalid input and malformed output are rejected. Approval decisions are bound to the action step and fingerprint. SQLite stores the run, ordered trace, decision and simulated rollback preparation. Closing and reopening the app preserves the last committed checkpoint. No real infrastructure operation is performed.

Android reports use `report_version: mobile/v1` and `grader_version: mobile-exact/v1`. Every evaluation creates twelve new runs. Counts, recovery rates and sequence accuracy are recomputed from their persisted events; a missing or changed trace makes the report fail closed. Source scenario hashes and the build revision are included. These reports are a separate contract from desktop `exact-v6` reports and cannot be passed to the desktop `arl gate` command. Latency values naturally differ across engines and devices.

The Android edition does not include arbitrary scenario import, external model providers, the full CLI or the desktop multi-process execution lease system. It is an offline experiment app, not a production incident controller. Use Export trace to save a selected run through Android's file picker.

## Your data

Desktop records are stored separately from the extracted app:

- Windows: `%LOCALAPPDATA%\agent-reliability-lab`.
- macOS: `~/Library/Application Support/agent-reliability-lab`.
- An explicit `--data-dir` can choose another directory.

Android uses its private app storage and excludes these records from automatic backup. App updates preserve that directory; uninstalling or clearing app storage removes it. Export traces before removing the app if you need to keep them. Copying or deleting an extracted desktop bundle does not move or remove its user data.

The English and Chinese editions have separate application IDs and data directories. Their UI language differs; the six scenario definitions and portable execution code are the same.

## Reproduce a build

```text
uv sync --locked --dev --group build
npm ci --prefix web
npm --prefix web run build
uv run --group build python distribution/build_desktop.py
```

Build desktop assets on their target operating system. The builder launches the frozen executable from a separate directory with spaces and non-ASCII characters, checks the authenticated service and bundled dashboard, and creates the ZIP only after that smoke test passes. Android's Gradle build calls `distribution/prepare_android.py` to package only the explicit resource allowlist and standard-library mobile adapter.

The application tests compare all six scenarios in both modes against the desktop engine, including outcomes, logical tool order, failures, successful attempts and retries. They also cover approval conflicts and duplicates, interruption and reopening, concurrent approval, trace pagination, evidence corruption and trace export. Physical-device testing remains a separate release activity.
