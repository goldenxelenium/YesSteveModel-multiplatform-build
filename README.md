# YSM multiplatform builds

One Forge 1.20.1 development JAR with Linux x86_64, macOS Apple Silicon and macOS Intel native libraries. Unofficial; Java 17 required.

CI builds each native library on its target architecture, packages one JAR, and runs JNI smoke checks on all three platforms before publishing a prerelease. The Apple Silicon job uses the Xcode 27 runner; Intel uses macOS 26, the final Intel macOS release. The deployment target is macOS 14.5. Linux glibc requirements are recorded in release notes.

The workflow checks upstream daily. Push a `v*` tag or run it manually to build pinned revisions; manual runs publish only when selected. Releases include checksums, source revisions, license notices and the macOS loader patch.

The macOS loader patch and Intel target are community adaptations. Linux uses Clang 23 with GCC-built dependencies; SVT-AV1 LTO is disabled. A Kokkos mdspan header is supplied when missing. macOS uses Apple Clang and system libc++. Full in-game testing is not automated.

The first GitHub Actions run must pass before a release is published. Source revisions and download hashes are in `build-config.json`.
