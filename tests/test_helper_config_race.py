"""Deterministic helper configuration ordering tests; no desktop or input use."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import context_session as module


def session(enabled):
    value = module.Session.__new__(module.Session)
    value.lock = threading.RLock()
    value.config_lock = threading.RLock()
    value.config = {'enabled': enabled, 'app_modes': {}}
    value.connected = False
    value.epoch = 0
    value.identity = ('2', 'seat0')
    value.identity_reader = SimpleNamespace(current=lambda: ('2', 'seat0'))
    value.screen_locked = False
    value.focus = 'window'
    value.app = None
    value.stop = threading.Event()
    value.disconnect = threading.Event()
    value.inspector = SimpleNamespace(close=lambda: None)
    value.kwin = SimpleNamespace(close=lambda: None)
    return value


class ConfigurationOrdering(unittest.TestCase):
    def stale_read(self, enabled):
        value = session(not enabled)
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def read_old_snapshot():
            old = {'enabled': not enabled, 'app_modes': {}}
            entered.set()
            if not release.wait(1):
                raise AssertionError('test did not release stale config read')
            return old
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'CONFIG', Path(directory) / 'settings.json'):
            with patch.object(module, 'read_config', read_old_snapshot):
                reader = threading.Thread(target=value.refresh_state)
                reader.start()
                self.assertTrue(entered.wait(.5))
                def update():
                    value.set_enabled(enabled)
                    completed.set()
                writer = threading.Thread(target=update)
                writer.start()
                # A stale reload must finish before the control can commit.
                # Previously writer completed here and reader overwrote it.
                self.assertFalse(completed.wait(.025))
                release.set()
                reader.join(1)
                writer.join(1)
                self.assertFalse(reader.is_alive())
                self.assertFalse(writer.is_alive())
                self.assertTrue(completed.is_set())
            self.assertEqual(value.config['enabled'], enabled)
            self.assertEqual(json.loads(module.CONFIG.read_text())['enabled'], enabled)
            value.refresh_state()
            self.assertEqual(value.config['enabled'], enabled)
    def test_stale_false_heartbeat_cannot_override_completed_enable(self):
        self.stale_read(True)
    def test_stale_true_heartbeat_cannot_override_completed_pause(self):
        self.stale_read(False)
    def test_reload_waits_for_inflight_atomic_save_without_blocking_focus(self):
        value = session(False)
        saving, finish_save, read_started = threading.Event(), threading.Event(), threading.Event()
        original_open, original_read = module.os.open, module.read_config
        def delayed_open(*args, **kwargs):
            saving.set()
            if not finish_save.wait(1):
                raise AssertionError('test did not release atomic save')
            return original_open(*args, **kwargs)
        def observe_read():
            read_started.set()
            return original_read()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'CONFIG', Path(directory) / 'settings.json'):
            module.CONFIG.write_text(json.dumps(value.config))
            with patch.object(module.os, 'open', delayed_open), patch.object(module, 'read_config', observe_read):
                writer = threading.Thread(target=value.set_enabled, args=(True,))
                writer.start()
                self.assertTrue(saving.wait(.5))
                reader = threading.Thread(target=value.refresh_state)
                reader.start()
                self.assertFalse(read_started.wait(.025))
                focus = threading.Thread(target=value.on_focus, args=('other-window',))
                focus.start()
                focus.join(.2)
                self.assertFalse(focus.is_alive(), 'config I/O must not retain the state lock')
                finish_save.set()
                writer.join(1)
                reader.join(1)
                self.assertFalse(writer.is_alive())
                self.assertFalse(reader.is_alive())
            self.assertTrue(value.config['enabled'])
            self.assertEqual(value.focus, 'other-window')
    def test_concurrent_toggles_remain_atomic_without_lock_inversion(self):
        value = session(False)
        start = threading.Barrier(3)
        results = []
        def toggle():
            start.wait()
            results.append(value.toggle())
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'CONFIG', Path(directory) / 'settings.json'):
            threads = [threading.Thread(target=toggle) for _ in range(2)]
            for thread in threads: thread.start()
            start.wait()
            for thread in threads:
                thread.join(1)
                self.assertFalse(thread.is_alive(), 'toggle deadlocked')
            self.assertEqual(sorted(results), [False, True])
            self.assertFalse(value.config['enabled'])
            self.assertFalse(json.loads(module.CONFIG.read_text())['enabled'])
    def test_refresh_cannot_reenable_after_close(self):
        value = session(True)
        value.close()
        with patch.object(module, 'read_config', side_effect=AssertionError('closed helper must not reload settings')):
            value.refresh_state()
        self.assertTrue(value.stop.is_set())
        self.assertFalse(value.config['enabled'])

if __name__ == '__main__': unittest.main()
