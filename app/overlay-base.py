#!/usr/bin/python3
"""Click-through KDE Wayland anchor/ghost indicator; no input or socket logic."""
import array
import logging
import os
from pathlib import Path
import struct
import time
import cairo
import gi
# A Cairo import alone does not provide PyGObject's drawing callback bridge.
gi.require_foreign('cairo')
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gdk, GLib, Gtk
from gi.repository import Gtk4LayerShell as LayerShell
log = logging.getLogger('midscroll-overlay')
ICON_PATH = str(Path(__file__).with_name('move-all.svg'))
BADGE_PX = 42
ICON_PX = 24
GHOST_W = 22
GHOST_H = 34
GHOST_ALPHA = .85
STALE_SEC = 2
CURSOR_DIRS = ('~/.local/share/icons', '~/.icons', '/usr/share/icons', '/usr/local/share/icons', '/usr/share/pixmaps')
CURSOR_NAMES = ('default', 'left_ptr', 'arrow', 'top_left_arrow')
DEFAULT_CURSOR_SIZE = 24
XCURSOR_IMAGE = 0xfffd0002
MAX_CURSOR_PX = 256
CSS = 'window { background: transparent; } .badge { background-color: rgba(30,30,32,0.82); border: 1px solid rgba(255,255,255,0.28); border-radius:9999px; }'


def kde_input_setting(key, fallback=None):
    """A value from kcminputrc, where KDE keeps the cursor settings."""
    path = os.path.expanduser("~/.config/kcminputrc")
    try:
        with open(path) as f:
            for line in f:
                name, _, value = line.partition("=")
                if name.strip() == key and value.strip():
                    return value.strip()
    except OSError:
        pass
    return fallback

def cursor_theme_name():
    return (os.environ.get("XCURSOR_THEME")
            or kde_input_setting("cursorTheme", "default"))

def cursor_size():
    for value in (os.environ.get("XCURSOR_SIZE"),
                  kde_input_setting("cursorSize")):
        try:
            size = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < size <= MAX_CURSOR_PX:
            return size
    return DEFAULT_CURSOR_SIZE

def find_cursor_file(theme, seen=None):
    """Path to a theme's pointer cursor, following Inherits= if needed."""
    seen = seen if seen is not None else set()
    if not theme or theme in seen or len(seen) > 8:
        return None
    seen.add(theme)
    inherits = []
    for directory in CURSOR_DIRS:
        base = os.path.join(os.path.expanduser(directory), theme)
        for name in CURSOR_NAMES:
            path = os.path.join(base, "cursors", name)
            if os.path.isfile(path):
                return path
        try:
            with open(os.path.join(base, "index.theme")) as f:
                for line in f:
                    if line.startswith("Inherits"):
                        inherits += [p.strip() for p in
                                     line.partition("=")[2].split(",")
                                     if p.strip()]
        except OSError:
            pass
    for parent in inherits:
        path = find_cursor_file(parent, seen)
        if path:
            return path
    return None

