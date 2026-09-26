"""KDE pointer/window snapshots, bounded by one absolute request deadline."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop

DBusGMainLoop(set_as_default=True)
INTERFACE = 'org.midscroll.Context'
OBJECT = '/org/midscroll/Context'


def valid_context(value):
    if not isinstance(value, dict):
        return {}
    for key in ('x', 'y'):
        if not isinstance(value.get(key), (int, float)) or not math.isfinite(value[key]) or abs(value[key]) > 100000:
            return {}
    for key in ('active', 'id', 'app', 'instance', 'caption'):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > (4096 if key == 'caption' else 256)):
            return {}
    if value.get('found'):
        if not isinstance(value.get('pid'), int) or value['pid'] <= 0:
            return {}
        for key in ('client', 'frame'):
            rect = value.get(key)
            if not isinstance(rect, list) or len(rect) != 4 or any(not isinstance(n, (int, float)) or not math.isfinite(n) or abs(n) > 100000 for n in rect):
                return {}
    return value


def same_target(before, after):
    keys = ('x', 'y', 'active', 'found', 'id', 'pid', 'app', 'instance', 'caption', 'frame', 'client', 'fullscreen', 'desktop', 'dock', 'popup', 'window_matches')
    return bool(before and after and before.get('found') and after.get('found')
                and all(before.get(key) == after.get(key) for key in keys))


class KWinContext(dbus.service.Object):
    def __init__(self, on_focus=lambda token: None, on_pause=lambda: None):
        self.bus = dbus.SessionBus()
        self.owner = str(self.bus.get_name_owner('org.kde.KWin'))
        super().__init__(self.bus, OBJECT)
        self.on_focus, self.on_pause = on_focus, on_pause
        self.lock = threading.Lock()
        self.pending = {}
        self.sequence = 0
        self.dir = Path(tempfile.mkdtemp(prefix='midscroll-v2-kwin-', dir=os.environ.get('XDG_RUNTIME_DIR')))
        self.prefix = 'midscroll-v2-' + str(os.getpid())
        self.persistent = None
        self.bus.add_signal_receiver(self.owner_changed, signal_name='NameOwnerChanged', dbus_interface='org.freedesktop.DBus', arg0='org.kde.KWin')

    def owner_changed(self, name, old, new):
        self.owner = str(new)
        self.on_focus('')
        if new:
            self.install_hooks()

    @dbus.service.method(INTERFACE, in_signature='is', out_signature='', sender_keyword='sender')
    def Result(self, sequence, payload, sender=None):
        if sender != self.owner:
            return
        with self.lock:
            item = self.pending.get(int(sequence))
        if item:
            try:
                data = valid_context(json.loads(str(payload)))
            except (ValueError, TypeError):
                data = {}
            item[1].update(data)
            item[0].set()

    @dbus.service.method(INTERFACE, in_signature='s', out_signature='', sender_keyword='sender')
    def Focus(self, token, sender=None):
        if sender == self.owner:
            self.on_focus(str(token)[:256])

    @dbus.service.method(INTERFACE, in_signature='', out_signature='', sender_keyword='sender')
    def Toggle(self, sender=None):
        if sender == self.owner:
            self.on_pause()

    def _callback(self, method, args=''):
        return 'callDBus(%s,%s,%s,%s%s);' % (json.dumps(str(self.bus.get_unique_name())), json.dumps(OBJECT), json.dumps(INTERFACE), json.dumps(method), ', ' + args if args else '')

    def _unload(self, name):
        try:
            self.bus.call_async('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', 's', (name,), lambda *a: None, lambda *a: None, timeout=.2)
        except dbus.DBusException:
            pass

    def install_hooks(self):
        source = 'function report(w) { ' + self._callback('Focus', 'w ? String(w.internalId) : ""') + ' }\n'
        source += 'workspace.windowActivated.connect(report); report(workspace.activeWindow);\n'
        source += 'registerShortcut("midscroll-toggle", "Toggle Windows Scroll Linux", "Ctrl+Alt+M", function() { ' + self._callback('Toggle') + ' });\n'
        name = 'midscroll-v2-hooks'
        self._unload(name)
        path = self.dir / 'hooks.js'
        path.write_text(source)
        sid = self.bus.call_blocking('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'loadScript', 'ss', (str(path), name), timeout=.3)
        self.bus.call_async('org.kde.KWin', '/Scripting/Script' + str(sid), 'org.kde.kwin.Script', 'run', '', (), lambda *a: None, lambda *a: None, timeout=.3)
        self.persistent = name

    def query(self, deadline=None, window_id=None):
        deadline = time.monotonic() + .045 if deadline is None else deadline
        if deadline <= time.monotonic():
            return {}
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            ready, data = threading.Event(), {}
            self.pending[sequence] = (ready, data)
        name = self.prefix + '-' + str(sequence)
        path = self.dir / ('query-' + str(sequence) + '.js')
        source = '''
function rect(r) { return r ? [r.x,r.y,r.width,r.height] : [0,0,0,0]; }
var p = workspace.cursorPos;
var windows = workspace.windowAt(p,16);
var w = windows.find(function(win) { return String(win.resourceClass) !== "org.midscroll.overlay"; });
var a = workspace.activeWindow;
var data = {x:p.x,y:p.y,active:a ? String(a.internalId) : "",found:!!w};
if(w) {
 data.id=String(w.internalId); data.app=String(w.resourceClass);
 data.instance=String(w.resourceName); data.pid=w.pid; data.caption=w.caption;
 data.frame=rect(w.frameGeometry); data.client=rect(w.clientGeometry);
 data.window_matches=workspace.windowList().filter(function(win) { return win.pid === w.pid && win.caption === w.caption; }).length;
 data.fullscreen=w.fullScreen; data.desktop=w.desktopWindow; data.dock=w.dock; data.popup=w.popupWindow;
}
'''
        if window_id is not None:
            source = source.replace('var w = windows.find(function(win) { return String(win.resourceClass) !== "org.midscroll.overlay"; });', 'var w = workspace.windowList().find(function(win) { return String(win.internalId) === ' + json.dumps(window_id) + '; });')
        source += self._callback('Result', str(sequence) + ',JSON.stringify(data)')
        try:
            path.write_text(source)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            sid = self.bus.call_blocking('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'loadScript', 'ss', (str(path), name), timeout=remaining)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {}
            self.bus.call_async('org.kde.KWin', '/Scripting/Script' + str(sid), 'org.kde.kwin.Script', 'run', '', (), lambda *a: None, lambda *a: ready.set(), timeout=remaining)
            if not ready.wait(max(0, deadline - time.monotonic())):
                return {}
            return dict(data)
        except (OSError, dbus.DBusException):
            return {}
        finally:
            with self.lock:
                self.pending.pop(sequence, None)
            self._unload(name)
            path.unlink(missing_ok=True)

    def close(self):
        if self.persistent:
            self._unload(self.persistent)
        for path in self.dir.glob('*.js'):
            path.unlink(missing_ok=True)
        try:
            self.dir.rmdir()
        except OSError:
            pass
