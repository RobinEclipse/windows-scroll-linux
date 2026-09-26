# v1 validation

The release was installed and exercised on Bazzite KDE, Plasma 6, Wayland and
x86_64 Linux. All 20 disposable desktop cases passed, with the original desktop
focus, pointer position and enabled state restored.

Coverage includes click-to-scroll, every mouse button on either of two devices,
wheel cancellation, pause, pending cancellation, text fields, native tab and
link events, and disconnecting and reconnecting a mouse. Native action tests use
owned GTK controls and do not close a user's real browser tabs.

The 46 portable Rust tests, formatting checks and strict Clippy checks passed.
The Python suite covers the helper, deadlines, settings races, optional indicator,
binary compatibility, installation, rollback and cleanup. It passed on the
development host and in the Ubuntu 22.04 build environment with Python 3.10.

Fresh-install checks exercise temporary filesystem roots and mocked service
control. They verify defaults, startup choices, rollback, user preferences,
directory access under a restrictive root umask, and unsafe symlink ancestors.
They are not a substitute for a full fresh desktop installation on every listed
distribution. The actual installed upgrade also passed its service health and
payload-hash checks.

The binary loads on the Ubuntu 22.04 build baseline and the Bazzite desktop.
Its newest required glibc symbol version is 2.34. That is an ABI check, not a
claim that Ubuntu 22.04's older Plasma desktop is supported.

GNOME, Xfce, X11, touchpads, other seats, other CPU architectures and real
multi-user switching are outside this release's desktop validation. An
application can expose incomplete or incorrect accessibility data. These tests
do not establish identical Windows behavior in every application or guarantee
every application's response to a native middle-click.
