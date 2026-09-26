#!/usr/bin/python3
"""User-only reversible desktop preferences. Never execute as root."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

BASE = Path.home() / '.local/share/windows-scroll-linux'
CONFIG = Path.home() / '.config'
ENV = CONFIG / 'plasma-workspace/env/90-windows-scroll-linux.sh'
ENV_TEXT = ('#!/bin/sh\n# Managed by Windows Scroll Linux.\n'
            'export ACCESSIBILITY_ENABLED=1\nexport QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\n')
KEYS = ((CONFIG / 'kwinrc', 'Wayland', 'EnablePrimarySelection'),
        (CONFIG / 'gtk-3.0/settings.ini', 'Settings', 'gtk-enable-primary-paste'),
        (CONFIG / 'gtk-4.0/settings.ini', 'Settings', 'gtk-enable-primary-paste'))


def write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.windows-scroll-linux-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(path):
    if path.is_symlink():
        raise ValueError(f'refusing to replace a symlinked preferences file: {path}')
    if not path.exists():
        return None
    data = path.read_bytes()
    if len(data) > 1024 * 1024:
        raise ValueError(f'preferences file is too large: {path}')
    return {'data': base64.b64encode(data).decode(), 'mode': path.stat().st_mode & 0o777}


def key_value(path, group, key):
    result = subprocess.run(['/usr/bin/kreadconfig6', '--file', str(path), '--group', group,
                             '--key', key, '--default', '__absent__'], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def set_key(path, group, key, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ['/usr/bin/kwriteconfig6', '--file', str(path), '--group', group, '--key', key]
    args += ['--delete'] if value == '__absent__' else [value]
    subprocess.run(args, check=True, capture_output=True, text=True)


def a11y(value=None):
    import dbus
    props = dbus.Interface(dbus.SessionBus().get_object('org.a11y.Bus', '/org/a11y/bus'),
                           'org.freedesktop.DBus.Properties')
    original = bool(props.Get('org.a11y.Status', 'IsEnabled'))
    if value is not None and not props.Get('org.a11y.Status', 'ScreenReaderEnabled'):
        props.Set('org.a11y.Status', 'IsEnabled', dbus.Boolean(value))
    return original


def reconfigure():
    try:
        import dbus
        dbus.Interface(dbus.SessionBus().get_object('org.kde.KWin', '/KWin'),
                       'org.kde.KWin').reconfigure()
    except Exception:
        pass


def restore_preferences(record, whole=False):
    for item in record['keys']:
        path = Path(item['path'])
        if whole:
            current = snapshot(path)
            expected = record.get('after', {}).get(str(path))
            if expected is not None and current == expected:
                original = record['files'][str(path)]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, base64.b64decode(original['data']), original['mode'])
                continue
        if key_value(path, item['group'], item['key']) == 'false':
            set_key(path, item['group'], item['key'], item['value'])
    if ENV.is_file() and ENV.read_text() == ENV_TEXT:
        original = record['files'].get(str(ENV))
        if original is None:
            ENV.unlink()
        else:
            write(ENV, base64.b64decode(original['data']), original['mode'])
    try:
        a11y(record['a11y'])
    except Exception:
        pass
    reconfigure()


def main(action, identifier):
    if os.geteuid() == 0:
        raise ValueError('desktop preferences must be changed as the desktop user')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
        raise ValueError('invalid transaction identifier')
    os.umask(0o077)
    BASE.mkdir(parents=True, exist_ok=True)
    path = BASE / 'transactions' / (identifier + '.json')
    if action == 'prepare':
        files = {str(file): snapshot(file) for file in (*[row[0] for row in KEYS], ENV)}
        if files[str(ENV)] is not None and ENV.read_text() != ENV_TEXT:
            raise ValueError('the login environment file already exists with different content')
        record = {'transaction': identifier, 'files': files, 'a11y': a11y(), 'keys': [
            {'path': str(file), 'group': group, 'key': key,
             'value': key_value(file, group, key)} for file, group, key in KEYS]}
        write(path, json.dumps(record).encode())
    elif action == 'apply':
        record = json.loads(path.read_text())
        # Record every completed write so interruption can restore exact prior files.
        record['after'] = {}
        for file, group, key in KEYS:
            set_key(file, group, key, 'false')
            record['after'][str(file)] = snapshot(file)
            write(path, json.dumps(record).encode())
        write(ENV, ENV_TEXT.encode(), 0o700)
        record['after'][str(ENV)] = snapshot(ENV)
        write(path, json.dumps(record).encode())
        a11y(True)
        reconfigure()
    elif action == 'rollback':
        if path.exists():
            restore_preferences(json.loads(path.read_text()), whole=True)
        original = BASE / 'original.json'
        if original.exists() and json.loads(original.read_text()).get('transaction') == identifier:
            original.unlink()
    elif action == 'commit':
        original = BASE / 'original.json'
        if not original.exists() or json.loads(original.read_text()).get('removed'):
            write(original, path.read_bytes())
    elif action == 'uninstall':
        original = BASE / 'original.json'
        if original.exists():
            record = json.loads(original.read_text())
            restore_preferences(record)
            record['removed'] = True
            write(original, json.dumps(record).encode())
    else:
        raise ValueError('unknown desktop setup action')


if __name__ == '__main__':
    main(*sys.argv[1:])
