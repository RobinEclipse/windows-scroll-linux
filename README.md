# Windows Scroll Linux

**v1**

[Download v1 for Linux](https://github.com/RobinEclipse/windows-scroll-linux/releases/download/v1/windows-scroll-linux-v1-linux-x86_64.tar.gz)
| [Installation guide](INSTALL.md)

Windows-style mouse wheel click scrolling for Linux. Click the wheel, release
it, then move the mouse to scroll. Click another mouse button to stop.

Works across application content on **KDE Plasma 6 with Wayland**. Tested on
Bazzite KDE. This release does not support GNOME, Xfce, X11 or touchpads.

## What it does

- Click the wheel to start scrolling, or hold it for temporary scrolling.
- Move farther from the starting point to scroll faster.
- Scroll vertically, horizontally or diagonally.
- Stop with a mouse button, the physical wheel or Escape.
- Block ordinary middle-click paste on the mice it handles.
- Keep native middle-click actions on tabs and links when they can be identified.
- Start automatically and provide a settings window for speed and preferences.

The click that stops scrolling is consumed. Your next click works normally.
Pausing keeps middle-click paste blocked. Native middle-button game controls and
CAD panning are not supported. Excluded mice keep their normal behavior.

## Install

You need a 64-bit Intel or AMD Linux PC, KDE Plasma 6, a Wayland session, systemd
and a regular mouse. The prebuilt download requires glibc 2.34 or newer.

1. Open the [v1 release](https://github.com/RobinEclipse/windows-scroll-linux/releases/tag/v1).
2. Download **windows-scroll-linux-v1-linux-x86_64.tar.gz** from **Assets**.
3. Extract it and open a terminal inside the extracted folder.
4. Run:

   ```sh
   bash install.sh
   ```

The installer checks your system and asks for administrator authentication.
Release all mouse buttons while it starts the services. If dependencies are
missing, follow the instructions it prints. Log out and back in after a first
installation so applications can expose their tabs and links reliably.

See the [installation guide](INSTALL.md) for dependencies, troubleshooting,
updates and removal. The automatically generated GitHub source archives are for
developers and do not contain the prebuilt program.

## Use

Click the mouse wheel over content, then move the mouse. Press another mouse
button to stop. Use **Ctrl+Alt+M** to pause or resume scrolling, and open
**Windows Scroll Linux Settings** from your application menu to change settings.

Some applications do not expose their tabs and links reliably. In those cases,
the program uses scrolling or suppresses the click. It cannot exactly reproduce
Windows behavior in every application.

## Uninstall

From the extracted download folder:

```sh
python3 uninstall.py
```

This stops the services and removes the program's launchers. Recovery copies and
source versions are retained.

## Source and license

The input engine is written in Rust. Desktop integration runs separately as
your normal user. See [development and testing](docs/DEVELOPMENT.md).

Released under the [Unlicense](LICENSE). Based on
[gnhen/midscroll](https://github.com/gnhen/midscroll), with a rewritten input
engine. Dependencies retain their own licenses. This is an independent project
and is not affiliated with Microsoft.