def read_xcursor(path, want):
    """(pixels, width, height, xhot, yhot) for one image in an Xcursor file.

    Xcursor is a small container: a header, a table of contents, then
    chunks. We take the image chunk whose nominal size is closest to the
    one the compositor would have picked, and the first frame if the
    cursor happens to be animated. Only sizes we would actually draw are
    accepted, so a corrupt or hostile file can't make us map something
    enormous.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(4 << 20)
    except OSError:
        return None
    if len(data) < 16 or data[:4] != b"Xcur":
        return None
    _magic, header, _version, ntoc = struct.unpack_from("<4sIII", data)
    best = None
    for i in range(min(ntoc, 1024)):
        try:
            ctype, nominal, pos = struct.unpack_from("<III", data,
                                                     header + i * 12)
        except struct.error:
            break
        if ctype != XCURSOR_IMAGE:
            continue
        if best is None or abs(nominal - want) < abs(best[0] - want):
            best = (nominal, pos)
    if best is None:
        return None
    try:
        (_size, _type, _subtype, _version, width, height, xhot, yhot,
         _delay) = struct.unpack_from("<9I", data, best[1])
    except struct.error:
        return None
    if not (0 < width <= MAX_CURSOR_PX and 0 < height <= MAX_CURSOR_PX):
        return None
    if not (xhot <= width and yhot <= height):
        return None
    off = best[1] + 36
    count = width * height
    try:
        pixels = struct.unpack_from(f"<{count}I", data, off)
    except struct.error:
        return None
    # Xcursor stores premultiplied ARGB little-endian, which is what a
    # cairo ARGB32 surface wants once it is in this machine's word order.
    return array.array("I", pixels).tobytes(), width, height, xhot, yhot

def load_theme_cursor():
    """Your own pointer image as (surface, xhot, yhot), or None.

    The ghost should look like the cursor you already have, so it is
    read straight out of the active Xcursor theme - the same file the
    compositor draws from - rather than approximated.
    """
    path = find_cursor_file(cursor_theme_name())
    if not path:
        return None
    image = read_xcursor(path, cursor_size())
    if not image:
        log.warning("could not read the cursor theme at %s; drawing a "
                    "plain arrow instead", path)
        return None
    pixels, width, height, xhot, yhot = image
    stride = cairo.ImageSurface.format_stride_for_width(
        cairo.FORMAT_ARGB32, width)
    surface = cairo.ImageSurface.create_for_data(
        bytearray(pixels), cairo.FORMAT_ARGB32, width, height, stride)
    log.info("ghost cursor: %s (%dx%d, hotspot %d,%d)",
             path, width, height, xhot, yhot)
    return surface, xhot, yhot

def draw_fallback_ghost(cr, width, height):
    """A plain arrow, for when the theme can't be read."""
    s = min(width / GHOST_W, height / GHOST_H)
    cr.scale(s, s)
    cr.move_to(1, 1)
    cr.line_to(1, 25)
    cr.line_to(6.5, 20)
    cr.line_to(10, 28.5)
    cr.line_to(13.5, 27)
    cr.line_to(10, 19)
    cr.line_to(17, 18.5)
    cr.close_path()
    cr.set_source_rgba(1, 1, 1, 0.72)
    cr.fill_preserve()
    cr.set_source_rgba(0, 0, 0, 0.85)
    cr.set_line_width(1.5)
    cr.stroke()

def make_ghost_drawer(cursor):
    """Draw function for the ghost: your own pointer if we have it."""
    def draw(_area, cr, width, height, *_data):
        if cursor is None:
            draw_fallback_ghost(cr, width, height)
            return
        cr.set_source_surface(cursor[0], 0, 0)
        # Slightly see-through: it is a copy of your cursor, and it
        # should be readable as one rather than mistaken for the real
        # pointer, which is parked back at the badge.
        cr.paint_with_alpha(GHOST_ALPHA)
    return draw

