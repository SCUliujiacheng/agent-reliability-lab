# Runtime license sources

The full license texts in this directory are copied unchanged from the official
version-tagged sources listed in `sources.json`. SHA-256 checksums refer to the
original UTF-8/LF bytes; `.gitattributes` preserves them on Windows checkouts.

Android currently packages the Chaquopy 17.0.0 runtime and its Maven target
`com.chaquo.python:target:3.13.9-0` (CPython 3.13.9). Recheck the actual Gradle
target and update the version, source files and checksums together when upgrading.
The application does not patch these runtimes; Chaquopy's runtime integration is
maintained in its upstream source repository.

`distribution.collect_licenses.collect_licenses` also copies production npm
license files from the installed, lockfile-matched `node_modules` packages.
Desktop builds copy the actual build interpreter's `LICENSE*` files, including
the Unix `lib/pythonX.Y` location, plus installed Python distribution notices.
No license text is downloaded during packaging. The output `licenses/components.json`
records component versions, sources, relative license paths and byte hashes.

This is packaged license evidence, not a claim that all transitive runtime
licensing obligations have been independently audited.
