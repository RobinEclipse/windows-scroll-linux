from collections import Counter
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import context_session as module
from context_session import Session, query_valid
from kwin_context import same_target, valid_context

CONTEXT = {'found': True, 'x': 100, 'y': 100, 'pid': 123, 'id': 'window', 'active': 'window',
           'app': 'testapp', 'instance': 'testapp', 'caption': 'Document', 'window_matches': 1,
           'client': [0, 0, 800, 600], 'frame': [0, 0, 800, 600]}

class SessionTests(unittest.TestCase):
    def session(self):
        session = Session.__new__(Session)
        session.app = None
        session.lock = threading.RLock()
        session.config_lock = threading.RLock()
        session.stop = threading.Event()
        session.disconnect = threading.Event()
        session.outbox = queue.Queue(maxsize=64)
        session.epoch = 7
        session.connection = 1
        session.connected = True
        session.focus = 'window'
        session.screen_locked = False
        session.config = {'enabled': True, 'app_modes': {}}
        session.identity = ('1', 'seat0')
        session.identity_reader = SimpleNamespace(current=lambda: ('1', 'seat0'))
        session.last = {}
        session.stats = Counter()
        session.busy = (1, 9)
        session.kwin = SimpleNamespace(query=lambda **kw: dict(CONTEXT))
        session.inspector = SimpleNamespace(classify=lambda *args: ('native', 'link'), ready=True)
        return session
    def request(self, allow=True):
        return {'type': 'query', 'id': 9, 'epoch': 7, 'allow_native': allow}
    def messages(self, session):
        result = []
        while not session.outbox.empty(): result.append(session.outbox.get_nowait()[1])
        return result
    def answer(self, session, allow=True):
        session.answer(self.request(allow), (1, 9), session.config, time.monotonic() + .16)
        return self.messages(session)
    def test_stable_native_target_passes(self):
        self.assertEqual(self.answer(self.session())[0]['mode'], 'native')
    def test_daemon_motion_veto_suppresses_native_action(self):
        self.assertEqual(self.answer(self.session(), allow=False)[0]['mode'], 'ignore')
    def test_moving_pointer_recheck_suppresses_native_action(self):
        session = self.session()
        contexts = iter([dict(CONTEXT), dict(CONTEXT, x=101)])
        session.kwin.query = lambda **kw: next(contexts)
        self.assertEqual(self.answer(session)[0]['mode'], 'ignore')
    def test_resizing_window_recheck_suppresses_native_action(self):
        session = self.session()
        contexts = iter([dict(CONTEXT), dict(CONTEXT, client=[0, 0, 801, 600])])
        session.kwin.query = lambda **kw: next(contexts)
        self.assertEqual(self.answer(session)[0]['mode'], 'ignore')
    def test_focus_not_matching_hook_is_suppressed(self):
        session = self.session()
        session.focus = 'other-window'
        self.assertEqual(self.answer(session)[0]['mode'], 'ignore')
    def test_session_change_is_suppressed(self):
        session = self.session()
        session.identity_reader.current = lambda: ('2', 'seat0')
        self.assertEqual(self.answer(session)[0]['mode'], 'ignore')
    def race(self, change):
        session = self.session()
        entered, release = threading.Event(), threading.Event()
        def classify(*args):
            entered.set()
            release.wait(.5)
            return 'native', 'link'
        session.inspector.classify = classify
        task = threading.Thread(target=session.answer, args=(self.request(), (1, 9), dict(session.config), time.monotonic() + .16))
        task.start()
        self.assertTrue(entered.wait(.1))
        change(session)
        release.set()
        task.join(.2)
        self.assertFalse(task.is_alive())
        messages = self.messages(session)
        self.assertFalse(any(message['type'] == 'decision' for message in messages))
        self.assertEqual(session.epoch, 8)
    def test_pause_invalidates_inflight_native_reply(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(module, 'CONFIG', Path(temp) / 'settings.json'):
            self.race(lambda session: session.set_enabled(False))
    def test_focus_change_invalidates_inflight_reply(self):
        self.race(lambda session: session.on_focus('other'))
    def test_lock_invalidates_inflight_reply(self):
        self.race(lambda session: session.on_lock(True))
    def test_repeated_pause_still_advances_epoch(self):
        session = self.session()
        with tempfile.TemporaryDirectory() as temp, patch.object(module, 'CONFIG', Path(temp) / 'settings.json'):
            session.set_enabled(False)
            session.set_enabled(False)
        self.assertEqual(session.epoch, 9)
    def test_expired_result_is_never_enqueued(self):
        session = self.session()
        session.answer(self.request(), (1, 9), session.config, time.monotonic() - 1)
        self.assertEqual(self.messages(session), [])
    def test_stale_query_is_not_run(self):
        session = self.session()
        session.submit(dict(self.request(), epoch=6))
        self.assertEqual(self.messages(session), [])
    def test_second_simultaneous_mouse_does_not_queue_work(self):
        session = self.session()
        session.submit(dict(self.request(), id=10))
        reply = self.messages(session)[0]
        self.assertEqual((reply['id'], reply['mode']), (10, 'ignore'))
    def test_congested_socket_never_blocks_pause(self):
        session = self.session()
        for _ in range(64): session._emit({'type': 'state'})
        start = time.monotonic()
        session.on_focus('new')
        self.assertLess(time.monotonic() - start, .01)
        self.assertTrue(session.disconnect.is_set())
    def test_protocol_rejects_bool_id_and_missing_veto(self):
        self.assertTrue(query_valid(self.request()))
        self.assertFalse(query_valid(dict(self.request(), id=True)))
        self.assertFalse(query_valid({'type': 'query', 'id': 9, 'epoch': 7}))
        self.assertFalse(query_valid(dict(self.request(), id=2**64)))

class ContextTests(unittest.TestCase):
    def test_same_target_requires_exact_pointer(self):
        self.assertTrue(same_target(CONTEXT, CONTEXT))
        self.assertFalse(same_target(CONTEXT, dict(CONTEXT, x=100.01)))
    def test_nonfinite_coordinates_are_rejected(self):
        self.assertEqual(valid_context(dict(CONTEXT, x=float('nan'))), {})
    def test_malformed_geometry_is_rejected(self):
        self.assertEqual(valid_context(dict(CONTEXT, client=[1, 2, 3])), {})
    def test_title_changes_invalidate_target(self):
        self.assertFalse(same_target(CONTEXT, dict(CONTEXT, caption='New Document')))

if __name__ == '__main__': unittest.main()
