#!/usr/bin/python3
"""Windows Scroll Linux v1 transactional installer for KDE Plasma 6 Wayland.

Build the payload with build_payload.py first. Root never executes source from
the user-writable payload: copy, reject links, verify the complete manifest,
then validate the root-owned copy before stopping any service.
"""
from __future__ import annotations

import fcntl
import errno
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

DEFAULT_CONFIG = '''# Windows Scroll Linux v1\nDEADZONE_PX=15\nSPEED_MULT=0.008\nSPEED_EXP=2.2\nMAX_PX_PER_SEC=30000\nPX_PER_NOTCH=55\nMAX_DRAG_PX=1200\nTICK_HZ=90\nGHOST_SCALE=1\nNATURAL=false\nGHOST_CURSOR=true\nSNAP_CURSOR_ON_RELEASE=true\nEXTRA_DEVICES=\nIGNORE_DEVICES=\n'''
CONFIG = Path('/etc/midscroll.conf')
MARKER = Path('/usr/local/lib/midscroll/INSTALLATION.txt')
SYSTEM_ENABLE_LINK = '/etc/systemd/system/multi-user.target.wants/midscroll.service'

BASE = Path('/usr/local/lib/midscroll')
STATE = Path('/var/lib/midscroll/v2-transactions')
LOCK = Path('/run/midscroll-admin/config.lock')
INTEGRATION = (
    '/usr/local/bin/midscroll',
    '/usr/local/bin/midscroll-overlay',
    '/usr/local/bin/midscroll-control',
    '/usr/local/bin/midscroll-settings',
    '/usr/local/bin/midscroll-apply',
    '/etc/systemd/system/midscroll.service',
    '/etc/systemd/user/midscroll-overlay.service',
    '/usr/local/share/applications/io.github.gnhen.midscroll.Settings.desktop',
    '/usr/local/share/applications/midscroll-start.desktop',
    '/usr/local/share/applications/midscroll-stop.desktop',
)
REQUIRED_RELEASE = {'release/midscrolld', 'release/LICENSE', 'release/VERSION',
                    'release/session_setup.py',
                    'release/README.md', 'release/REVIEW-RESOLUTION.md', 'release/THIRD_PARTY_NOTICES.md',
                    'release/RUST_STDLIB_NOTICES.html',
                    'release/app/context_session.py', 'release/app/context_policy.py',
                    'release/app/kwin_context.py', 'release/app/overlay-base.py',
                    'release/app/control.py', 'release/app/settings.py',
                    'release/app/apply_settings.py', 'release/app/move-all.svg'}
MAX_PAYLOAD = 100 * 1024 * 1024


def invoking_user(environ=None):
    environ = os.environ if environ is None else environ
    raw = environ.get('PKEXEC_UID') or environ.get('SUDO_UID')
    if not raw or not raw.isdigit() or int(raw) <= 0:
        raise ValueError('cannot identify the invoking desktop user; run with pkexec or sudo from that user')
    user = pwd.getpwuid(int(raw))
    if not user.pw_dir.startswith('/'):
        raise ValueError('invoking user has no absolute home directory')
    return user


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode(), 0o600)


