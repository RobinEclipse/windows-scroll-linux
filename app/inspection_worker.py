#!/usr/bin/python3
"""AT-SPI subprocess; the session helper can kill even a blocked C API call."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_policy import Inspector

MAX_FRAME = 16384

def main():
    inspector = Inspector()
    inspector.warm(time.monotonic() + .3)
    print('{"ready":true}', flush=True)
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line:
            return
        if len(line) > MAX_FRAME or not line.endswith(b'\n'):
            return
        try:
            request = json.loads(line)
            deadline = float(request['deadline'])
            if deadline <= time.monotonic():
                result = ('scroll', 'inspection-timeout')
            else:
                result = inspector.classify(request['context'], request.get('config'), deadline)
            print(json.dumps({'id': request['id'], 'mode': result[0], 'reason': result[1]}), flush=True)
        except Exception:
            return

if __name__ == '__main__':
    main()
