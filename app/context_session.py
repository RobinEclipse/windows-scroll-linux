#!/usr/bin/python3
"""Unprivileged v2 KDE helper. Mouse processing never waits in this process.

The socket reader, GLib control/overlay loop and AT-SPI inspection run separately.
Generation changes and reply enqueueing share one lock, preventing stale replies
from being sent after pause, focus, lock, session or connection cancellation.
"""
from collections import Counter
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import queue
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time
import dbus
import dbus.service
import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gio, GLib, Gtk
try:
    gi.require_version('GLibUnix', '2.0')
    from gi.repository import GLibUnix
    add_unix_signal = GLibUnix.signal_add
except (ImportError, ValueError):
    # Older GI packages expose this API through GLib rather than GLibUnix.
    add_unix_signal = GLib.unix_signal_add

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from context_policy import basic_policy
from inspection_client import InspectionClient
from session_identity import SessionIdentity
from kwin_context import KWinContext, same_target

CONFIG = Path.home() / '.config/midscroll-context.json'
SOCKET = '/run/midscroll/state.sock'
MAX_FRAME = 4096
QUERY_BUDGET = .160
log = logging.getLogger('midscroll-context')
PRODUCT_NAME = 'Windows Scroll Linux'
PUBLIC_VERSION = '1'
PRODUCT_LABEL = PRODUCT_NAME + ' v' + PUBLIC_VERSION


