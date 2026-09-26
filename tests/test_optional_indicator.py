"""Missing rendering dependencies must not disable mouse handling or controls."""
from collections import Counter
import importlib.util
import json
from pathlib import Path
import queue
import sys
import threading
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import context_session as module

class OptionalIndicator(unittest.TestCase):
    def test_direct_launch_without_preload_skips_native_overlay_import(self):
        with patch.dict(module.os.environ, {'LD_PRELOAD': ''}), \
                patch.object(module.importlib.util, 'spec_from_file_location') as native_import, \
                self.assertLogs(module.log, level='WARNING'):
            overlay = module.create_overlay(None)
        self.assertFalse(overlay.available)
        self.assertIn('preloaded', overlay.reason)
        native_import.assert_not_called()
    def test_system_soname_and_absolute_preload_both_allow_import(self):
        for preloads in ('libgtk4-layer-shell.so.0',
                         '/usr/lib/libother.so:/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so.0'):
            with self.subTest(preloads=preloads), \
                    patch.dict(module.os.environ, {'LD_PRELOAD': preloads}), \
                    patch.object(module.importlib.util, 'spec_from_file_location', side_effect=ImportError('optional import reached')):
                with self.assertRaisesRegex(ImportError, 'optional import reached'):
                    module.load_overlay()
    def load_failure(self, error):
        def loader(): raise error
        with self.assertLogs(module.log, level='WARNING'):
            return module.create_overlay(None, loader)
    def test_missing_layer_shell_typelib_keeps_noop_indicator(self):
        overlay = self.load_failure(ValueError('Namespace Gtk4LayerShell not available'))
        self.assertIsInstance(overlay, module.NullOverlay)
        self.assertFalse(module.indicator_status(overlay)['available'])
        self.assertIn('Gtk4LayerShell', module.indicator_status(overlay)['reason'])
        overlay.start(50, 60)
        overlay.set_offset(10, 20)
        overlay.set_active(False)
    def test_missing_cairo_is_optional(self):
        overlay = self.load_failure(ImportError("No module named 'cairo'"))
        self.assertFalse(overlay.available)
    def test_unsupported_compositor_falls_back_before_visual_surface(self):
        def unavailable(app): raise RuntimeError('Wayland layer-shell protocol unavailable')
        with self.assertLogs(module.log, level='WARNING'):
            overlay = module.create_overlay(None, lambda: SimpleNamespace(Overlay=unavailable))
        self.assertFalse(overlay.available)
    def test_installed_indicator_is_used_without_private_vendor_paths(self):
        instance = SimpleNamespace(available=True, reason='')
        overlay = module.create_overlay(None, lambda: SimpleNamespace(Overlay=lambda app: instance))
        self.assertIs(overlay, instance)
        self.assertEqual(module.indicator_status(overlay), {'available': True, 'reason': ''})
    def session(self):
        session = module.Session.__new__(module.Session)
        session.overlay = module.NullOverlay('Gtk4LayerShell is not installed')
        session.lock = threading.RLock()
        session.config_lock = threading.RLock()
        session.stop = threading.Event()
        session.disconnect = threading.Event()
        session.config = {'enabled': True, 'app_modes': {}}
        session.connected = True
        session.connection = 1
        session.outbox = queue.Queue()
        session.epoch = 3
        session.identity = ('2', 'seat0')
        session.focus = 'window'
        session.screen_locked = False
        session.app = None
        session.last = {}
        session.stats = Counter()
        session.backend_status = {}
        session.connection_error = ''
        return session
    def test_daemon_status_is_processed_without_visual_indicator(self):
        session = self.session()
        session.line({'type': 'start', 'x': 100, 'y': 100, 'ghost': True})
        session.line({'type': 'pos', 'dx': 10, 'dy': 20})
        session.line({'type': 'status', 'healthy': True, 'backend': 'rust'})
        session.line({'type': 'stop'})
        result = json.loads(module.Control.Status(SimpleNamespace(session=session)))
        self.assertTrue(result['connected'])
        self.assertTrue(result['enabled'])
        self.assertTrue(result['daemon']['healthy'])
        self.assertFalse(result['indicator']['available'])
        self.assertEqual(result['product'], 'Windows Scroll Linux')
        self.assertEqual(result['version'], '1')
    def test_missing_overlay_does_not_change_middle_click_query_policy(self):
        session = self.session()
        session.busy = (1, 9)
        session.submit({'type': 'query', 'id': 10, 'epoch': 3, 'allow_native': True})
        decision = session.outbox.get_nowait()[1]
        self.assertEqual(decision['mode'], 'ignore')
    def test_older_gi_namespace_uses_existing_glib_signal_api(self):
        original = module.gi.require_version
        def require(namespace, version):
            if namespace == 'GLibUnix':
                raise ValueError('namespace unavailable on this older GI installation')
            return original(namespace, version)
        source = Path(module.__file__)
        spec = importlib.util.spec_from_file_location('context_session_older_gi', source)
        older = importlib.util.module_from_spec(spec)
        with warnings.catch_warnings(), patch.object(module.gi, 'require_version', require):
            warnings.simplefilter('ignore')
            spec.loader.exec_module(older)
            self.assertIs(older.add_unix_signal, module.GLib.unix_signal_add)

if __name__ == '__main__': unittest.main()
