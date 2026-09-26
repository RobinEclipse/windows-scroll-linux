# Windows Scroll Linux

Windows-style middle-click autoscroll for **KDE Plasma 6 on Wayland**.

Click the mouse wheel, release it, then move the mouse to scroll. Click a mouse
button to stop. Move farther from the starting point to scroll faster, vertically,
horizontally or diagonally.

**Current release: v1. Tested on Bazzite KDE.**

[Download v1 (Linux x86_64)](https://github.com/RobinEclipse/windows-scroll-linux/releases/download/v1/windows-scroll-linux-v1-linux-x86_64.tar.gz)
| [Installation guide](INSTALL.md)
| [Report a problem](https://github.com/RobinEclipse/windows-scroll-linux/issues/new/choose)

## Before you install

You need KDE Plasma 6 with a Wayland session, a regular mouse with a clickable
wheel, systemd and permission to install software as an administrator. The
prebuilt download is for 64-bit Intel or AMD Linux with **glibc 2.34 or newer**.

Bazzite KDE is the tested desktop. Other distributions must meet the same
requirements; full desktop testing on them has not been completed. GNOME, Xfce,
Plasma 5 and X11 are unsupported. This release does not provide touchpad
autoscroll or an ARM download.

## Install v1

1. [Download the installer archive](https://github.com/RobinEclipse/windows-scroll-linux/releases/download/v1/windows-scroll-linux-v1-linux-x86_64.tar.gz)
   and extract it with your file manager.
2. Open a terminal inside the extracted **windows-scroll-linux-v1** folder and run:

   ```sh
   bash install.sh
   ```

3. Follow the administrator prompt. Keep mouse buttons released while the
   installer starts the services. After the first installation, log out and
   log back in.

If dependencies are missing, follow the instructions the installer prints.
The [full guide](INSTALL.md) covers Bazzite, dependencies, updates and removal.
On the release page, choose **windows-scroll-linux-v1-linux-x86_64.tar.gz**.
GitHub's **Source code** archives do not contain the prebuilt program.

## How it behaves

| Action | Result |
| --- | --- |
| Click and release the wheel over content, then move | Autoscroll stays on until you stop it. |
| Hold the wheel and move beyond the dead zone | Scroll temporarily; release the wheel to stop. Holding still and releasing acts like a click. |
| Press any mouse button while autoscrolling | Stop scrolling. This click is consumed; click again to interact with the app. |
| Turn the wheel or press Escape | Stop autoscrolling. The wheel movement or Escape also reaches the app. |
| Middle-click a reliably identified tab or link | Send the app its normal middle-click, such as closing a tab or opening a link. The app decides the result. |

Ordinary middle-click paste is blocked on mice handled by the running service.
Recognized password fields, single-line inputs, desktop areas and panels suppress
the middle-click instead of starting autoscroll.

Open **Windows Scroll Linux Settings** from your application menu to adjust
speed, direction and the optional pointer indicator. **Ctrl+Alt+M** pauses or
resumes scrolling. While paused, middle-click remains blocked on handled mice,
including tab and link actions.

## Limits to know

- **Tab and link detection depends on the app.** The app must expose reliable
  accessibility information about the control under the pointer. Unrecognized
  or ambiguous targets may scroll or do nothing. Closing every browser tab or
  opening every link with the wheel is not guaranteed.
- **This is an approximation of Windows behavior.** It works across application
  content on the supported desktop, but cannot reproduce every app's Windows
  behavior. Native middle-button game controls and CAD panning are unsupported
  on handled mice.
- **The indicator is optional.** Scrolling still works if its drawing libraries
  are unavailable. See [missing dependencies](INSTALL.md#missing-dependencies).
- **Excluded mice bypass interception.** Desktop paste preferences still apply.
  The installer also changes KDE and GTK primary-selection paste settings;
  [the guide explains these changes](INSTALL.md#what-the-installer-changes).

## Help and removal

Start with [troubleshooting](INSTALL.md#if-something-is-not-working). If the
problem persists, [open an issue](https://github.com/RobinEclipse/windows-scroll-linux/issues/new/choose)
with your desktop version, mouse model and steps to reproduce it.

To uninstall, open a terminal in the extracted download folder and run:

```sh
python3 uninstall.py
```

See [removal details](INSTALL.md#uninstall) for what is restored or retained.

## Development and license

[![Build and test](https://github.com/RobinEclipse/windows-scroll-linux/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/RobinEclipse/windows-scroll-linux/actions/workflows/build.yml)

The input engine is written in Rust, with Python desktop integration running as
your normal user. See [building and testing](docs/DEVELOPMENT.md),
[how it works](docs/TECHNICAL.md) and [what was tested for v1](docs/VALIDATION.md).

Released under the [Unlicense](LICENSE). Based on
[gnhen/midscroll](https://github.com/gnhen/midscroll), with a rewritten input
engine. Dependencies retain their own licenses. This is an independent project
and is not affiliated with Microsoft.
