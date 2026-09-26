---
name: Bug report
about: Report a problem with Windows Scroll Linux
title: ''
labels: ''
assignees: ''
---

The prebuilt release supports KDE Plasma 6 on Wayland. Bazzite KDE is the tested desktop.

### System and version

- Release or source commit (public v1 reports binary 1.0.0 with `midscroll --version`):
- Linux distribution and version:
- Plasma version (`plasmashell --version`):
- Session type (`echo "$XDG_SESSION_TYPE"`):
- Mouse model and connection (USB, Bluetooth, or wireless receiver):
- Affected application and version (include Flatpak or native package):

### Steps to reproduce

Describe where you click and move the mouse, and whether it happens every time.

1.
2.
3.

### Expected behavior

### Actual behavior

### Optional diagnostics

Paste relevant output below. Review it for personal details, application names, and device names before posting publicly.

```sh
midscroll-control status
journalctl -u midscroll.service -n 50 --no-pager
journalctl --user -u midscroll-overlay.service -n 50 --no-pager
```

```text
Paste relevant output here.
```
