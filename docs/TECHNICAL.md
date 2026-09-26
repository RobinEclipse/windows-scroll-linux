# How Windows Scroll Linux works

The Rust daemon reads relative mice through evdev and forwards input through
uinput. A supervised polling loop handles movement, gestures, reconnection and
watchdog reporting. It never waits for an application's accessibility response.

An unprivileged desktop helper asks KWin which window is under the pointer.
A separate worker uses AT-SPI to identify tabs, links and editable controls.
Inspection has a deadline. A stalled worker can be replaced independently of
the input loop.

Native middle-clicks require a verified noneditable tab or link, matching window
identity and an unchanged pointer context. Ordinary process siblings, duplicate
titles and ambiguous targets cannot authorize native clicks. Motion, pause,
focus changes and session changes invalidate pending decisions.

Applications can change after inspection or expose incorrect accessibility data.
There is no generic native-middle fallback for ordinary content, but forwarding
native clicks to verified controls cannot guarantee the behavior of every app.
Excluded devices and periods when the service is stopped are outside interception.

The daemon authenticates the helper's Unix UID and active local graphical
session through logind. Only seat0 is supported. Processes sharing the same Unix
account remain trusted. Keyboards are not grabbed or read as event streams;
the daemon checks the current Escape state when needed. Neither installed
service needs Internet access.

The optional indicator uses Gtk4LayerShell. Without that library, scrolling
continues without an on-screen indicator. Pointer restoration passes accumulated
relative movement through normal compositor acceleration, so the final pointer
position can differ from the indicator.

Internal service, executable and configuration names retain `midscroll` for
upgrade compatibility. Internal protocol version 2 and the `v2` installation
link are independent of the public release label **v1**.

## Recovery

Installation validates a staged payload before replacing managed files. It keeps
versioned sources, backups and a transaction journal. Failed updates restore the
previous files and service state. The next installer invocation recovers an
interrupted transaction. This is not recovery performed automatically at boot.

SELinux enforcement is preserved. The active daemon is installed at
`/usr/local/bin/midscroll` so normal executable labels can apply.

See [the reliability review](../REVIEW-RESOLUTION.md) for the defects covered by
the implementation and regression tests.
