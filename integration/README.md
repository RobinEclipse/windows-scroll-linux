# Disposable desktop integration tests

Run this only after the Windows Scroll Linux daemon and session helper are installed and active in the current KDE Wayland session. The suite opens two temporary GTK windows and creates two temporary virtual mice. It takes approximately 45 to 65 seconds. Do not move or click the physical mouse during the run.

From the source directory, run as the desktop user:

```sh
GDK_BACKEND=wayland python3 integration/run.py --output live-results.json
```

The runner requests one system authentication prompt through `pkexec` for its constrained mouse injector. Do not run the runner itself as root. Python evdev, dbus-python, PyGObject and GTK4 must be available to `/usr/bin/python3`. The runtime helper has its own previously documented dependencies.

The injector is a short-lived test process, not an installed service. It accepts only mouse buttons and bounded relative mouse/wheel movements from the invoking user's private runtime socket. It cannot generate keyboard events. It uses isolated Python mode, verifies the caller UID, never runs a shell, and closes its devices on disconnect, error or its watchdog deadline.

The 20 live cases cover:

- A quick click latches scrolling in a window launched with accessibility disabled.
- Left, right, middle and both side buttons stop scrolling from either mouse.
- The stopping click is consumed; the next left click is delivered normally.
- Physical wheel movement cancels scrolling from either mouse.
- Pause suppresses middle-clicks.
- Immediate pending-request cancellation and pause cannot revive scrolling later.
- Unknown and recognized text entries receive no middle events and retain their text.
- The disposable recognized tab and link receive one balanced native middle down/up pair.
- Disconnecting and recreating a mouse with the same name restores interception. Results record whether the kernel reused the same event path.

The fixture records GTK GestureClick press/release callbacks before claiming middle-clicks, preventing real clipboard paste or URL activation even when a regression causes the test to fail. Native cases first verify that the GTK observer receives an ordinary left-button pair. A leaked event still fails the relevant test. Native action tests therefore verify event delivery to the owned control; they do not close a real browser tab or open a real link.

Before each click the runner verifies that the pointer and keyboard focus belong to its disposable window. It saves the original active window, pointer position and enabled setting. Cleanup pauses capture, releases every test mouse button and restores the pointer before closing both virtual mice and test windows. Focus and the enabled setting are then restored independently, so a window-manager failure cannot leave scrolling paused. Idempotent KWin commands have at most three attempts. The JSON includes initial and final pointer, focus and enabled-state snapshots, plus each cleanup step. Ctrl+C requests this same cleanup path. SIGKILL or power loss cannot run application cleanup; the injector still releases its devices when its socket closes or watchdog expires.

The JSON report is written incrementally, includes per-case evidence and GTK button callback data, and records cleanup failures. Failed cases preserve both disposable fixtures’ state and helper diagnostics. Use `--only native` or `--only hotplug` for focused reruns; omit it for all 20 cases. Fixture-only GTK logs are saved next to the report if a run fails. The runner exits nonzero on any failure. This does not replace deterministic unit tests: live inspection may complete before a pause races with it, and kernel event-path reuse is not guaranteed.

No installation, existing application content, user browser tab, or actual keyboard input is modified by the test runner.
