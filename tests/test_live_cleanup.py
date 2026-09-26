"""Cleanup regressions require no graphical windows, root, or injected input."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

INTEGRATION = Path(__file__).resolve().parents[1] / 'integration'
sys.path.insert(0, str(INTEGRATION))
spec = importlib.util.spec_from_file_location('live_runner', INTEGRATION / 'run.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from kwin_bridge import KWinBridge

class Cleanup(unittest.TestCase):
    def runner(self):
        runner = module.Runner.__new__(module.Runner)
        runner.results = {'status': 'pass'}
        runner.saved_enabled = True
        runner.saved_pointer = (10, 20)
        runner.saved_focus = 'original-window'
        runner.connection = SimpleNamespace(close=Mock())
        runner.stream = None
        runner.fixture_processes = {}
        runner.injector = None
        runner.request = Mock(return_value={'ok': True})
        runner.move = Mock()
        state = {'enabled': True}
        runner.enabled = Mock(side_effect=lambda value: state.update(enabled=value))
        runner.control = lambda: {'enabled': state['enabled'], 'connected': True}
        runner.bridge = SimpleNamespace(activate=Mock(return_value={'found': True}),
                                       snapshot=Mock(return_value={'active': 'original-window', 'x': 10, 'y': 20}))
        return runner
    @patch.object(module.time, 'sleep', lambda _: None)
    def test_focus_timeout_cannot_skip_restoring_enabled_setting(self):
        runner = self.runner()
        runner.bridge.activate.side_effect = TimeoutError('KWin did not answer')
        runner.cleanup()
        self.assertEqual(runner.enabled.call_args_list[-1].args, (True,))
        self.assertTrue(runner.results['restored_desktop']['enabled'])
        self.assertFalse(runner.results['restored'])
        self.assertTrue(any('restore-focus' in error for error in runner.results['cleanup_errors']))
    @patch.object(module.time, 'sleep', lambda _: None)
    def test_release_failure_on_one_mouse_does_not_skip_other_or_restore(self):
        runner = self.runner()
        def request(operation, mouse=0):
            if operation == 'release_all' and mouse == 0:
                raise OSError('first mouse disappeared')
            return {'ok': True}
        runner.request.side_effect = request
        runner.cleanup()
        self.assertTrue(any(call.args == ('release_all',) and call.kwargs == {'mouse': 1} for call in runner.request.call_args_list))
        runner.move.assert_called_once_with(10, 20)
        self.assertEqual(runner.enabled.call_args_list[-1].args, (True,))
    @patch.object(module.time, 'sleep', lambda _: None)
    def test_success_records_verified_final_snapshot(self):
        runner = self.runner()
        runner.cleanup()
        self.assertTrue(runner.results['restored'])
        self.assertEqual(runner.results['restored_desktop'], {'active': 'original-window', 'x': 10, 'y': 20, 'enabled': True, 'helper_connected': True})
    @patch.object(module.time, 'sleep', lambda _: None)
    def test_final_enabled_mismatch_is_reported(self):
        runner = self.runner()
        runner.control = lambda: {'enabled': False, 'connected': True}
        runner.cleanup()
        self.assertFalse(runner.results['restored'])
        self.assertIn('final enabled setting differs from the saved setting', runner.results['cleanup_errors'])
    @patch.object(module.time, 'sleep', lambda _: None)
    def test_original_paused_setting_is_preserved(self):
        runner = self.runner()
        runner.saved_enabled = False
        runner.cleanup()
        self.assertTrue(runner.results['restored'])
        self.assertEqual(runner.enabled.call_args_list[-1].args, (False,))

class KWinRetries(unittest.TestCase):
    @patch('kwin_bridge.time.sleep', lambda _: None)
    def test_transient_timeout_retries_idempotent_command(self):
        bridge = SimpleNamespace(run=Mock(side_effect=[TimeoutError('slow'), {'found': True}]))
        self.assertEqual(KWinBridge.retry(bridge, 'idempotent command'), {'found': True})
        self.assertEqual(bridge.run.call_count, 2)
    @patch('kwin_bridge.time.sleep', lambda _: None)
    def test_persistent_timeout_is_bounded_to_three_attempts(self):
        bridge = SimpleNamespace(run=Mock(side_effect=TimeoutError('slow')))
        with self.assertRaises(TimeoutError):
            KWinBridge.retry(bridge, 'idempotent command')
        self.assertEqual(bridge.run.call_count, 3)

if __name__ == '__main__': unittest.main()