def public_parents(path):
    """Give newly created integration directories traversal despite root umask."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)


def validate_public_paths(paths, boundary=Path('/'), expected_uid=0):
    """Resolve normal system aliases, then reject writable/untrusted ancestors."""
    checked = set()
    boundary = boundary.resolve()
    pending = [Path(path).absolute() for path in paths]
    while pending:
        directory = pending.pop()
        while directory != boundary and directory not in checked:
            checked.add(directory)
            resolved = directory.resolve()
            if resolved != directory and resolved not in checked:
                pending.append(resolved)
            if directory.exists():
                metadata = directory.stat()
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != expected_uid
                        or metadata.st_mode & 0o022):
                    raise ValueError(f'unsafe installation directory ownership or permissions: {directory}')
                if metadata.st_mode & 0o005 != 0o005:
                    raise ValueError(f'installation directory must be readable and traversable by the desktop user: {directory}')
            if directory.parent == directory:
                break
            directory = directory.parent


def atomic_bytes(path, data, mode=0o644):
    public_parents(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def nofollow_read(base, relative, maximum):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith('/') or '..' in parts or '.' in parts:
        raise ValueError('unsafe payload path')
    current = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = new
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
                raise ValueError(f'payload must contain bounded regular files: {relative}')
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError(f'payload file grew beyond its limit: {relative}')
            return data
    finally:
        os.close(current)


def verified_stage(source, destination):
    """Copy each bounded manifest file into private staging, then hash that copy."""
    manifest_bytes = nofollow_read(source, 'manifest.json', 65536)
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get('format') != 1:
        raise ValueError('unsupported manifest format')
    entries = manifest.get('files')
    if not isinstance(entries, dict) or not 10 <= len(entries) <= 100:
        raise ValueError('invalid manifest file list')
    if any(not isinstance(name, str) or name != PurePosixPath(name).as_posix()
           for name in entries):
        raise ValueError('manifest paths must be canonical relative paths')
    required = REQUIRED_RELEASE | {f'integration{path}' for path in INTEGRATION}
    if not required <= set(entries):
        raise ValueError('manifest is missing required files')
    allowed = required | {name for name in entries if name.startswith('release/app/')
                         and len(PurePosixPath(name).parts) == 3
                         and name.endswith(('.py', '.svg'))}
    if set(entries) != allowed:
        raise ValueError('manifest includes unexpected paths')
    total = 0
    for name, entry in entries.items():
        if not isinstance(entry, dict) or entry.get('mode') not in (0o644, 0o755):
            raise ValueError(f'invalid file attributes: {name}')
        limit = 64 * 1024 * 1024 if name in ('release/midscrolld', 'integration/usr/local/bin/midscroll') else 2 * 1024 * 1024
        data = nofollow_read(source / 'payload', name, limit)
        total += len(data)
        if total > MAX_PAYLOAD or hashlib.sha256(data).hexdigest() != entry.get('sha256'):
            raise ValueError(f'payload hash or size mismatch: {name}')
        target = destination / name
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic_bytes(target, data, entry['mode'])
    for directory in destination.rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)
    # The immutable installed directory has no user-owned links or files.
    return manifest


def command(argv, check=True, timeout=30):
    return subprocess.run(argv, check=check, timeout=timeout, text=True,
                          capture_output=True,
                          env={'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C.UTF-8'})


class Services:
    def __init__(self, user, setup_source=None):
        self.user = user
        self.setup_source = setup_source

    def user_command(self, argv, check=True):
        return command(['/usr/sbin/runuser', '-u', self.user.pw_name, '--',
                        '/usr/bin/env', f'XDG_RUNTIME_DIR=/run/user/{self.user.pw_uid}',
                        f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{self.user.pw_uid}/bus',
                        *argv], check=check)

    def control(self, which, operation, check=True):
        argv = ['/usr/bin/systemctl'] + (['--user'] if which == 'user' else [])
        argv += [operation]
        if operation != 'daemon-reload':
            argv.append('midscroll-overlay.service' if which == 'user' else 'midscroll.service')
        return self.user_command(argv, check) if which == 'user' else command(argv, check)

    def snapshot(self):
        return {which: {'active': self.control(which, 'is-active', False).returncode == 0,
                        'enabled': self.control(which, 'is-enabled', False).stdout.strip(),
                        'exists': ('LoadState=not-found' not in self.show(which, 'LoadState'))}
                for which in ('system', 'user')}

    def show(self, which, properties):
        unit = 'midscroll-overlay.service' if which == 'user' else 'midscroll.service'
        argv = ['/usr/bin/systemctl'] + (['--user'] if which == 'user' else [])
        argv += ['show', unit, '--property=' + properties]
        return (self.user_command(argv, False) if which == 'user' else command(argv, False)).stdout

    def user_setup(self, action, identifier):
        if self.setup_source:
            self.user_command(['/usr/bin/python3', '-I', '-c', self.setup_source, action, identifier])

    def enable_new(self, fresh):
        if fresh:
            for which in ('system', 'user'):
                self.control(which, 'enable')

    def disable_new(self, fresh):
        if fresh:
            for which in ('user', 'system'):
                self.control(which, 'disable', False)

    def stop(self):
        errors = []
        for which in ('user', 'system'):
            try:
                if 'LoadState=not-found' not in self.show(which, 'LoadState'):
                    self.control(which, 'stop')
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f'{which}: {error}')
        if errors:
            raise RuntimeError('could not stop services: ' + '; '.join(errors))

    def reload(self):
        self.control('system', 'daemon-reload')
        self.control('user', 'daemon-reload')

    def relabel(self, paths):
        executable = shutil.which('restorecon', path='/usr/sbin:/usr/bin:/sbin:/bin')
        if executable:
            command([executable, '-RF', *map(str, paths)])

    def restore_activity(self, before):
        for which in ('system', 'user'):
            if before[which]['active']:
                self.control(which, 'start')
            elif before[which].get('exists', True):
                self.control(which, 'stop')

    def health(self, before):
        previous = None
        for sample in range(3):
            if before['system']['active']:
                output = command(['/usr/bin/systemctl', 'show', 'midscroll.service',
                                  '--property=ActiveState,SubState,MainPID,NRestarts']).stdout
                state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
                if state.get('ActiveState') != 'active' or state.get('SubState') != 'running' or int(state.get('MainPID', 0)) <= 0:
                    raise RuntimeError('daemon did not become ready')
                identity = (state['MainPID'], state.get('NRestarts'))
                if previous is not None and identity != previous:
                    raise RuntimeError('daemon restarted during installation health check')
                previous = identity
            if before['user']['active']:
                self.control('user', 'is-active')
            if sample < 2:
                time.sleep(1.8)
        if before['system']['active']:
            try:
                daemon = json.loads(command(['/usr/local/bin/midscroll', '--status']).stdout)
                attached = daemon.get('attached_mice')
                if (str(daemon.get('pid')) != previous[0] or daemon.get('backend') != 'rust'
                        or daemon.get('seat') != 'seat0' or type(attached) is not int
                        or attached < 1):
                    raise ValueError('no current, attached relative mouse')
            except (ValueError, AttributeError) as error:
                raise RuntimeError('input service is running but has no verified attached mouse; release mouse buttons and check device access') from error
        if before['system']['active'] and before['user']['active']:
            for _ in range(10):
                result = self.user_command(['/usr/local/bin/midscroll-control', 'status'], False)
                try:
                    connected = json.loads(result.stdout).get('connected', False)
                except (ValueError, AttributeError):
                    connected = False
                if result.returncode == 0 and connected:
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError('session helper did not connect to the daemon')


def clone_entry(source, target):
    """Copy a regular file or symlink, preserving ownership, mode and xattrs."""
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        os.symlink(os.readlink(source), target)
        os.lchown(target, metadata.st_uid, metadata.st_gid)
        shutil.copystat(source, target, follow_symlinks=False)
    elif stat.S_ISREG(metadata.st_mode):
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chown(target, metadata.st_uid, metadata.st_gid)
        shutil.copystat(source, target, follow_symlinks=False)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        raise ValueError(f'refusing to replace a non-file path: {source}')
    fsync_directory(target.parent)


def replace_entry(source, target):
    public_parents(target.parent)
    temporary = target.parent / f'.{target.name}.{uuid.uuid4().hex}'
    try:
        clone_entry(source, temporary)
        os.replace(temporary, target)
        fsync_directory(target.parent)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


class Transaction:
    """Durable backups permit both automatic rollback and retry after a crash."""
    def __init__(self, directory, root=Path('/'), services=None):
        self.directory = Path(directory)
        self.root = Path(root)
        self.services = services
        self.journal_path = self.directory / 'journal.json'
        self.journal = {}

    def target(self, absolute):
        return self.root / str(absolute).lstrip('/')

    def save(self):
        atomic_json(self.journal_path, self.journal)

    def prepare(self, release, paths, fresh=False):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        backups = self.directory / 'backups'
        backups.mkdir(mode=0o700)
        self.journal = {'format': 1, 'phase': 'preparing', 'release': str(release),
                        'user_uid': self.services.user.pw_uid if hasattr(self.services, 'user') else None,
                        'services': self.services.snapshot(), 'files': {}, 'fresh': fresh,
                        'setup_source': getattr(self.services, 'setup_source', None)}
        self.save()
        paths = tuple(dict.fromkeys((*paths, str(CONFIG), str(MARKER), SYSTEM_ENABLE_LINK)))
        for number, absolute in enumerate(paths):
            target = self.target(absolute)
            present = os.path.lexists(target)
            key = str(number)
            if present:
                clone_entry(target, backups / key)
            self.journal['files'][str(absolute)] = key if present else None
        if hasattr(self.services, 'user_setup'):
            self.services.user_setup('prepare', self.directory.name)
        self.journal['phase'] = 'prepared'
        self.save()

    def install(self, staged, release):
        try:
            self.services.stop()
            self.journal['phase'] = 'installing'
            self.save()
            release_target = self.target(release)
            public_parents(release_target.parent)
            try:
                os.replace(staged / 'release', release_target)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                # The version is not live until its symlink is replaced below.
                shutil.copytree(staged / 'release', release_target)
            fsync_directory(release_target.parent)
            for absolute in INTEGRATION:
                replace_entry(staged / f'integration{absolute}', self.target(absolute))
            if not self.target(CONFIG).exists():
                atomic_bytes(self.target(CONFIG), DEFAULT_CONFIG.encode())
            if not self.target(MARKER).exists():
                atomic_bytes(self.target(MARKER), b'Windows Scroll Linux v1\nManaged installation; internal midscroll identifiers retained for compatibility.\n')
            link = self.directory / 'new-v2-link'
            os.symlink(str(release), link)
            replace_entry(link, self.target(BASE / 'v2'))
            if hasattr(self.services, 'relabel'):
                self.services.relabel((release_target, *[self.target(path) for path in INTEGRATION],
                                       self.target(BASE / 'v2'), self.target(CONFIG), self.target(MARKER)))
            self.services.reload()
            if hasattr(self.services, 'user_setup'):
                self.services.user_setup('apply', self.directory.name)
            fresh = self.journal.get('fresh', False)
            if hasattr(self.services, 'enable_new'):
                self.services.enable_new(fresh)
            desired = self.journal['services'] if not fresh else {
                which: {'active': True, 'enabled': 'enabled', 'exists': True}
                for which in ('system', 'user')}
            self.services.restore_activity(desired)
            self.services.health(desired)
            if hasattr(self.services, 'user_setup'):
                self.services.user_setup('commit', self.directory.name)
            self.journal['phase'] = 'committed'
            self.save()
        except BaseException:
            self.rollback()
            raise

    def rollback(self):
        self.journal = json.loads(self.journal_path.read_text())
        if self.journal['phase'] in ('committed', 'rolled_back', 'preparing'):
            return
        self.journal['phase'] = 'rolling_back'
        self.save()
        # If stopping a broken service fails, still restore its executable and unit.
        errors = []
        try:
            self.services.stop()
        except Exception as error:
            errors.append(str(error))
        if hasattr(self.services, 'disable_new'):
            try:
                self.services.disable_new(self.journal.get('fresh', False))
            except Exception as error:
                errors.append('startup restoration: ' + str(error))
        if hasattr(self.services, 'user_setup'):
            try:
                self.services.user_setup('rollback', self.directory.name)
            except Exception as error:
                errors.append('desktop preferences restoration: ' + str(error))
        for absolute, key in self.journal['files'].items():
            target = self.target(absolute)
            try:
                if key is None:
                    if os.path.lexists(target):
                        target.unlink()
                        fsync_directory(target.parent)
                else:
                    replace_entry(self.directory / 'backups' / key, target)
            except Exception as error:
                errors.append(f'{absolute}: {error}')
        if errors:
            self.journal['phase'] = 'rollback_failed'
            self.journal['errors'] = errors
            self.save()
            raise RuntimeError('rollback incomplete; retained backups at ' + str(self.directory) + ': ' + '; '.join(errors))
        release = self.target(self.journal['release'])
        if release.exists():
            shutil.rmtree(release)
        self.services.reload()
        self.services.restore_activity(self.journal['services'])
        self.journal['phase'] = 'rolled_back'
        self.save()


def recover_pending(state, root=Path('/'), services_factory=None):
    if not state.exists():
        return
    for journal_path in sorted(state.glob('*/journal.json')):
        journal = json.loads(journal_path.read_text())
        if journal.get('phase') in ('committed', 'rolled_back', 'preparing'):
            continue
        services = services_factory(journal) if services_factory else Services(
            pwd.getpwuid(journal['user_uid']), journal.get('setup_source'))
        transaction = Transaction(journal_path.parent, root, services)
        transaction.rollback()


def dependency_commands(os_release=None, immutable=None):
    if os_release is None:
        os_release = {}
        for line in Path('/etc/os-release').read_text().splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                os_release[key] = value.strip('"')
    identifiers = (os_release.get('ID', '') + ' ' + os_release.get('ID_LIKE', '')).split()
    if immutable is None:
        immutable = Path('/run/ostree-booted').exists()
    if immutable:
        return None, [['rpm-ostree', 'install', 'python3-gobject', 'python3-dbus', 'gtk4', 'at-spi2-core']]
    if any(value in identifiers for value in ('debian', 'ubuntu')):
        return 'apt', [['/usr/bin/apt-get', 'update'], ['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends',
            'python3-gi', 'python3-dbus', 'python3-cairo', 'python3-gi-cairo', 'gir1.2-gtk-4.0', 'gir1.2-atspi-2.0', 'at-spi2-core', 'libsystemd0', 'libudev1']]
    if 'fedora' in identifiers:
        return 'dnf', [['/usr/bin/dnf', 'install', '-y', 'python3-gobject', 'python3-dbus', 'python3-cairo', 'gtk4', 'at-spi2-core', 'systemd-libs']]
    if any(value in identifiers for value in ('arch', 'manjaro')):
        return 'pacman', [['/usr/bin/pacman', '-S', '--needed', '--noconfirm', 'python-gobject', 'python-dbus', 'python-cairo', 'gtk4', 'at-spi2-core', 'systemd-libs']]
    return None, []


def check_dependencies(install_deps=False, user=None):
    probe = ['/usr/bin/python3', '-I', '-c',
             'import ctypes,dbus,gi; ctypes.CDLL("libsystemd.so.0"); ctypes.CDLL("libudev.so.1"); '
             'gi.require_version("Gtk","4.0"); gi.require_version("Atspi","2.0"); from gi.repository import Gtk,Atspi']
    result = command(probe, check=False)
    services = Services(user) if user else None
    bus_probe = ['/usr/bin/python3', '-I', '-c',
        'import dbus; p=dbus.Interface(dbus.SessionBus().get_object("org.a11y.Bus","/org/a11y/bus"),'
        '"org.freedesktop.DBus.Properties"); p.Get("org.a11y.Status","IsEnabled")']
    bus_result = services.user_command(bus_probe, check=False) if services and result.returncode == 0 else result
    if result.returncode == 0 and bus_result.returncode == 0:
        return
    manager, commands = dependency_commands()
    if install_deps and manager:
        print('Installing the required desktop libraries through ' + manager + '.', flush=True)
        for argv in commands:
            subprocess.run(argv, check=True, timeout=900, env={
                'PATH': '/usr/sbin:/usr/bin', 'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'})
        command(probe)
        if services:
            services.user_command(bus_probe)
        return
    instructions = '\n'.join('  sudo ' + ' '.join(argv) for argv in commands)
    suffix = '\nReboot after rpm-ostree changes, then rerun the installer.' if manager is None and commands else ''
    if manager:
        suffix = '\nOr rerun: bash install.sh --install-deps'
    raise ValueError('Missing Python GTK4, AT-SPI accessibility bus, D-Bus or system libraries.\n' +
                     (instructions or 'Install Python 3, PyGObject GTK4/AT-SPI, dbus-python, libsystemd and libudev with your distribution package manager.') + suffix)


def preflight(staged, user, install_deps=False):
    if not Path(f'/run/user/{user.pw_uid}/bus').exists():
        raise ValueError('run the installer from your active KDE Plasma 6 Wayland desktop session')
    validate_public_paths((BASE, BASE / 'releases', CONFIG.parent,
                           Path(SYSTEM_ENABLE_LINK).parent,
                           *[Path(path).parent for path in INTEGRATION]))
    # Check the actual binary before dependency installation or managed-file changes.
    try:
        command([str(staged / 'release/midscrolld'), '--version'])
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', '') or str(error)
        raise ValueError('The packaged binary cannot run on this CPU or C library. Use the matching architecture or build from source. ' + detail.strip()) from error
    services = Services(user)
    session = command(['/usr/bin/loginctl', 'show-seat', 'seat0', '--property=ActiveSession', '--value']).stdout.strip()
    data = command(['/usr/bin/loginctl', 'show-session', session,
                    '--property=User', '--property=Type', '--property=Remote', '--property=Seat']).stdout
    active = dict(line.split('=', 1) for line in data.splitlines() if '=' in line)
    if active != {'User': str(user.pw_uid), 'Type': 'wayland', 'Remote': 'no', 'Seat': 'seat0'}:
        raise ValueError('the invoking user must own the active local Wayland session on seat0')
    info = services.user_command(['/usr/bin/busctl', '--user', 'call', 'org.kde.KWin', '/KWin', 'org.kde.KWin', 'supportInformation']).stdout
    version = re.search(r'KWin version:\s*(\d+)\.', info)
    if not version or version.group(1) != '6':
        raise ValueError('the running compositor must be KDE Plasma 6 KWin; Plasma 5 and other desktops are unsupported')
    if not shutil.which('kreadconfig6') or not shutil.which('kwriteconfig6'):
        raise ValueError('KDE Plasma 6 configuration tools kreadconfig6 and kwriteconfig6 must be installed')
    check_dependencies(install_deps, user)
    if os.path.lexists(BASE / 'v2') and not (BASE / 'v2').is_symlink():
        raise ValueError('the internal v2 path is an unmanaged directory; refusing to replace it')
    managed = False
    if os.path.lexists(MARKER):
        metadata = MARKER.lstat()
        managed = stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0 and not metadata.st_mode & 0o022
        if not managed:
            raise ValueError('the existing installation marker is not a trusted root-owned file')
    if not managed:
        for absolute in (*INTEGRATION, str(CONFIG), SYSTEM_ENABLE_LINK):
            if os.path.lexists(absolute):
                raise ValueError(f'an unmanaged file already exists at {absolute}; preserve or remove that installation first')
    fresh = not Path('/etc/systemd/system/midscroll.service').exists()
    if fresh:
        user_link = Path(user.pw_dir) / '.config/systemd/user/graphical-session.target.wants/midscroll-overlay.service'
        if os.path.lexists(user_link):
            raise ValueError('an unmanaged desktop startup link already exists for midscroll-overlay.service')
    if CONFIG.exists():
        metadata = CONFIG.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError('/etc/midscroll.conf must be a trusted root-owned regular file')
        configuration = CONFIG
    else:
        configuration = staged / 'default.conf'
        atomic_bytes(configuration, DEFAULT_CONFIG.encode())
    command([str(staged / 'release/midscrolld'), '--check-config', str(configuration)])
    return fresh


def main():
    parser = argparse.ArgumentParser(description='Install Windows Scroll Linux v1 on KDE Plasma 6 Wayland')
    parser.add_argument('--install-deps', action='store_true', help='install missing runtime libraries through the supported distribution package manager')
    options = parser.parse_args()
    if os.geteuid() != 0:
        os.execv('/usr/bin/pkexec', ['/usr/bin/pkexec', '/usr/bin/python3', '-I',
                                   str(Path(__file__).resolve()), *sys.argv[1:]])
    user = invoking_user()
    os.umask(0o077)
    LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        recover_pending(STATE)
        identifier = time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12]
        transaction_directory = STATE / identifier
        transaction_directory.mkdir(mode=0o700)
        staged = transaction_directory / 'stage'
        staged.mkdir(mode=0o700)
        verified_stage(Path(__file__).resolve().parent, staged)
        fresh = preflight(staged, user, options.install_deps)
        setup_source = (staged / 'release/session_setup.py').read_text()
        release = BASE / 'releases' / identifier
        transaction = Transaction(transaction_directory, services=Services(user, setup_source))
        transaction.prepare(release, (*INTEGRATION, str(BASE / 'v2'), str(CONFIG), str(MARKER), SYSTEM_ENABLE_LINK), fresh=fresh)
        def interrupted(signum, _frame):
            raise InterruptedError(f'installation interrupted by signal {signum}')
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        transaction.install(staged, release)
        print('Windows Scroll Linux v1 installed. Scrolling is ready.' if fresh else 'Windows Scroll Linux v1 updated. Existing startup and running states were preserved.')
        print('Log out and back in once to enable accessibility in applications that initialize it only at startup.')
        print(f'Rollback records: {transaction_directory}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        detail = (error.stderr or '').strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f'Windows Scroll Linux installation failed: {detail or str(error)}', file=sys.stderr)
        raise SystemExit(1)
