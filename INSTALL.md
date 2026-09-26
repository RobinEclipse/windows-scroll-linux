# Install Windows Scroll Linux v1

[Download v1](https://github.com/RobinEclipse/windows-scroll-linux/releases/download/v1/windows-scroll-linux-v1-linux-x86_64.tar.gz)
| [Back to overview](README.md)

These instructions apply to **v1**. The program may report `1.0.0` in command
output; this is the same release version.

## Requirements

- A 64-bit Intel or AMD PC running Linux with glibc 2.34 or newer.
- KDE Plasma 6, using a Wayland session on the default seat (`seat0`).
- systemd and permission to authenticate as an administrator.
- The distribution's Python at `/usr/bin/python3`, plus `pkexec` and a working
  polkit authentication prompt. Python 3.10 is the oldest tested version.
- A regular mouse with a clickable wheel.

Bazzite KDE is the tested desktop. The installer also includes dependency
instructions for Debian, Ubuntu, Fedora and Arch. Those systems must meet the
requirements above. A supported package manager alone does not make an older
Plasma desktop compatible. GNOME, Xfce, Plasma 5, X11 and musl-based systems such
as Alpine are unsupported. This release does not provide touchpad autoscroll
or an ARM download. Other seats and multi-user switching are outside its desktop
validation.

You can check your desktop in a terminal:

```sh
plasmashell --version
echo "$XDG_SESSION_TYPE"
uname -m
getconf GNU_LIBC_VERSION
```

Look for Plasma 6, `wayland`, `x86_64` and glibc 2.34 or newer.
See [v1 validation](docs/VALIDATION.md) for the scope of testing.

## Download and install

1. Open the [v1 release page](https://github.com/RobinEclipse/windows-scroll-linux/releases/tag/v1).
2. Under **Assets**, download **windows-scroll-linux-v1-linux-x86_64.tar.gz**.
3. Extract it with your file manager.
4. Open a terminal inside the extracted **windows-scroll-linux-v1** folder.
5. Run:

   ```sh
   bash install.sh
   ```

Run this from your normal desktop account. The installer asks for administrator
authentication when needed. Keep all mouse buttons released while it starts.
It checks the environment, installs the program, and enables automatic startup
on a fresh installation. Updates preserve your scrolling configuration and
service startup/running choices, but reapply the desktop integration described
below.

After the first installation, log out and log back in. This lets newly started
applications expose their controls for tab and link detection.

The GitHub **Source code** downloads do not contain the compiled program. Use
the named Linux asset unless you want to build it yourself.

If you prefer extracting in a terminal, open one in the directory containing
the downloaded archive and run:

```sh
tar -xzf windows-scroll-linux-v1-linux-x86_64.tar.gz
cd windows-scroll-linux-v1
bash install.sh
```

Keep the extracted folder for the uninstaller. You can also download it again
later.

## What the installer changes

Administrator access is needed to install the program and run a system service
that reads mouse input and creates a virtual mouse. A separate desktop helper
runs as your normal account. A fresh installation starts both automatically
and adds application-menu launchers to the system.

For the account used to install it, the installer also:

- Disables KDE primary selection and GTK middle-click paste. These desktop
  preferences can affect excluded mice and touchpads too. Normal keyboard
  copy and paste, such as Ctrl+C and Ctrl+V in a text editor, still work.
- Enables accessibility integration so applications can report their tabs and
  links. It does not start a screen reader.
- Adds a Plasma login environment file.

Updates reapply the paste and accessibility preferences. The uninstaller
restores the previous managed preferences when those values have not since
been changed. Browser profiles are not edited. The installed services do not
need Internet access.

Internal commands and services retain the name `midscroll` for upgrade
compatibility. This is expected, even though the program is called Windows
Scroll Linux. See [technical details](docs/TECHNICAL.md) for more.

## Missing dependencies

The installer prints the packages your distribution needs. On a regular Debian,
Ubuntu, Fedora or Arch installation, it can install the required packages with:

```sh
bash install.sh --install-deps
```

This uses your distribution's package manager. It may need an Internet connection
and administrator authentication. It does not use pip to modify system Python.
If Python or `pkexec` itself is missing, install it with your distribution's
package manager first. The dependency helper needs them to run.

On Bazzite and other Fedora Atomic desktops, the installer does not modify the
base image automatically. If a dependency is missing, follow its printed
`rpm-ostree install` command, reboot, then run `bash install.sh` again. Use the
host terminal, not a Toolbox or Distrobox container.

The runtime needs Python GTK 4, D-Bus and AT-SPI bindings, plus the system systemd
and udev libraries. The optional indicator additionally needs Gtk4LayerShell,
Cairo and the PyGObject Cairo bridge. Without those, scrolling works but the
on-screen indicator is unavailable. Availability of these optional packages
differs between distributions.

`--install-deps` checks the required runtime dependencies. It does not install
Gtk4LayerShell, and it does no package installation if the required dependencies
already work. For the indicator, install your distribution's GTK4 layer-shell
library and introspection bindings, Cairo and the PyGObject Cairo bridge. Then
restart the desktop helper:

```sh
systemctl --user restart midscroll-overlay.service
```

On Atomic desktops, reboot first if installing those packages required layering
them into the system image.

## Start scrolling

Click and release the wheel over content, then move your mouse. Move farther
from the starting point to scroll faster. Click any mouse button, including the
wheel again, to stop. The stopping click is consumed; your next click is
delivered normally.

For temporary scrolling, hold the wheel and move beyond the dead zone before
releasing it. Release to stop. If you release without crossing the dead zone,
click-to-scroll stays active, even if you held the button for a long time.

Turning the physical wheel or pressing Escape also cancels scrolling. The wheel
movement or Escape still reaches the application. Mouse cancellation applies
to mice handled by the service. Switching windows or locking the desktop also
stops scrolling.

Reliably identified tabs and links receive the application's normal middle-click
action. This can close a tab or open a link, depending on the app. Keep the
pointer still when clicking. Recognition depends on the app's accessibility
information and is not guaranteed for every browser or custom control.

Middle-clicks on the desktop, panels, window borders and popup windows are
ignored. Recognized password fields and single-line inputs also ignore them.
Multiline text areas can scroll.

Open **Windows Scroll Linux Settings** from the application menu to adjust
speed, direction and the pointer indicator. **Ctrl+Alt+M** pauses or resumes
scrolling. Pause suppresses all middle-click actions on handled mice, including
tab and link actions. Pointer movement, other buttons and ordinary wheel
scrolling continue to work.

Native middle-button game controls and CAD panning are unsupported on handled
mice. Excluding a mouse in Settings bypasses input interception for that device;
the account-wide desktop paste preferences still apply. Stopping the input
service also ends interception. Neither case guarantees that every app will
block middle-click paste.

## If something is not working

**A tab or link does not respond to middle-click.** Restart the application after
logging out and back in. Stop moving the pointer briefly before clicking. Native
actions require reliable accessibility data and an unchanged pointer context.
Custom controls and ambiguous targets use scrolling or suppression instead.
Also check that scrolling is not paused: pause blocks these middle-clicks too.

**There is no indicator.** Check the [optional dependencies](#missing-dependencies).
The status command reports whether the indicator is available. Scrolling can
still work without it.

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
names. If you need help, [open an issue](https://github.com/RobinEclipse/windows-scroll-linux/issues/new/choose)
with your desktop version, mouse model, affected app and reproduction steps.

## Update

Download a new release, extract it, and run its `bash install.sh` command. Your
scrolling configuration and startup choices are kept; the desktop integration
preferences are reapplied. Failed updates attempt to restore the preceding files
and service state. If installation was interrupted, running the installer again
recovers its incomplete transaction before retrying.

## Uninstall

From the same desktop account used to install it, open a terminal in the
extracted download folder and run:

```sh
python3 uninstall.py
```

Authenticate when prompted, then log out and back in. If you deleted the folder,
download and extract the v1 release again to get the uninstaller.

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
publisher signature. `binary-compatibility.json` is optional technical metadata;
you do not need it to install the program.
