#!/usr/bin/python3
"""Validate and transactionally apply only supported autoscroll settings.

The Rust parser is the final authority. No caller-supplied path, shell command,
or environment-selected executable is used by the privileged entry point.
"""
from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path('/etc/midscroll.conf')
BINARY = Path('/usr/local/lib/midscroll/v2/midscrolld')
LOCK = Path('/run/midscroll-admin/config.lock')
NUMERIC = {
    'DEADZONE_PX': (0.0, 1000.0, 15.0),
    'SPEED_MULT': (0.000001, 1000.0, 0.008),
    'SPEED_EXP': (0.1, 8.0, 2.2),
    'MAX_PX_PER_SEC': (1.0, 100000.0, 30000.0),
    'PX_PER_NOTCH': (1.0, 10000.0, 55.0),
    'MAX_DRAG_PX': (1.0, 10000.0, 1200.0),
    'TICK_HZ': (10.0, 500.0, 90.0),
    'GHOST_SCALE': (0.1, 10.0, 1.0),
}
BOOLS = {'NATURAL': False, 'GHOST_CURSOR': True,
         'SNAP_CURSOR_ON_RELEASE': True}
DEVICES = ('EXTRA_DEVICES', 'IGNORE_DEVICES')
LEGACY = {'TOGGLE_MODE', 'DESKTOP_SCROLL', 'BLACKLIST', 'ALLOW_KEYBOARDS'}
DEFAULTS = {key: val[2] for key, val in NUMERIC.items()} | BOOLS | {
    key: '' for key in DEVICES}


def parse_bool(value):
    if value.lower() in ('true', 'yes', 'on', '1'):
        return True
    if value.lower() in ('false', 'no', 'off', '0'):
        return False
    raise ValueError('expected true or false')


def device_list(value):
    if len(value.encode()) > 8192:
        raise ValueError('device list is too long')
    specs = [part.strip() for part in value.split(',') if part.strip()]
    if len(specs) > 32:
        raise ValueError('at most 32 device specifications are supported')
    for spec in specs:
        if (len(spec.encode()) > 128 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in spec)
                or any(c in spec for c in '#=\x7f')):
            raise ValueError('invalid device specification')
        if spec.startswith('/'):
            if (not spec.startswith('/dev/input/') or spec == '/dev/input/'
                    or any(part in ('.', '..') for part in spec.split('/'))):
                raise ValueError('device paths must be below /dev/input without . or ..')
        elif re.fullmatch(r'[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}', spec):
            pass
        elif len(spec) < 3:
            raise ValueError('device names must be at least three characters')
    return ', '.join(specs)


def parse_assignments(args, initial=None):
    values = dict(DEFAULTS if initial is None else initial)
    if len(args) > len(DEFAULTS):
        raise ValueError('too many settings')
    seen = set()
    for arg in args:
        if len(arg.encode()) > 8256 or '\n' in arg or '\r' in arg or '=' not in arg:
            raise ValueError('expected a bounded KEY=VALUE argument')
        key, raw = arg.split('=', 1)
        raw = raw.strip()
        if key not in DEFAULTS or key in seen:
            raise ValueError(f'unknown or duplicate setting: {key}')
        seen.add(key)
        if key in NUMERIC:
            lower, upper, _ = NUMERIC[key]
            value = float(raw)
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError(f'{key} must be between {lower:g} and {upper:g}')
        elif key in BOOLS:
            value = parse_bool(raw)
        else:
            value = device_list(raw)
        values[key] = value
    if values['MAX_DRAG_PX'] <= values['DEADZONE_PX']:
        raise ValueError('MAX_DRAG_PX must be greater than DEADZONE_PX')
    return values


