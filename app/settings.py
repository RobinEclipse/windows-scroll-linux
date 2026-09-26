#!/usr/bin/python3
"""Settings for Windows Scroll Linux v1."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from apply_settings import BOOLS, DEFAULTS, NUMERIC, parse_assignments, read_values

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk

LABELS = {
    'DEADZONE_PX': ('Dead zone (pixels)', 'Movement inside this distance does not scroll.'),
    'SPEED_MULT': ('Speed multiplier', 'Scales scrolling speed at every distance.'),
    'SPEED_EXP': ('Acceleration exponent', 'Controls how strongly speed increases with distance.'),
    'MAX_PX_PER_SEC': ('Maximum speed (pixels/second)', 'Caps the generated scroll speed.'),
    'PX_PER_NOTCH': ('Pixels per wheel notch', 'Converts the speed into wheel events.'),
    'MAX_DRAG_PX': ('Maximum movement distance', 'Must exceed the dead zone.'),
    'TICK_HZ': ('Scroll update rate (Hz)', 'Higher rates generate more wheel events.'),
    'GHOST_SCALE': ('Indicator movement scale', 'Scales the movement of the optional pointer indicator.'),
    'NATURAL': ('Reverse scrolling direction', 'Invert the generated scroll direction.'),
    'GHOST_CURSOR': ('Show moving pointer indicator', 'Hides only the moving pointer when the visual indicator is available.'),
    'SNAP_CURSOR_ON_RELEASE': ('Move pointer when scrolling ends', 'Restore accumulated pointer movement when scrolling ends.'),
}


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Windows Scroll Linux v1 Settings', default_width=660,
                         default_height=730)
        self.controls = {}
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        self.set_child(outer)
        note = Gtk.Label(xalign=0, wrap=True, label=(
            'Click the wheel to scroll; another mouse button stops scrolling. '
            'Pausing keeps middle-click paste blocked. Native game and CAD '
            'middle-button overrides are not supported. The optional visual indicator '
            'requires GTK4 layer shell.'))
        outer.append(note)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        outer.append(scroll)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        scroll.set_child(rows)
        for key, (minimum, maximum, default) in NUMERIC.items():
            digits = 6 if key == 'SPEED_MULT' else (2 if key in ('SPEED_EXP', 'GHOST_SCALE') else 0)
            step = 0.001 if key == 'SPEED_MULT' else (0.1 if digits else 1)
            control = Gtk.SpinButton.new_with_range(minimum, maximum, step)
            control.set_digits(digits)
            control.set_value(default)
            self.controls[key] = control
            rows.append(self.row(key, control))
        for key in BOOLS:
            control = Gtk.Switch(valign=Gtk.Align.CENTER)
            self.controls[key] = control
            rows.append(self.row(key, control))
        for key, title, detail in (
            ('IGNORE_DEVICES', 'Excluded mouse devices', 'Comma-separated mouse names, vendor:product IDs, or /dev/input paths. Excluded devices retain their native middle-click behavior, including paste.'),):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(Gtk.Label(label=title, xalign=0))
            explanation = Gtk.Label(label=detail, xalign=0, wrap=True)
            explanation.add_css_class('dim-label')
            box.append(explanation)
            entry = Gtk.Entry(max_length=8192)
            self.controls[key] = entry
            box.append(entry)
            rows.append(box)
        footer = Gtk.Box(spacing=8)
        self.status = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        footer.append(self.status)
        about = Gtk.Button(label='About')
        about.connect('clicked', self.about)
        footer.append(about)
        reset = Gtk.Button(label='Load defaults')
        reset.connect('clicked', lambda _button: self.load(DEFAULTS))
        footer.append(reset)
        self.apply_button = Gtk.Button(label='Apply')
        self.apply_button.add_css_class('suggested-action')
        self.apply_button.connect('clicked', self.apply)
        footer.append(self.apply_button)
        outer.append(footer)
        try:
            self.load(read_values())
        except (OSError, ValueError) as exc:
            self.load(DEFAULTS)
            self.status.set_text(f'Could not load existing settings: {exc}')

    def about(self, _button):
        dialog = Gtk.AboutDialog(transient_for=self, modal=True,
                                 program_name='Windows Scroll Linux', version='1',
                                 comments='Click-to-scroll and middle-click paste blocking for KDE Wayland.')
        license_path = Path(__file__).resolve().parent.parent / 'LICENSE'
        try:
            dialog.set_license(license_path.read_text())
        except OSError:
            pass
        dialog.present()

    def row(self, key, control):
        row = Gtk.Box(spacing=12)
        title, description = LABELS[key]
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        column.append(Gtk.Label(label=title, xalign=0))
        detail = Gtk.Label(label=description, xalign=0, wrap=True)
        detail.add_css_class('dim-label')
        column.append(detail)
        row.append(column)
        row.append(control)
        return row

    def load(self, values):
        for key, control in self.controls.items():
            if key in NUMERIC:
                control.set_value(values[key])
            elif key in BOOLS:
                control.set_active(values[key])
            else:
                control.set_text(values[key])
        self.status.set_text('Press Apply to save changes.')

    def apply(self, _button):
        args = []
        for key, control in self.controls.items():
            if key in NUMERIC:
                control.update()
                value = format(control.get_value(), '.15g')
            elif key in BOOLS:
                value = 'true' if control.get_active() else 'false'
            else:
                value = control.get_text()
            args.append(f'{key}={value}')
        try:
            parse_assignments(args)
            process = Gio.Subprocess.new(
                ['/usr/bin/pkexec', '/usr/local/bin/midscroll-apply', *args],
                Gio.SubprocessFlags.STDERR_PIPE)
        except (GLib.Error, ValueError) as exc:
            self.status.set_text(f'Not applied: {exc}')
            return
        self.apply_button.set_sensitive(False)
        self.status.set_text('Applying...')
        process.communicate_utf8_async(None, None, self.applied, None)

    def applied(self, process, result, _data):
        self.apply_button.set_sensitive(True)
        try:
            _ok, _out, error = process.communicate_utf8_finish(result)
            if process.get_successful():
                self.status.set_text('Applied; Windows Scroll Linux v1 is ready.')
            else:
                self.status.set_text('Not applied: ' + ((error or '').strip()[-700:] or 'authorization dismissed'))
        except GLib.Error as exc:
            self.status.set_text(f'Not applied: {exc.message}')


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.github.gnhen.midscroll.Settings')
        GLib.set_application_name('Windows Scroll Linux v1')

    def do_activate(self):
        window = self.props.active_window or Window(self)
        window.present()


if __name__ == '__main__':
    raise SystemExit(App().run(sys.argv))
