# YSM Linux + macOS community builds

Builds **one Forge 1.20.1 mod JAR** with all three native libraries embedded:

| Platform | Embedded resource | Runner |
| --- | --- | --- |
| Linux amd64 / x86_64, glibc | `META-INF/native/libysm.so` | `ubuntu-24.04` |
| macOS Apple Silicon / arm64 | `META-INF/native/libysm.dylib` | `macos-15` |
| macOS Intel / x86_64 | `META-INF/native/libysm-x86_64.dylib` | `macos-15-intel` |

This is a community development build, not an official YSM release. Linux arm64, musl Linux, Windows and Android are **not** built by this workflow.
The Linux minimum glibc symbol version is measured and recorded in each release.
The macOS deployment target is 14.5; the smoke test runs on macOS 15, not 14.5.
Game runtime: Java 17. A full Minecraft game session is not automated.

## Enable releases

1. Create an empty repository and push this project to its `main` branch.
2. Enable GitHub Actions. The release job requests `contents: write` for the
   repository's built-in `GITHUB_TOKEN`; no personal access token is needed.
3. Run **Build and release Linux + macOS mod** manually for the first validation.
   Leave **publish** off to get a downloadable Actions artifact without a release.
4. After the first successful run, use **publish**, push a `v*` tag, or let the
   daily upstream check publish a prerelease when either upstream changes.

Schedule: daily at 04:43 UTC. It resolves both upstream branch tips to immutable
commit SHAs before starting the matrix. A previously published source/CI revision
combination is skipped. Failed builds are retried by the next scheduled check.
Pushes to `main` and pull requests build the pinned revisions without publishing.
Tag releases also use the pins. Manual builds can choose `pinned` or `latest`.
All releases are labelled prereleases because upstream is an unstable development version.

## Build and release gates

1. Resolve source SHAs once for all jobs.
2. Build Linux and macOS libraries from the **same native commit** on their native hosts.
3. Inspect ELF/Mach-O architecture, load each shared library, and record ABI and SHA-256.
4. Apply the committed macOS Java-loader patch and embed both libraries before `shadowJar`.
5. Run upstream full built-in model materialization and packaged sound-linkage checks.
6. Check that the final JAR contains the exact native bytes from both build jobs.
7. Download that final JAR on all three targets and initialize its JNI runtime
   using Java 17. macOS also checks the native user-directory API.
8. Publish only after **all final-JAR JNI smoke jobs** pass.

Release assets are uploaded to a draft first. Only a completed upload is published;
interrupted drafts can be rebuilt and resumed without overwriting published releases.

The release contains the mod JAR, a ZIP with standalone libraries, source/build
provenance, the loader patch and license notices, and `SHA256SUMS`.
The JAR also embeds provenance at `META-INF/ysm-community-build.json`.

## Explicit adaptations

- **Java loader:** upstream `NativeLibUtil` currently admits Windows/Linux but
  has no production macOS branch. `patches/macos-loader.patch` adds macOS arm64
  selection (`aarch64` and `arm64`) and extraction under `~/Library/Caches/ysm/unstable`.
  It selects a separate Intel dylib on x86_64.
- **Linux:** native project code uses the maintainer's published Clang 23.1.4
  and LLD; dependencies use GCC 13 and static libstdc++. The host glibc is used,
  not the maintainer's glibc 2.19 sysroot.
- **macOS:** both project and dependencies use the runner's Xcode Apple Clang,
  SDK and system libc++. This does not reproduce the maintainer's custom static
  libc++ ABI v2 environment.
- **mdspan:** when the chosen standard library lacks `<mdspan>`, the pinned Kokkos
  reference implementation is supplied as a compiler header in namespace `std`.
- **SVT-AV1:** LTO is disabled through a Conan CMake configuration override,
  avoiding GCC LTO objects in the Linux LLD link.
- Maven Central and Forge are tried before the additional upstream repositories.

The sandbox-only Java bytecode agent, network proxy and `/proc` workarounds from
the initial Linux build are deliberately absent from CI.
`build-config.json` pins source revisions, mdspan and downloaded archive hashes.
Actions are pinned to commit SHAs. Runner SDKs, transitive Java/Python dependencies
and native dependency tags are not fully locked, so byte-for-byte reproducibility
is not claimed. Actual native compiler versions are included in release metadata.

## Reference inspected

[Starrysparklez/YesSteveModel-macOS-build](https://github.com/Starrysparklez/YesSteveModel-macOS-build)
at `c2be56caafd34147919501c4faa9ce19c76c1e13` contains no CI workflow or committed
loader changes compared with its upstream base. Its `3.0-1.20.1` release JAR
contains an arm64 `libysm.dylib` and a modified loader. Its release JAR SHA-256 is
`af23f6821787f72c3f216ceae6726da7ff5d7898e96a0ee4d624aed7961e0735`.
This project rebuilds libraries from source; it does not redistribute that prebuilt binary.

## Local checks

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/ci.py
actionlint .github/workflows/release.yml
git -C /path/to/YesSteveModel apply --check /path/to/patches/macos-loader.patch
```

The first GitHub run is still required to validate the native macOS build and the
complete cross-platform release path. On upstream loader changes the patch fails
closed; review and refresh it instead of silently applying a different edit.
