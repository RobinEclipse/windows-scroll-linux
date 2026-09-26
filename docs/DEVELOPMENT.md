# Development and testing

## Build

Use a recent stable Rust toolchain, a C linker and Python 3. The release toolchain
is pinned in the GitHub Actions workflow. From the repository root:

```sh
cargo build --manifest-path daemon/Cargo.toml --release --locked
python3 packaging/generate_notices.py
python3 packaging/build_payload.py dist
```

The resulting `dist` directory contains an installer and payload. Building on a
newer distribution can raise the binary's minimum glibc version. Official x86_64
release builds use an Ubuntu 22.04 container and check ELF version requirements.
The container is a build environment, not a claim of Plasma 5 compatibility.

Other architectures require a native source build and are not covered by the
prebuilt x86_64 download or the current desktop validation.

## Automated checks

```sh
cargo test --manifest-path daemon/Cargo.toml --locked
cargo fmt --manifest-path daemon/Cargo.toml --check
cargo clippy --manifest-path daemon/Cargo.toml --locked --all-targets -- -D warnings
python3 -m unittest discover -s tests -v
```

The Python tests need the runtime Python GTK and D-Bus dependencies listed in
the installation guide. They use isolated mocks and temporary files. Rust tests
that need a real desktop session or synthetic input devices are ignored by
default. Run those separately with appropriate input permissions and
`--ignored --test-threads=1 --nocapture`.

The [desktop integration suite](../integration/README.md) uses disposable GTK
windows and virtual mice. Run it only on a test desktop with no physical input
during the run. It restores focus, pointer position and the enabled state, and
reports cleanup failures explicitly.

## Dependency patch

`daemon/vendor/evdev` contains evdev 0.13.2 with a minimal Linux `UI_SET_PHYS`
ioctl-size correction. Cargo selects it through a local patch. Its upstream
archive hash and the exact change are in
`daemon/vendor/evdev/MIDSCROLL-PATCH.md`. Original licenses are retained.

`THIRD_PARTY_NOTICES.md` and `RUST_STDLIB_NOTICES.html` contain dependency and
toolchain notices. Do not remove or rewrite third-party license texts when
editing product copy.
