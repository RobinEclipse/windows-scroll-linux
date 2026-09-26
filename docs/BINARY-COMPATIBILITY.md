# Linux binary compatibility

The v1 downloadable input daemon targets **x86-64 Linux with glibc 2.34 or
newer**. It uses the ordinary x86-64 GNU target and does not enable native-CPU
optimizations. The complete application also needs KDE Plasma 6 on Wayland and
the Python/GTK/accessibility dependencies described in the installation guide.
The libc requirement alone does not establish desktop compatibility.

The release binary was built in an Ubuntu 22.04 container using Rust 1.98.1 and
the GCC 11 linker. That container provides glibc 2.35. Inspection of the finished
ELF shows that its newest required glibc symbol version is **GLIBC_2.34**.
`--version` runs inside that baseline container and on the development Bazzite
host. This verifies binary loading, not a full desktop test on Ubuntu 22.04.

The daemon directly links to `libc.so.6`, `libm.so.6`, and `libgcc_s.so.1`.
At runtime it loads the distribution's `libsystemd.so.0` and `libudev.so.1`.
These system libraries are not bundled. A musl-static build is not shipped;
the supported binary uses the GNU ABI needed by those host libraries.

Each download has a SHA-256 checksum and `binary-compatibility.json` describing
the actual daemon. The build verifies its ABI with:

```sh
python3 packaging/check_binary.py daemon/target/release/midscrolld
```

The check rejects an unexpected architecture, a non-PIE executable, unexpected
linked libraries, or glibc symbols newer than 2.34. It does not certify every
CPU, kernel, graphics stack, application, or desktop session.

## Rebuilding

GitHub Actions builds inside the pinned Ubuntu 22.04 container, even though the
runner host is Ubuntu 24.04. This prevents a hosted-runner upgrade from silently
raising the daemon's libc requirement. Rust dependencies remain locked in
`daemon/Cargo.lock`. The repository includes its small evdev ABI fix and the
original dependency licenses.

On another Linux architecture, build from source with an appropriate Rust GNU
toolchain and system C linker:

```sh
cargo build --locked --release --manifest-path daemon/Cargo.toml
```

The x86-64 compatibility checker is specifically for the provided x86-64
release, and should not be used to label a different architecture as verified.
Other architectures and libc implementations do not currently have tested
prebuilt downloads. Builds made directly on a newer distribution may require a
newer libc than the downloadable x86-64 release.