def read_values(path=CONFIG):
    text = Path(path).read_text()
    if len(text.encode()) > 65536:
        raise ValueError('configuration exceeds 64 KiB')
    args = []
    seen = set()
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            raise ValueError('malformed configuration line')
        key, value = [part.strip() for part in line.split('=', 1)]
        if key in seen:
            raise ValueError(f'duplicate setting: {key}')
        seen.add(key)
        if key in ('ALLOW_KEYBOARDS', 'TOGGLE_MODE', 'DESKTOP_SCROLL'):
            boolean = parse_bool(value)
            if key == 'ALLOW_KEYBOARDS' and boolean:
                raise ValueError('grabbing keyboard devices is unsupported')
        if key in LEGACY:
            continue
        args.append(f'{key}={value}')
    return parse_assignments(args)


def serialize(values):
    lines = [
        '# Windows Scroll Linux v1 settings; validated by midscrolld.',
        '# Middle-click paste prevention remains enabled while paused.',
        '# Native game/CAD exceptions and keyboard capture are unsupported.',
    ]
    for key in DEFAULTS:
        value = values[key]
        if isinstance(value, bool):
            value = 'true' if value else 'false'
        elif isinstance(value, (float, int)):
            value = format(value, '.15g')
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def trusted_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022 or metadata.st_size > 65536):
            raise ValueError('configuration must be a bounded root-owned regular file')
        return stream.read(), metadata


def atomic_write(path, data, mode=0o644, uid=None, gid=None, xattrs=None):
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            for attribute, value in (xattrs or {}).items():
                os.setxattr(stream.fileno(), attribute, value)
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=20, env={'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C.UTF-8'})


def health(runner=run, delay=1.8):
    previous = None
    for sample in range(3):
        output = runner(['/usr/bin/systemctl', 'show', 'midscroll.service',
                         '--property=ActiveState,SubState,MainPID,NRestarts']).stdout
        state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if (state.get('ActiveState') != 'active' or state.get('SubState') != 'running'
                or not state.get('MainPID', '0').isdigit() or int(state.get('MainPID', '0')) <= 0):
            raise RuntimeError('the daemon did not become ready')
        identity = (state['MainPID'], state.get('NRestarts'))
        if previous is not None and previous != identity:
            raise RuntimeError('the daemon restarted during its health check')
        previous = identity
        if delay and sample < 2:
            time.sleep(delay)


def apply_transaction(values, path=CONFIG, binary=BINARY, runner=run,
                      health_check=health, require_trusted=True):
    """Replace config only after Rust validates it; restore bytes on failure."""
    old, metadata = trusted_read(path) if require_trusted else (path.read_bytes(), path.stat())
    xattrs = {name: os.getxattr(path, name, follow_symlinks=False)
              for name in os.listxattr(path, follow_symlinks=False)}
    data = serialize(values).encode()
    fd, check_name = tempfile.mkstemp(prefix='.midscroll-check.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        runner([str(binary), '--check-config', check_name])
    finally:
        os.unlink(check_name)
    changed = False
    try:
        changed = True
        atomic_write(path, data, stat.S_IMODE(metadata.st_mode), metadata.st_uid,
                     metadata.st_gid, xattrs)
        runner(['/usr/bin/systemctl', 'restart', 'midscroll.service'])
        health_check(runner)
    except BaseException as error:
        if changed:
            atomic_write(path, old, stat.S_IMODE(metadata.st_mode), metadata.st_uid,
                         metadata.st_gid, xattrs)
            try:
                runner(['/usr/bin/systemctl', 'restart', 'midscroll.service'])
                health_check(runner)
            except Exception as restore_error:
                raise RuntimeError(f'settings restored, but the old service could not restart: {restore_error}') from error
        raise


def main(argv):
    if os.geteuid() != 0:
        raise ValueError('apply changes through Windows Scroll Linux v1 Settings')
    os.umask(0o077)
    LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        trusted_read(CONFIG)
        # Read and validate existing settings so partial CLI updates preserve them.
        values = parse_assignments(argv, read_values(CONFIG))
        def interrupted(signum, _frame):
            raise InterruptedError(f'settings update interrupted by signal {signum}')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        apply_transaction(values)
    print('Settings applied; Windows Scroll Linux v1 is ready.')


if __name__ == '__main__':
    try:
        main(sys.argv[1:])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Windows Scroll Linux settings: {exc}', file=sys.stderr)
        raise SystemExit(2)
