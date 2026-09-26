#!/usr/bin/python3
"""Short-lived UInput mouse source for this test, authenticated to PKEXEC_UID.

Only fixed mouse buttons/relative axes are accepted. No keyboard event injection.
The injector closes both devices on EOF, errors, or a 240-second watchdog.
"""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import time
from evdev import UInput, ecodes as e

BUTTONS = (e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA)
AXES = (e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL, e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES)


def valid_events(events):
    if not isinstance(events, list) or len(events) > 128:
        raise ValueError('invalid event batch')
    for event in events:
        if not isinstance(event, list) or len(event) != 3 or any(type(n) is not int for n in event):
            raise ValueError('invalid event')
        kind, code, value = event
        if kind == e.EV_SYN and code == e.SYN_REPORT and value == 0:
            continue
        if kind == e.EV_KEY and code in BUTTONS and value in (0, 1):
            continue
        if kind == e.EV_REL and code in AXES and abs(value) <= 500:
            continue
        raise ValueError('event outside mouse-only test capability')
    return events


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', type=Path, required=True)
    args = parser.parse_args()
    uid = int(os.environ.get('PKEXEC_UID', '-1'))
    if os.geteuid() != 0 or uid <= 0:
        raise SystemExit('Launch this script through pkexec as the logged-in user.')
    path = args.socket
    parent = path.parent
    runtime = Path('/run/user') / str(uid)
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(directory_fd)
    if (parent.parent != runtime or not parent.name.startswith('autoscroll-v2-integration-')
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077
            or path.name != 'inject.sock' or path.exists()):
        raise SystemExit('Unexpected test socket location or ownership.')
    devices = {}
    held = {}
    def close(index):
        device = devices.pop(index, None)
        if device:
            try:
                for code in held.pop(index, set()):
                    device.write(e.EV_KEY, code, 0)
                device.syn()
            finally:
                device.close()
    def create(index):
        close(index)
        device = UInput({e.EV_KEY: list(BUTTONS), e.EV_REL: list(AXES)},
                        name='Autoscroll v2 disposable mouse ' + str(index),
                        phys='autoscroll-v2-integration-' + str(index))
        devices[index] = device
        held[index] = set()
        return {'name': device.name, 'path': device.device.path}
    deadline = time.monotonic() + 240
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            old_umask = os.umask(0o177)
            try:
                listener.bind('/proc/self/fd/' + str(directory_fd) + '/inject.sock')
            finally:
                os.umask(old_umask)
            os.chown('inject.sock', uid, -1, dir_fd=directory_fd, follow_symlinks=False)
            listener.listen(1)
            listener.settimeout(30)
            client, _ = listener.accept()
            with client:
                _, peer_uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if peer_uid != uid:
                    raise PermissionError('wrong peer uid')
                client.settimeout(5)
                stream = client.makefile('rb')
                while time.monotonic() < deadline:
                    raw = stream.readline(4097)
                    if not raw:
                        return
                    if len(raw) > 4096 or not raw.endswith(b'\n'):
                        raise ValueError('oversized frame')
                    request = json.loads(raw)
                    index = request.get('mouse', 0)
                    if type(index) is not int or index not in (0, 1):
                        raise ValueError('invalid mouse')
                    operation = request.get('op')
                    if operation == 'create':
                        result = create(index)
                    elif operation == 'close':
                        close(index)
                        result = {}
                    elif operation == 'events':
                        for kind, code, value in valid_events(request['events']):
                            devices[index].write(kind, code, value)
                            if kind == e.EV_KEY:
                                if value: held[index].add(code)
                                else: held[index].discard(code)
                        result = {}
                    elif operation == 'release_all':
                        if index in devices:
                            for code in BUTTONS:
                                devices[index].write(e.EV_KEY, code, 0)
                            devices[index].syn()
                            held[index].clear()
                        result = {}
                    elif operation == 'ping':
                        result = {}
                    elif operation == 'quit':
                        client.sendall(b'{"ok":true}\n')
                        return
                    else:
                        raise ValueError('invalid operation')
                    client.sendall(json.dumps({'ok': True, **result}).encode() + b'\n')
    finally:
        for index in list(devices):
            try:
                close(index)
            except OSError:
                pass
        try:
            os.unlink('inject.sock', dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)

if __name__ == '__main__':
    main()