def load_overlay():
    # Gtk4LayerShell interposes Wayland symbols and must precede GTK in the
    # process loader. The launcher supplies this preload. Loading the optional
    # typelib here after GTK without it can fail in native code, beyond the
    # exception boundary used for missing optional dependencies below.
    preloads = os.environ.get('LD_PRELOAD', '').replace(':', ' ').split()
    if not any(Path(value).name.startswith('libgtk4-layer-shell.so') for value in preloads):
        raise RuntimeError('Gtk4LayerShell was not preloaded. Use the installed midscroll-overlay launcher for the visual indicator.')
    spec = importlib.util.spec_from_file_location('midscroll_overlay', ROOT / 'overlay-base.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NullOverlay:
    """No visual surface, with explicit status; mouse policy is unaffected."""
    available = False
    def __init__(self, reason):
        self.reason = reason
    def start(self, x, y, ghost=True):
        pass
    def set_active(self, active):
        pass
    def set_offset(self, dx, dy):
        pass


def create_overlay(app, loader=None):
    try:
        module = (loader or load_overlay)()
        return module.Overlay(app)
    except Exception as error:
        # Optional rendering dependencies must not disable paste protection or
        # UI inspection. Keep the reason visible through the status command.
        reason = type(error).__name__ + ': ' + str(error)
        log.warning('Visual indicator unavailable; mouse handling does not depend on it: %s', reason)
        return NullOverlay(reason[:300])


def indicator_status(overlay):
    if overlay is None:
        return {'available': False, 'reason': 'The visual indicator is starting.'}
    available = bool(getattr(overlay, 'available', False))
    return {'available': available, 'reason': '' if available else getattr(overlay, 'reason', 'The visual indicator is unavailable.')}


def read_config():
    try:
        if CONFIG.stat().st_size <= 16384:
            data = json.loads(CONFIG.read_text())
            if isinstance(data, dict):
                return {'enabled': data.get('enabled', True) is True,
                        'app_modes': data.get('app_modes', {}) if isinstance(data.get('app_modes', {}), dict) else {}}
    except (OSError, ValueError):
        pass
    return {'enabled': True, 'app_modes': {}}


def trustworthy_socket(path):
    try:
        parent = os.lstat(str(Path(path).parent))
        entry = os.lstat(path)
        return (stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022
                and stat.S_ISSOCK(entry.st_mode) and entry.st_uid == 0)
    except OSError:
        return False


def query_valid(message):
    return (isinstance(message, dict) and message.get('type') == 'query'
            and type(message.get('id')) is int and 0 <= message['id'] < 2 ** 64
            and type(message.get('epoch')) is int and 0 <= message['epoch'] < 2 ** 64
            and type(message.get('allow_native')) is bool)


class Session:
    def __init__(self, app=None):
        self.app = app
        self.overlay = None
        self.lock = threading.RLock()
        # Always acquire config_lock before lock when both are needed.
        self.config_lock = threading.RLock()
        self.stop = threading.Event()
        self.disconnect = threading.Event()
        self.outbox = queue.Queue(maxsize=64)
        self.epoch = 0
        self.connection = 0
        self.connected = False
        self.connection_error = ''
        self.connection_log_time = 0.
        self.focus = ''
        self.screen_locked = False
        self.identity = None
        self.identity_reader = SessionIdentity()
        self.config = read_config()
        self.last = {}
        self.stats = Counter()
        self.backend_status = {}
        self.busy = None
        self.inspector = InspectionClient()
        self.kwin = KWinContext(self.on_focus, self.toggle)
        self.control = Control(self)
        try:
            self.screen_locked = bool(self.kwin.bus.call_blocking('org.freedesktop.ScreenSaver', '/ScreenSaver', 'org.freedesktop.ScreenSaver', 'GetActive', '', (), timeout=.3))
            self.kwin.bus.add_signal_receiver(self.on_lock, signal_name='ActiveChanged', dbus_interface='org.freedesktop.ScreenSaver')
        except dbus.DBusException:
            # Missing lock knowledge suppresses input until a lock-state signal.
            self.screen_locked = True
            log.warning('Screen-lock state unavailable; autoscroll is paused')

    def _emit(self, message):
        """Call with self.lock held; queueing cannot block control/GLib callbacks."""
        if self.connected:
            try:
                self.outbox.put_nowait((self.connection, message))
            except queue.Full:
                self.disconnect.set()

    def _state(self, kind='state'):
        session, seat = self.identity or ('', '')
        value = {'type': kind, 'session': session, 'seat': seat, 'epoch': self.epoch,
                 'enabled': self.config.get('enabled', True) and self.identity is not None,
                 'locked': self.screen_locked, 'focus': self.focus}
        if kind == 'hello':
            value['version'] = 2
        return value

    def _change(self):
        self.epoch += 1
        self._emit(self._state())

    def on_focus(self, token):
        with self.lock:
            self.focus = str(token)[:256]
            self._change()

    def on_lock(self, active):
        with self.lock:
            self.screen_locked = bool(active)
            self._change()

    def set_enabled(self, value):
        # Serialize the whole in-memory update + atomic file replacement with
        # heartbeat reloads. No stale read can overwrite a completed control.
        with self.config_lock:
            with self.lock:
                self.config['enabled'] = bool(value)
                self._change()  # invalidate pending inspection before disk I/O
                saved = dict(self.config)
            try:
                CONFIG.parent.mkdir(parents=True, exist_ok=True)
                temp = CONFIG.with_name(CONFIG.name + '.tmp-' + str(os.getpid()))
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(saved, output, indent=2)
                    output.write('\n')
                temp.replace(CONFIG)
            except OSError:
                log.warning('Could not save autoscroll enabled setting')
        if self.app:
            notification = Gio.Notification.new(PRODUCT_LABEL + ' ' + ('on' if value else 'paused'))
            notification.set_body('Ctrl+Alt+M toggles scrolling. Middle-click paste remains blocked.')
            self.app.send_notification('midscroll-state', notification)
        return bool(value)

    def toggle(self):
        # Keep read-modify-write atomic across concurrent toggles, but never
        # enter a configuration operation while retaining the state lock.
        with self.config_lock:
            with self.lock:
                value = not self.config.get('enabled', True)
            return self.set_enabled(value)

    def cancelled(self, epoch, connection):
        with self.lock:
            return (self.stop.is_set() or not self.connected or connection != self.connection
                    or epoch != self.epoch or self.screen_locked or not self.config.get('enabled', True))

    def submit(self, request):
        if not query_valid(request):
            return
        with self.lock:
            token = (self.connection, request['id'])
            if request['epoch'] != self.epoch:
                return
            if self.busy is not None or self.cancelled(request['epoch'], self.connection):
                self._emit({'type': 'decision', 'id': request['id'], 'epoch': self.epoch,
                            'mode': 'ignore', 'x': 0, 'y': 0, 'focus': self.focus})
                return
            self.busy = token
            config = dict(self.config)
        thread = threading.Thread(target=self.answer, args=(request, token, config, time.monotonic() + QUERY_BUDGET), daemon=True)
        thread.start()

    def answer(self, request, token, config, deadline):
        context = {}
        mode, reason = 'ignore', 'inspection-failed'
        cancelled = lambda: self.cancelled(request['epoch'], token[0])
        try:
            context = self.kwin.query(deadline=min(deadline - .025, time.monotonic() + .045))
            if cancelled():
                return
            if self.identity_reader.current() != self.identity:
                mode, reason = 'ignore', 'session-changed'
            elif not context or context.get('active', '') != self.focus:
                mode, reason = 'ignore', 'focus-not-synchronized'
            else:
                simple = basic_policy(context, config)
                if simple:
                    mode, reason = simple
                else:
                    mode, reason = self.inspector.classify(context, config, deadline - .030, cancelled)
                if cancelled():
                    return
                after = self.kwin.query(deadline=deadline - .003)
                if not same_target(context, after):
                    mode, reason = 'ignore', 'target-changed-during-inspection'
                if mode == 'native' and not request['allow_native']:
                    mode, reason = 'ignore', 'native-action-vetoed-by-motion'
        except Exception as exc:
            mode, reason = 'ignore', 'helper-' + type(exc).__name__
        finally:
            with self.lock:
                if self.busy == token:
                    self.busy = None
                # Epoch comparison AND queue insertion are atomic with controls.
                if not cancelled() and time.monotonic() <= deadline:
                    self.last = {'app': context.get('app', ''), 'mode': mode, 'reason': reason}
                    self.stats[reason] += 1
                    self._emit({'type': 'decision', 'id': request['id'], 'epoch': request['epoch'],
                                'mode': mode, 'x': context.get('x', 0), 'y': context.get('y', 0),
                                'focus': self.focus})
            if not self.inspector.ready and not self.stop.is_set():
                self.inspector.prewarm()

    def line(self, message):
        if self.overlay is None:
            return False
        kind = message.get('type')
        if kind == 'start':
            x, y = message.get('x'), message.get('y')
            if not all(type(v) in (int, float) and math.isfinite(v) and abs(v) < 100000 for v in (x, y)):
                return False
            self.overlay.start(x, y, message.get('ghost', True) is True)
        elif kind == 'stop':
            self.overlay.set_active(False)
        elif kind == 'pos':
            dx, dy = message.get('dx'), message.get('dy')
            if all(type(v) in (int, float) and math.isfinite(v) and abs(v) < 100000 for v in (dx, dy)):
                self.overlay.set_offset(dx, dy)
        elif kind == 'status':
            with self.lock:
                if not self.backend_status:
                    log.info('Daemon protocol v2 connected for session %s', self.identity)
                self.backend_status = message
                self.connection_error = ''
        return False

    def refresh_state(self):
        identity = self.identity_reader.current()
        with self.config_lock:
            if self.stop.is_set():
                return
            updated = read_config()
            with self.lock:
                changed = identity != self.identity or updated != self.config
                self.identity = identity
                self.config = updated
                if changed:
                    self._change()
                else:
                    self._emit(self._state())
                if identity is None:
                    self.disconnect.set()

    def heartbeat(self):
        while not self.stop.wait(1):
            self.refresh_state()

    def connection_problem(self, message):
        now = time.monotonic()
        with self.lock:
            changed = self.connection_error != message
            self.connection_error = message
            if changed or now - self.connection_log_time >= 30:
                self.connection_log_time = now
                log.warning('Daemon connection unavailable: %s', message)

    def watch(self):
        while not self.stop.is_set():
            try:
                identity = self.identity_reader.current()
                if identity is None:
                    raise OSError('no active local graphical session for uid ' + str(os.getuid()) + ' on seat0')
                if not trustworthy_socket(SOCKET):
                    raise OSError('socket or parent ownership/permissions unavailable at ' + SOCKET)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(.2)
                    client.connect(SOCKET)
                    _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != 0:
                        raise OSError('daemon is not root')
                    client.setblocking(False)
                    self.disconnect.clear()
                    with self.lock:
                        self.identity = identity
                        self.connected = True
                        self.backend_status = {}
                        self.connection += 1
                        connection = self.connection
                        self.epoch += 1
                        self._emit(self._state('hello'))
                    incoming, outgoing = b'', b''
                    while not self.stop.is_set() and not self.disconnect.is_set():
                        while len(outgoing) < 32768:
                            try:
                                generation, message = self.outbox.get_nowait()
                            except queue.Empty:
                                break
                            if generation != connection:
                                continue
                            frame = json.dumps(message, separators=(',', ':'), allow_nan=False).encode() + b'\n'
                            if len(frame) > MAX_FRAME:
                                raise OSError('protocol frame too large')
                            outgoing += frame
                        readable, writable, _ = select.select([client], [client] if outgoing else [], [], .02)
                        if writable:
                            outgoing = outgoing[client.send(outgoing):]
                        if readable:
                            chunk = client.recv(MAX_FRAME)
                            if not chunk:
                                raise OSError('daemon closed the connection')
                            incoming += chunk
                            while b'\n' in incoming:
                                frame, incoming = incoming.split(b'\n', 1)
                                if len(frame) >= MAX_FRAME:
                                    raise OSError('protocol frame too large')
                                message = json.loads(frame)
                                if not isinstance(message, dict):
                                    raise OSError('invalid protocol object')
                                if message.get('type') == 'query':
                                    self.submit(message)
                                elif self.overlay:
                                    GLib.idle_add(self.line, message)
                            if len(incoming) >= MAX_FRAME:
                                raise OSError('unterminated protocol frame')
            except (OSError, ValueError, TypeError) as exc:
                self.connection_problem(type(exc).__name__ + ': ' + str(exc))
            finally:
                with self.lock:
                    self.connected = False
                    self.connection += 1
                    self.epoch += 1
                if self.overlay:
                    GLib.idle_add(self.overlay.set_active, False)
            self.stop.wait(.4)

    def start(self):
        try:
            self.kwin.bus.call_blocking('org.a11y.Bus', '/org/a11y/bus', 'org.freedesktop.DBus.Properties', 'Set', 'ssv', ('org.a11y.Status', 'IsEnabled', dbus.Boolean(True)), timeout=.3)
        except dbus.DBusException:
            log.warning('Could not enable accessibility inspection')
        self.kwin.install_hooks()
        self.overlay = create_overlay(self.app)
        if not self.overlay.available and self.app:
            notification = Gio.Notification.new(PRODUCT_LABEL)
            notification.set_body('The visual indicator is unavailable. Scrolling and paste blocking do not require it.')
            self.app.send_notification('windows-scroll-linux-indicator', notification)
        self.inspector.prewarm()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        threading.Thread(target=self.watch, daemon=True).start()

    def close(self):
        with self.config_lock:
            with self.lock:
                self.config['enabled'] = False
                self._change()
                self.stop.set()
        self.inspector.close()
        self.kwin.close()


class Control(dbus.service.Object):
    def __init__(self, session):
        self.session = session
        self.name = dbus.service.BusName('org.midscroll.Control', session.kwin.bus)
        super().__init__(self.name, '/org/midscroll/Control')

    @dbus.service.method('org.midscroll.Control', in_signature='b', out_signature='b')
    def SetEnabled(self, enabled):
        return self.session.set_enabled(bool(enabled))

    @dbus.service.method('org.midscroll.Control', in_signature='', out_signature='b')
    def Toggle(self):
        return self.session.toggle()

    @dbus.service.method('org.midscroll.Control', in_signature='', out_signature='s')
    def Status(self):
        with self.session.lock:
            return json.dumps({'product': PRODUCT_NAME, 'version': PUBLIC_VERSION,
                               'indicator': indicator_status(self.session.overlay),
                               'enabled': self.session.config.get('enabled', True), 'connected': self.session.connected,
                               'session': self.session.identity, 'locked': self.session.screen_locked,
                               'epoch': self.session.epoch, 'protocol': 2, 'backend': 'rust',
                               'last': self.session.last, 'counts': dict(self.session.stats),
                               'daemon': self.session.backend_status, 'connection_error': self.session.connection_error})


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    GLib.set_application_name(PRODUCT_LABEL)
    app = Gtk.Application(application_id='org.midscroll.overlay', flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder = []
    def activate(app):
        session = Session(app)
        holder.append(session)
        session.start()
    app.connect('activate', activate)
    app.hold()
    add_unix_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (app.quit(), False)[1])
    add_unix_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (app.quit(), False)[1])
    try:
        app.run([])
    finally:
        for session in holder:
            session.close()

if __name__ == '__main__':
    main()
