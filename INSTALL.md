# Install Windows Scroll Linux v1

## Requirements

- A 64-bit Intel or AMD PC running Linux with glibc 2.34 or newer.
- KDE Plasma 6, using a Wayland session on the default seat.
- systemd and an administrator account.
- A regular mouse with a clickable wheel.

Bazzite KDE is the tested desktop. The installer also includes dependency
instructions for Debian, Ubuntu, Fedora and Arch. Those systems must meet the
requirements above. A supported package manager alone does not make an older
Plasma desktop compatible. GNOME, Xfce, Plasma 5, X11, Alpine, touchpads and ARM
devices are not covered by this prebuilt release.

You can check your desktop in a terminal:

```sh
plasmashell --version
echo "$XDG_SESSION_TYPE"
uname -m
getconf GNU_LIBC_VERSION
```

Look for Plasma 6, `wayland`, `x86_64` and glibc 2.34 or newer.

## Download and install

1. Open the [v1 release page](https://github.com/RobinEclipse/windows-scroll-linux/releases/tag/v1).
2. Under **Assets**, download `windows-scroll-linux-v1-linux-x86_64.tar.gz`.
3. Extract it with your file manager.
4. Open a terminal inside the extracted `windows-scroll-linux-v1` folder.
5. Run:

   ```sh
   bash install.sh
   ```

Run this from your normal desktop account. The installer asks for administrator
authentication when needed. Keep all mouse buttons released while it starts.
It checks the environment, installs the program, and enables automatic startup
on a fresh installation. Existing settings and startup choices are preserved
when updating.

After the first installation, log out and log back in. This lets newly started
applications expose their controls for tab and link detection.

The GitHub **Source code** downloads do not contain the compiled program. Use
the named Linux asset unless you want to build it yourself.

## Missing dependencies

The installer prints the packages your distribution needs. On a regular Debian,
Ubuntu, Fedora or Arch installation, it can install the required packages with:

```sh
bash install.sh --install-deps
```

This uses your distribution's package manager. It may need an Internet connection
and administrator authentication. It does not install packages from arbitrary
download sites or use pip to modify system Python.

On Bazzite and other Fedora Atomic desktops, the installer does not modify the
base image automatically. If a dependency is missing, follow its printed
`rpm-ostree install` command, reboot, then run `bash install.sh` again. Use the
host terminal, not a Toolbox or Distrobox container.

The runtime needs Python GTK 4, D-Bus and AT-SPI bindings, plus the system systemd
and udev libraries. The optional indicator additionally needs Gtk4LayerShell,
Cairo and the PyGObject Cairo bridge. Without those, scrolling works but the
on-screen indicator is unavailable. Availability of these optional packages
differs between distributions.

## Start scrolling

Click and release the wheel over content, then move your mouse. Move farther
from the starting point to scroll faster. Click another mouse button to stop.
The stopping click is consumed; your next click is delivered normally.

Holding the wheel and moving starts temporary scrolling. Releasing the wheel
stops that gesture. Turning the physical wheel or pressing Escape also cancels
scrolling.

Open **Windows Scroll Linux Settings** from the application menu to adjust
speed, direction and the pointer indicator. **Ctrl+Alt+M** pauses or resumes
scrolling. Pause continues to block middle-click paste on handled mice.

## If something is not working

**A tab or link does not respond to middle-click.** Restart the application after
logging out and back in. Native actions require reliable accessibility data.
Custom controls and ambiguous targets use scrolling or suppression instead.

**There is no indicator.** The Gtk4LayerShell library may be unavailable. The
status command reports whether the indicator is available. Scrolling can still
work without it.

**The installer cannot attach a mouse.** Release every mouse button and retry.
Check that you have a regular relative mouse connected and are running on the
host desktop. Touchpads and absolute pointing devices are not supported.

**Scrolling is paused or fails to start.** Check the services and connection:

```sh
midscroll-control status
systemctl status midscroll.service --no-pager
systemctl --user status midscroll-overlay.service --no-pager
```

For recent error messages:

```sh
journalctl -u midscroll.service -n 50 --no-pager
journalctl --user -u midscroll-overlay.service -n 50 --no-pager
```

Review logs before sharing them publicly. They may contain application or device
names. The internal command and service names remain `midscroll` for compatibility
with earlier development installations.

## Update

Download a new release, extract it, and run its `bash install.sh` command. Your
existing configuration is kept. Failed updates attempt to restore the preceding
files and service state. If installation was interrupted, running the installer
again recovers its incomplete transaction before retrying.

## Uninstall

Open a terminal in the extracted download folder and run:

```sh
python3 uninstall.py
```

The uninstaller stops automatic startup and removes the launchers, services and
active configuration. It restores managed desktop preferences when they have
not subsequently been changed. Source versions and recovery copies are retained.
Distribution packages are left installed.

## Verify the download

Download `SHA256SUMS` from the same release. In the download directory, run:

```sh
sha256sum --ignore-missing -c SHA256SUMS
```

The archive should report `OK`. Checksums detect corruption; they are not a
publisher signature.
