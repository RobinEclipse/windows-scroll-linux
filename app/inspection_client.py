"""Persistent, disposable AT-SPI worker with an absolute wall-clock deadline."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

class InspectionClient:
    def __init__(self, command=None):
        self.command = command or [sys.executable, '-I', str(Path(__file__).with_name('inspection_worker.py'))]
        self.lock = threading.Lock()
        self.process = None
        self.buffer = b''
        self.ready = False
        self.sequence = 0
        self.closed = False
        self.warm_lock = threading.Lock()
        self.warm_thread = None

    def _discard(self):
        process, self.process = self.process, None
        self.ready = False
        self.buffer = b''
        if process:
            process.kill() if process.poll() is None else None
            # SIGKILL is already issued; reap off the interaction path.
            def reap():
                process.wait()
                process.stdin.close()
                process.stdout.close()
            threading.Thread(target=reap, daemon=True).start()

    def _start(self):
        if self.process is None and not self.closed:
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stdin.fileno(), False)

    def _read(self, deadline, cancelled):
        while time.monotonic() < deadline and not cancelled():
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                return json.loads(line)
            if not self.process or self.process.poll() is not None:
                break
            ready, _, _ = select.select([self.process.stdout], [], [], min(.01, max(0, deadline - time.monotonic())))
            if ready:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                self.buffer += chunk
                if len(self.buffer) > 4096:
                    break
        return None

    def prewarm(self):
        """At most one warm-up thread, including when requests time out rapidly."""
        with self.warm_lock:
            if self.closed or (self.warm_thread and self.warm_thread.is_alive()):
                return
            self.warm_thread = threading.Thread(target=self.warm, daemon=True)
            self.warm_thread.start()

    def warm(self):
        with self.lock:
            try:
                self._start()
                if self.process and not self.ready:
                    self.ready = self._read(time.monotonic() + 1.5, lambda: self.closed) == {'ready': True}
                if not self.ready:
                    self._discard()
            except (OSError, ValueError):
                self._discard()

    def classify(self, context, config, deadline, cancelled=lambda: False):
        # A background warm-up must not make a query wait past its deadline.
        if not self.lock.acquire(timeout=max(0, deadline - time.monotonic())):
            return 'scroll', 'inspection-worker-starting'
        try:
            if cancelled() or self.closed:
                return 'ignore', 'inspection-cancelled'
            self._start()
            if not self.ready:
                self.ready = self._read(deadline, cancelled) == {'ready': True}
            if not self.ready:
                self._discard()
                return 'scroll', 'inspection-worker-timeout'
            self.sequence += 1
            request = json.dumps({'id': self.sequence, 'deadline': deadline, 'context': context, 'config': config}, separators=(',', ':')).encode() + b'\n'
            if len(request) > 16384:
                return 'scroll', 'inspection-request-too-large'
            if os.write(self.process.stdin.fileno(), request) != len(request):
                raise OSError('partial worker request')
            reply = self._read(deadline, cancelled)
            if cancelled():
                self._discard()
                return 'ignore', 'inspection-cancelled'
            if not reply or reply.get('id') != self.sequence or reply.get('mode') not in ('native', 'scroll', 'ignore'):
                self._discard()
                return 'scroll', 'inspection-worker-timeout'
            return reply['mode'], str(reply.get('reason', 'inspection-result'))[:100]
        except (OSError, ValueError, TypeError):
            self._discard()
            return 'scroll', 'inspection-worker-failed'
        finally:
            self.lock.release()

    def close(self):
        self.closed = True
        with self.lock:
            self._discard()
