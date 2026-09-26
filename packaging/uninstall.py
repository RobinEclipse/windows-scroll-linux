#!/usr/bin/python3
"""Remove Windows Scroll Linux while retaining recovery copies.

Private dependencies, source versions, and backups are deliberately retained in
/usr/local/lib/midscroll and /var/lib/midscroll. No unrelated user file is deleted.
"""
import fcntl
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile

MANAGED = (
    '/etc/systemd/system/midscroll.service',
    '/etc/systemd/user/midscroll-overlay.service', '/etc/midscroll.conf',
    '/usr/local/bin/midscroll', '/usr/local/bin/midscroll-overlay',
    '/usr/local/bin/midscroll-control', '/usr/local/bin/midscroll-settings',
    '/usr/local/bin/midscroll-apply',
    '/usr/local/share/applications/io.github.gnhen.midscroll.Settings.desktop',
    '/usr/local/share/applications/midscroll-start.desktop',
    '/usr/local/share/applications/midscroll-stop.desktop',
    '/usr/local/lib/midscroll/v2',
    '/usr/local/lib/midscroll/INSTALLATION.txt',
)
RESTORE_USER = r'''
from pathlib import Path
import json, subprocess
base = Path.home() / '.local/share/midscroll'
config = Path.home() / '.config'
allowed = {(str((config/'kwinrc').resolve()),'Wayland','EnablePrimarySelection'),
           (str((config/'gtk-3.0/settings.ini').resolve()),'Settings','gtk-enable-primary-paste'),
           (str((config/'gtk-4.0/settings.ini').resolve()),'Settings','gtk-enable-primary-paste')}
try:
    original = json.loads((base / 'user-settings-before.json').read_text())
except (OSError, ValueError):
    original = []
for item in original:
    if not isinstance(item, dict) or not isinstance(item.get('file'), str):
        continue
    identity = (str(Path(item['file']).resolve()), item.get('group'), item.get('key'))
    if identity not in allowed:
        continue
    args = ['--file', identity[0], '--group', identity[1], '--key', identity[2]]
    current = subprocess.run(['/usr/bin/kreadconfig6', *args, '--default', '__absent__'],
                             capture_output=True, text=True, check=True).stdout.strip()
    if current == 'false':
        change = ['--delete'] if item['value'] == '__absent__' else [item['value']]
        subprocess.run(['/usr/bin/kwriteconfig6', *args, *change], check=True)
managed = Path.home() / '.config/plasma-workspace/env/90-midscroll-accessibility.sh'
managed_text = ('#!/bin/sh\n'
    '# Managed by the context-aware midscroll setup. Expose application controls\n'
    '# early at login, including apps that initialize accessibility only at startup.\n'
    'export ACCESSIBILITY_ENABLED=1\nexport QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\n')
if managed.is_file() and managed.read_text() == managed_text:
    managed.unlink()
try:
    import dbus
except ImportError:
    dbus = None
if dbus is not None:
    try:
        original = json.loads((base / 'accessibility-before.json').read_text())
        props = dbus.Interface(dbus.SessionBus().get_object('org.a11y.Bus','/org/a11y/bus'),
                               'org.freedesktop.DBus.Properties')
        if not props.Get('org.a11y.Status','ScreenReaderEnabled'):
            props.Set('org.a11y.Status','IsEnabled',dbus.Boolean(original['IsEnabled']))
    except (OSError, ValueError, KeyError, dbus.DBusException):
        pass
'''


def command(argv, check=True):
    return subprocess.run(argv, check=check, timeout=30, env={
        'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C.UTF-8'})


def main():
    if os.geteuid() != 0:
        os.execv('/usr/bin/pkexec', ['/usr/bin/pkexec', '/usr/bin/python3', '-I',
                                   str(Path(__file__).resolve())])
    uid = os.environ.get('PKEXEC_UID') or os.environ.get('SUDO_UID')
    if not uid or not uid.isdigit() or int(uid) <= 0:
        raise ValueError('run with pkexec or sudo from the desktop user; no default user is assumed')
    user = pwd.getpwuid(int(uid))
    marker = Path('/usr/local/lib/midscroll/INSTALLATION.txt').lstat()
    if not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0 or marker.st_mode & 0o022:
        raise ValueError('managed installation marker was not found or is untrusted')
    os.umask(0o077)
    Path('/run/midscroll-admin').mkdir(mode=0o700, exist_ok=True)
    fd = os.open('/run/midscroll-admin/config.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prefix = ['/usr/sbin/runuser', '-u', user.pw_name, '--', '/usr/bin/env',
                  f'XDG_RUNTIME_DIR=/run/user/{uid}', f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus']
        backup_root = Path('/var/lib/midscroll')
        backup_root.mkdir(mode=0o700, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='uninstall-', dir=backup_root))
        setup_file = Path('/usr/local/lib/midscroll/v2/session_setup.py')
        setup_source = setup_file.read_text() if setup_file.is_file() else None
        for absolute in MANAGED:
            source = Path(absolute)
            if os.path.lexists(source):
                metadata = source.lstat()
                if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                    raise ValueError(f'refusing to remove unexpected non-file: {source}')
                destination = backup / absolute.lstrip('/')
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
        command([*prefix, '/usr/bin/systemctl', '--user', 'disable', '--now', 'midscroll-overlay.service'])
        command(['/usr/bin/systemctl', 'disable', '--now', 'midscroll.service'])
        for absolute in MANAGED:
            path = Path(absolute)
            if os.path.lexists(path):
                path.unlink()
        command(['/usr/bin/systemctl', 'daemon-reload'])
        command([*prefix, '/usr/bin/systemctl', '--user', 'daemon-reload'])
        try:
            if setup_source:
                command([*prefix, '/usr/bin/python3', '-I', '-c', setup_source, 'uninstall', 'uninstall'])
            command([*prefix, '/usr/bin/python3', '-I', '-c', RESTORE_USER])
        except subprocess.SubprocessError:
            print('Services removed; some desktop preferences could not be restored automatically.', file=sys.stderr)
        print('Windows Scroll Linux removed from startup and application menus. Log out to finish restoring desktop preferences.')
        print(f'Recovery copies retained at {backup}; private libraries and source versions are retained.')
        print('Browser profile settings were not changed.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Windows Scroll Linux removal failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
