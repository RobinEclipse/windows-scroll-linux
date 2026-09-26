"""Temporary KWin scripting bridge for owned-window integration checks."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default=True)

class KWinBridge(dbus.service.Object):
    def __init__(self, directory):
        self.bus = dbus.SessionBus()
        self.owner = str(self.bus.get_name_owner('org.kde.KWin'))
        self.object_path = '/org/midscroll/Integration'
        self.interface = 'org.midscroll.Integration'
        super().__init__(self.bus, self.object_path)
        self.directory = Path(directory)
        self.sequence = 0
        self.pending = {}
        self.lock = threading.Lock()
    @dbus.service.method('org.midscroll.Integration', in_signature='is', out_signature='', sender_keyword='sender')
    def Result(self, sequence, payload, sender=None):
        if sender != self.owner:
            return
        with self.lock:
            item = self.pending.get(int(sequence))
        if item:
            item[1].update(json.loads(str(payload)))
            item[0].set()
    def run(self, source):
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            event, result = threading.Event(), {}
            self.pending[sequence] = (event, result)
        name = 'autoscroll-v2-integration-' + str(os.getpid()) + '-' + str(sequence)
        path = self.directory / (name + '.js')
        source += '\ncallDBus(%s,%s,%s,"Result",%d,JSON.stringify(result));' % (
            json.dumps(str(self.bus.get_unique_name())), json.dumps(self.object_path), json.dumps(self.interface), sequence)
        try:
            path.write_text(source)
            sid = self.bus.call_blocking('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'loadScript', 'ss', (str(path), name), timeout=.5)
            self.bus.call_async('org.kde.KWin', '/Scripting/Script' + str(sid), 'org.kde.kwin.Script', 'run', '', (), lambda *a: None, lambda *a: event.set(), timeout=.5)
            if not event.wait(.5):
                raise TimeoutError('KWin integration query did not answer')
            return result
        finally:
            with self.lock:
                self.pending.pop(sequence, None)
            self.bus.call_async('org.kde.KWin', '/Scripting', 'org.kde.kwin.Scripting', 'unloadScript', 's', (name,), lambda *a: None, lambda *a: None, timeout=.5)
            path.unlink(missing_ok=True)
    def retry(self, source):
        # Snapshot and window activation are idempotent; retry temporary KWin
        # callback failures without repeating mouse input or installing hooks.
        for attempt in range(3):
            try:
                return self.run(source)
            except (TimeoutError, OSError, dbus.DBusException):
                if attempt == 2:
                    raise
                time.sleep(.05 * (attempt + 1))

    def snapshot(self, pid=None):
        source = '''
var p=workspace.cursorPos, a=workspace.activeWindow;
var h=workspace.windowAt(p,16).find(function(w){ return String(w.resourceClass)!=="org.midscroll.overlay"; });
var result={x:p.x,y:p.y,active:a?String(a.internalId):"",hover:h?String(h.internalId):""};
'''
        if pid is not None:
            source += '''
var matches=workspace.windowList().filter(function(w){return w.pid===%d;});
if(matches.length===1){var w=matches[0],r=w.clientGeometry;result.window={id:String(w.internalId),pid:w.pid,client:[r.x,r.y,r.width,r.height]};}
''' % int(pid)
        return self.retry(source)
    def activate(self, window):
        return self.retry('var w=workspace.windowList().find(function(w){return String(w.internalId)===' + json.dumps(window) + ';}); if(w){workspace.activeWindow=w;} var result={found:!!w};')