class Overlay:
    """The badge and ghost cursor, on one click-through surface."""

    available = True
    reason = ''

    def __init__(self, app):
        if not LayerShell.is_supported():
            raise RuntimeError('The compositor does not support the Wayland layer-shell protocol.')
        self.win = Gtk.Window(application=app)
        LayerShell.init_for_window(self.win)
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self.win, "midscroll")
        LayerShell.set_keyboard_mode(self.win,
                                     LayerShell.KeyboardMode.NONE)
        LayerShell.set_exclusive_zone(self.win, -1)
        # All four edges: the surface covers the monitor, so the ghost
        # can be drawn anywhere on it. Nothing on it takes input.
        for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                     LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self.win, edge, True)

        icon = Gtk.Image.new_from_file(ICON_PATH)
        icon.set_pixel_size(ICON_PX)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_hexpand(True)
        icon.set_vexpand(True)
        self.badge = Gtk.Box()
        self.badge.add_css_class("badge")
        self.badge.set_size_request(BADGE_PX, BADGE_PX)
        self.badge.append(icon)
        cursor = load_theme_cursor()
        self.ghost = Gtk.DrawingArea()
        if cursor is None:
            self.hotspot = (1, 1)  # the fallback arrow's tip
            self.ghost_size = (GHOST_W, GHOST_H)
            self.ghost.set_size_request(*self.ghost_size)
        else:
            self.hotspot = (cursor[1], cursor[2])
            self.ghost_size = (cursor[0].get_width(), cursor[0].get_height())
            self.ghost.set_size_request(*self.ghost_size)
        self.ghost.set_draw_func(make_ghost_drawer(cursor))
        self.fixed = Gtk.Fixed()
        self.fixed.put(self.badge, 0, 0)
        self.fixed.put(self.ghost, 0, 0)
        self.win.set_child(self.fixed)

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # The surface is recreated whenever the window is shown again,
        # and a full-screen surface that took input would swallow every
        # click in the session, so this is re-applied on every map -
        # never assumed to have survived from last time.
        self.win.connect("realize", self._make_click_through)
        self.win.connect("map", self._make_click_through)
        self.active = False
        self.seq = 0        # discards stale position queries
        self.anchor = None  # where the pointer is pinned, monitor-local
        self.offset = (0, 0)
        self.geo = None     # geometry of the monitor we are drawn on
        self.last_line = time.monotonic()
        GLib.timeout_add_seconds(1, self._check_stale)

    def _make_click_through(self, *_):
        surface = self.win.get_surface()
        if surface is not None:
            surface.set_input_region(cairo.Region())

    def _check_stale(self):
        """Watchdog for the two ways this could go wrong.

        Never leave the overlay up if the daemon stops talking, and
        re-assert the empty input region while it is up, so even a
        missed map signal can only cost a fraction of a second of
        clicks rather than the rest of the session.
        """
        if self.active:
            self._make_click_through()
            if time.monotonic() - self.last_line > STALE_SEC:
                log.warning("no state from midscroll for %.0fs; hiding",
                            STALE_SEC)
                self.set_active(False)
        return True

    def note_line(self):
        self.last_line = time.monotonic()

    def set_active(self, active):
        self.note_line()
        self.active = bool(active)
        self.seq += 1
        if not active:
            self.win.set_visible(False)
            self.anchor = None
            self.offset = (0, 0)

    def start(self, x, y, ghost=True):
        self.note_line()
        self.active = True
        self.seq += 1
        self.ghost.set_visible(bool(ghost))
        self._anchor_at(x, y)
        self.offset = (0, 0)
        self._layout()
        self.win.set_visible(True)
        self._make_click_through()

    def set_offset(self, dx, dy):
        """Where the ghost is, relative to the anchor."""
        self.note_line()
        self.offset = (dx, dy)
        if self.active and self.anchor is not None:
            self._layout()

    def _anchor_at(self, x, y):
        # Layer-shell surfaces belong to one output; find the monitor
        # holding the (global) anchor and work in its coordinates.
        monitors = Gdk.Display.get_default().get_monitors()
        self.geo = None
        for i in range(monitors.get_n_items()):
            mon = monitors.get_item(i)
            geo = mon.get_geometry()
            if (geo.x <= x < geo.x + geo.width
                    and geo.y <= y < geo.y + geo.height):
                LayerShell.set_monitor(self.win, mon)
                self.geo = geo
                break
        if self.geo is None:
            self.anchor = (x, y)
        else:
            self.anchor = (x - self.geo.x, y - self.geo.y)

    def _layout(self):
        if self.anchor is None:
            return
        ax, ay = self.anchor
        self.fixed.move(self.badge, ax - BADGE_PX // 2,
                        ay - BADGE_PX // 2)
        gx, gy = ax + self.offset[0], ay + self.offset[1]
        if self.geo is not None:
            # Windows stops the cursor at the screen edge; so do we.
            gx = max(0, min(gx, self.geo.width - 1))
            gy = max(0, min(gy, self.geo.height - 1))
        # Place the image by its hotspot, the way the compositor
        # places the real one, so the ghost points at the same pixel.
        self.fixed.move(self.ghost, gx - self.hotspot[0],
                        gy - self.hotspot[1])
