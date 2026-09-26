# Windows Scroll Linux v1 installer internals

The downloadable package installs a fresh copy or updates an existing managed
installation on KDE Plasma 6 Wayland with systemd and an active local seat0
session. Internal midscroll names and the v2 helper-version link are retained
for upgrade compatibility. See the top-level INSTALL.md for user instructions.

Build a Linux release binary and generate its dependency notices, then run:

```sh
python3 packaging/build_payload.py dist/windows-scroll-linux-v1 --binary daemon/target/release/midscrolld
```

The destination must not exist. The --binary argument can select a release built
against an older glibc baseline. The builder checks its version against Cargo.toml.
The published x86_64 GNU binary requires glibc 2.34 or later. The local evdev patch
and other Rust dependencies are pinned by Cargo.lock. Runtime libsystemd and
libudev remain supplied by the distribution.

The generated folder includes install.sh, install.py, uninstall.py, public
documentation, and a complete manifest and payload. Start it as the desktop user:

```sh
bash install.sh
```

The launcher requests administrator authentication through pkexec. If required
desktop libraries are missing, the installer prints distribution package commands.
On supported mutable distributions this option runs those commands:

```sh
bash install.sh --install-deps
```

Debian and Ubuntu use apt, Fedora uses dnf, and Arch uses pacman without a partial
repository refresh or full-system upgrade. Dependency installation covers GTK4,
Python GObject/D-Bus bindings, the AT-SPI client and activation service, and required
system libraries. The check tests the user's actual accessibility bus as well as
Python imports. On immutable rpm-ostree systems, missing libraries produce explicit
layering and reboot instructions rather than changing the operating-system image
automatically. Shared dependency packages are retained on removal or rollback.

The visual indicator is optional. The helper launcher first detects system
Gtk4LayerShell bindings and preloads the matching library before Python imports
GTK. An existing private compatibility library can be used as a fallback. Neither
private libraries nor an earlier installation are required. If visual dependencies
are unavailable, scrolling and controls continue without drawing the indicator.

## Preflight and transaction

Before changing installed files, preflight checks that the binary runs, that the
invoking user owns the active local Wayland session, and that the actual KWin
compositor reports version 6. Unsupported desktops are rejected before dependency
installation. Existing configuration is preserved and validated by the Rust parser.
A fresh install gets generic defaults with no personal device exclusions. Unknown
preexisting files at managed integration paths are not replaced.

All payload files are copied into root-owned staging without following links.
Expected paths and SHA-256 hashes are checked before staged code is executed. The
manifest detects corruption and changes; it is not a publisher signature.

Versioned helper sources and a reference binary live under
/usr/local/lib/midscroll/releases/. An atomic v2 symlink selects the helper.
The active ELF executable is installed at /usr/local/bin/midscroll, where the
normal SELinux executable label permits the service domain transition. Labels are
restored before starting services when restorecon is available. Enforcement is not
disabled and no custom allow policy is installed.

Fresh installs enable both startup units and start them. Upgrades preserve existing
startup and running states. Readiness requires a stable daemon process over a
watchdog interval, at least one attached relative mouse, and a connected desktop
helper when those services are started. Release mouse buttons while services
restart so replacement devices can attach cleanly.

Root file backups and durable journals live under
/var/lib/midscroll/v2-transactions/. Rollback restores prior files, removes newly
created entries, undoes new startup links, and restores prior running states.
Another installer invocation recovers an interrupted uncommitted attempt before
retrying. Recovery after forced termination or power loss is not automatic at boot.

## Desktop setup and removal

All home-directory mutations run as the invoking desktop user. The root installer
does not write through user-controlled home-directory paths. A packaged setup
helper records prior preferences in ~/.local/share/windows-scroll-linux, disables
KDE/GTK primary-selection paste, enables accessibility without starting a screen
reader, and installs a managed Plasma login environment file. Applications that
initialize accessibility only at startup may need a new login.

Preference rollback restores exact prior file bytes when the file still matches
the applied version. If unrelated edits were made meanwhile, it restores only
managed keys that still carry the managed value. Existing environment files with
different contents are not replaced. The oldest successful installation's prior
values are retained for removal.

Running python3 uninstall.py stops and disables services, removes launchers, units,
configuration and the active version link, and restores preferences as the desktop
user. Modified user values and unrelated files are retained. Private libraries,
historical source versions, and root recovery backups are retained. Browser
profiles are not edited by this installer.
