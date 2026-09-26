import sys
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from inspection_client import InspectionClient

class Worker(unittest.TestCase):
    def client(self, source):
        client = InspectionClient([sys.executable, '-u', '-c', source])
        self.addCleanup(client.close)
        client.warm()
        return client
    def test_hung_application_is_killed_within_whole_deadline(self):
        client = self.client('import sys,time; print(\'{"ready":true}\'); sys.stdin.readline(); time.sleep(60)')
        process = client.process
        start = time.monotonic()
        result = client.classify({}, {}, start + .055)
        self.assertEqual(result, ('scroll', 'inspection-worker-timeout'))
        self.assertLess(time.monotonic() - start, .15)
        process.wait(timeout=.5)
        self.assertNotEqual(process.returncode, 0)
    def test_worker_is_reused_for_successful_queries(self):
        client = self.client('import sys,json; print(\'{"ready":true}\');\nfor line in sys.stdin:\n r=json.loads(line); print(json.dumps({"id":r["id"],"mode":"native","reason":"link"}))')
        process = client.process
        for _ in range(2):
            self.assertEqual(client.classify({}, {}, time.monotonic() + .2), ('native', 'link'))
            self.assertIs(client.process, process)
    def test_cancellation_kills_busy_worker(self):
        client = self.client('import sys,time; print(\'{"ready":true}\'); sys.stdin.readline(); time.sleep(60)')
        cancelled = threading.Event()
        timer = threading.Timer(.02, cancelled.set)
        timer.start()
        start = time.monotonic()
        self.assertEqual(client.classify({}, {}, start + .5, cancelled.is_set)[0], 'ignore')
        self.assertLess(time.monotonic() - start, .15)
        timer.join()
    def test_oversize_worker_response_is_discarded(self):
        client = self.client('import sys; print(\'{"ready":true}\'); sys.stdin.readline(); print("x"*8192)')
        self.assertEqual(client.classify({}, {}, time.monotonic() + .2)[0], 'scroll')
        self.assertIsNone(client.process)
    def test_obsolete_sequence_is_discarded(self):
        client = self.client('import sys; print(\'{"ready":true}\'); sys.stdin.readline(); print(\'{"id":999,"mode":"native"}\')')
        self.assertEqual(client.classify({}, {}, time.monotonic() + .2)[0], 'scroll')
        self.assertIsNone(client.process)
    def test_repeated_prewarm_uses_one_thread(self):
        client = InspectionClient([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])
        self.addCleanup(client.close)
        client.prewarm()
        thread = client.warm_thread
        for _ in range(10): client.prewarm()
        self.assertIs(client.warm_thread, thread)
        client.closed = True
        thread.join(timeout=.3)
        self.assertFalse(thread.is_alive())
    def test_warmup_lock_does_not_bypass_deadline(self):
        client = InspectionClient([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])
        self.addCleanup(client.close)
        warm = threading.Thread(target=client.warm)
        warm.start()
        time.sleep(.02)
        start = time.monotonic()
        result = client.classify({}, {}, start + .04)
        self.assertEqual(result[1], 'inspection-worker-starting')
        self.assertLess(time.monotonic() - start, .1)
        client.closed = True
        warm.join(timeout=.3)
        self.assertFalse(warm.is_alive())

if __name__ == '__main__': unittest.main()
