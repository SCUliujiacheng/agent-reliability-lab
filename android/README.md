# Offline Android shell

The APK embeds Chaquopy Python 3.13 and the generated web application. It starts
`portable_host.start(data_dir, resource_dir)` on a worker thread and uses the
returned `origin` and full HttpOnly `cookie` to load a same-origin WebView.
The server remains alive across Activity recreation and is stopped when the last
Activity finishes. App data is stored in `noBackupFilesDir/data`; it survives
application updates and is removed by uninstalling. Versioned resources are
extracted from the APK separately. No external Python installation is required.

## Metadata and generated files

`../distribution/app.json` must contain `slug`, `name`, `version`,
`android_application_id`, `android_version_code`, and `export_paths`.
An export path is an exact absolute path (query strings are allowed), or an
absolute path ending in `/` which permits exactly one following filename.
Downloads must return JSON or CSV, remain on this launch's exact origin and never
redirect. Export streams are bounded to 128 MiB. Import files are copied from a
user-selected content URI with an actual **16 MiB byte limit**, even if the
provider's reported length is missing or false. JSON/CSV contents still undergo
the application's own validation. The UI receives a private read-only copy.

The Gradle `:app:prepareAndroid` task runs `distribution/prepare_android.py` from
the repository root. It must generate:

```
app/build/generated/python/portable_host.py
app/build/generated/python/mobile_app.py
app/build/generated/assets/application/app.json
app/build/generated/assets/application/web/index.html
```

The generator owns whitelisting of application core and sample resources.
It must not copy a real database, credentials, reports, or a virtual environment.
`PYTHON` selects the generator interpreter; `CHAQUOPY_BUILD_PYTHON` selects the
Python 3.13 interpreter used by Chaquopy (it must be 3.13, not 3.12/3.14).

## Build and verify

Use JDK 17, SDK platform 36, build-tools 36.0.0 and Python 3.13. From `android/`:

```sh
./gradlew --no-daemon :app:assembleDebug :app:assembleDebugAndroidTest :app:boundaryPolicyTest :app:lintDebug
./gradlew --no-daemon :app:connectedDebugAndroidTest
```

The second command requires a booted Android emulator/device. Use x86_64 in Linux
CI and test arm64-v8a and a 16 KiB-page emulator/device before claiming hardware
coverage. The APK contains both supported ABIs. The real instrumentation suite
starts the packaged Python, renders the actual WebView, checks unauthenticated
requests/cookie protection, lifecycle token rotation and persistence, exercises
native content-URI import/export and picker cancellation, and checks the ledger's
real backup/restore when that project is bundled. It uses no release JS bridge.
Debug uses an `.debug` application ID suffix to keep test data separate.

To exercise the exact signed release configuration, provide the release signing
environment and build/run with `-PtestBuildType=release`:

```sh
./gradlew -PtestBuildType=release :app:assembleRelease :app:assembleReleaseAndroidTest
./gradlew -PtestBuildType=release :app:connectedReleaseAndroidTest
```

Verify the app and instrumentation APK certificate digests match before installing.
The test APK is a separate artifact and must never be distributed to users. To
verify an in-place update, use direct adb instrumentation so Gradle test cleanup
cannot remove the data between phases. Install both signed APKs, run the
`OfflineAppTest#upgradePreservesPersistedUserData` method with `-e upgradePhase seed`,
`adb install -r` the signed app APK, then run it with `-e upgradePhase verify`.
The runner component is `<android_application_id>.test/androidx.test.runner.AndroidJUnitRunner`.
For the ledger this checks a real stored entry, amount and note; the shared shell
also checks its stable private directory for every bundled application.

The pure Java boundary tests can also be run without an Android SDK:

```sh
mkdir -p build/boundary-tests
javac -d build/boundary-tests app/src/main/java/dev/portableapp/shell/BoundaryPolicy.java app/src/test/java/dev/portableapp/shell/BoundaryPolicyTest.java
java -cp build/boundary-tests dev.portableapp.shell.BoundaryPolicyTest
```

## Signing

Release tasks require all four environment variables:

```
ANDROID_KEYSTORE_PATH
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
```

Only trusted maintainer branches, manual runs and tag publishing jobs receive
these secrets; pull-request builds must not. The keystore is a persistent,
private release key stored outside the repository; preserve it for future
upgrades. The portfolio applications share a release certificate, whose public
fingerprint is pinned in `distribution/android-signing.json`. The build does not
generate a key or use a debug key for releases. Missing signing configuration
fails release packaging. Local builds without the release key use debug tasks.

```sh
./gradlew --no-daemon :app:assembleRelease
"$ANDROID_HOME/build-tools/36.0.0/apksigner" verify --verbose --print-certs app/build/outputs/apk/release/app-release.apk
```

The official Gradle 8.13 wrapper is checked in. Its JAR SHA-256 is
`81a82aaea5abcc8ff68b3dfcb58b3c3c429378efd98e7433460610fecd7ae45f`;
the distribution checksum is pinned in `gradle-wrapper.properties`.

Verification status must be reported separately for Java tests, APK compilation,
emulator instrumentation, and physical devices. Do not treat compilation as a
passed device test.
