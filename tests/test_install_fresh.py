"""Fresh installs and desktop rollback tested without installed writes."""
import importlib.util
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, SOURCE / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load('fresh_installer', 'packaging/install.py')
setup = load('fresh_session_setup', 'packaging/session_setup.py')


class FreshServices:
    def __init__(self, root, fail=False):
        self.root, self.fail = root, fail
        self.calls = []
        self.setup_source = 'trusted test setup'
        self.link = root / 'user-home/.config/systemd/user/graphical-session.target.wants/midscroll-overlay.service'

    def snapshot(self):
        return {which: {'active': False, 'enabled': 'not-found', 'exists': False}
                for which in ('system', 'user')}

    def stop(self):
        self.calls.append('stop')

    def reload(self):
        self.calls.append('reload')

    def user_setup(self, action, identifier):
        self.calls.append(('setup', action, identifier))

    def enable_new(self, fresh):
        self.calls.append(('enable', fresh))
        if fresh:
            self.link.parent.mkdir(parents=True, exist_ok=True)
            self.link.symlink_to('/etc/systemd/user/midscroll-overlay.service')

    def disable_new(self, fresh):
        self.calls.append(('disable', fresh))
        if fresh:
            self.link.unlink(missing_ok=True)

    def restore_activity(self, state):
        self.calls.append(('activity', state))

    def health(self, state):
        self.calls.append(('health', state))
        if self.fail:
            self.fail = False
            raise RuntimeError('fresh startup failed')


class FreshTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def transaction(self, identifier, fail=False):
        services = FreshServices(self.root, fail)
        transaction = install.Transaction(self.root / 'transactions' / identifier,
                                          self.root, services)
        release = install.BASE / 'releases' / identifier
        transaction.prepare(release, (*install.INTEGRATION, str(install.BASE / 'v2')), fresh=True)
        stage = self.root / ('stage-' + identifier)
        (stage / 'release').mkdir(parents=True)
        (stage / 'release/midscrolld').write_bytes(b'test executable')
        for absolute in install.INTEGRATION:
            target = stage / ('integration' + absolute)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'test integration')
        return transaction, services, release, stage

    def test_first_install_creates_generic_defaults_and_enables_both_services(self):
        transaction, services, release, stage = self.transaction('success')
        transaction.install(stage, release)
        text = (self.root / str(install.CONFIG).lstrip('/')).read_text()
        self.assertIn('IGNORE_DEVICES=\n', text)
        self.assertNotIn('CORSAIR', text)
        self.assertTrue((self.root / str(install.MARKER).lstrip('/')).is_file())
        self.assertIn(('enable', True), services.calls)
        desired = [call[1] for call in services.calls if isinstance(call, tuple) and call[0] == 'health'][0]
        self.assertTrue(desired['system']['active'] and desired['user']['active'])
        self.assertIn(('setup', 'commit', 'success'), services.calls)

    def test_fresh_public_directories_remain_traversable_under_root_umask(self):
        transaction, services, release, stage = self.transaction('private-umask')
        previous = os.umask(0o077)
        try:
            transaction.install(stage, release)
        finally:
            os.umask(previous)
        for absolute in (str(install.BASE), str(install.BASE / 'releases'),
                         '/usr/local/bin', '/usr/local/share/applications', '/etc/systemd/user'):
            directory = self.root / absolute.lstrip('/')
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755, absolute)

    def test_failed_fresh_install_restores_absence_preferences_and_startup(self):
        transaction, services, release, stage = self.transaction('failure', fail=True)
        with self.assertRaisesRegex(RuntimeError, 'fresh startup'):
            transaction.install(stage, release)
        for absolute in (*install.INTEGRATION, str(install.CONFIG), str(install.MARKER), str(install.BASE / 'v2')):
            self.assertFalse(os.path.lexists(self.root / absolute.lstrip('/')), absolute)
        self.assertFalse(os.path.lexists(services.link))
        self.assertIn(('setup', 'rollback', 'failure'), services.calls)
        self.assertIn(('activity', services.snapshot()), services.calls)
        retry, services, release, stage = self.transaction('retry')
        retry.install(stage, release)
        self.assertEqual(json.loads(retry.journal_path.read_text())['phase'], 'committed')

    def test_nonexistent_services_are_not_stopped(self):
        services = install.Services(SimpleNamespace(pw_uid=1000, pw_name='test'))
        with patch.object(services, 'show', return_value='LoadState=not-found'), patch.object(services, 'control') as control:
            services.stop()
        control.assert_not_called()

    def test_restorecon_is_optional_on_non_selinux_distributions(self):
        services = install.Services(SimpleNamespace(pw_uid=1000, pw_name='test'))
        with patch.object(install.shutil, 'which', return_value=None), patch.object(install, 'command') as command:
            services.relabel(['/test'])
        command.assert_not_called()

    def test_untrusted_existing_install_directory_is_rejected(self):
        directory = self.root / 'usr/local/lib/midscroll'
        directory.mkdir(parents=True)
        directory.chmod(0o777)
        with self.assertRaisesRegex(ValueError, 'unsafe installation directory'):
            install.validate_public_paths([directory], self.root, os.getuid())
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'unsafe installation directory'):
            install.validate_public_paths([directory], self.root, os.getuid() + 1)

    def test_normal_bazzite_usr_local_alias_is_supported(self):
        real = self.root / 'var/usrlocal'
        real.mkdir(parents=True)
        real.chmod(0o755)
        real.parent.chmod(0o755)
        (self.root / 'usr').mkdir()
        (self.root / 'usr/local').symlink_to(real, target_is_directory=True)
        install.validate_public_paths([self.root / 'usr/local/lib/midscroll'], self.root, os.getuid())

    def test_symlink_cannot_hide_a_writable_lexical_ancestor(self):
        unsafe, safe = self.root / 'unsafe', self.root / 'safe'
        unsafe.mkdir()
        unsafe.chmod(0o777)
        safe.mkdir()
        safe.chmod(0o755)
        (unsafe / 'alias').symlink_to(safe, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe installation directory'):
            install.validate_public_paths([unsafe / 'alias/new'], self.root, os.getuid())

    def test_nested_alias_cannot_hide_writable_resolved_intermediate_parent(self):
        unsafe, safe, entry = self.root / 'unsafe', self.root / 'safe', self.root / 'entry'
        for directory in (unsafe, safe, entry, unsafe / 'inner'):
            directory.mkdir()
            directory.chmod(0o755)
        unsafe.chmod(0o777)
        (entry / 'first').symlink_to(unsafe / 'inner', target_is_directory=True)
        (unsafe / 'inner/second').symlink_to(safe, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'unsafe installation directory'):
            install.validate_public_paths([entry / 'first/second/new'], self.root, os.getuid())

    def test_dependency_commands_do_not_upgrade_the_whole_distribution(self):
        for distro, manager in (('debian', 'apt'), ('ubuntu', 'apt'), ('fedora', 'dnf'), ('arch', 'pacman')):
            actual, commands = install.dependency_commands({'ID': distro}, immutable=False)
            self.assertEqual(actual, manager)
            self.assertFalse(any('upgrade' in argument or argument in ('-Syu', '-Sy') for argv in commands for argument in argv))
        manager, commands = install.dependency_commands({'ID': 'bazzite', 'ID_LIKE': 'fedora'}, immutable=True)
        self.assertIsNone(manager)
        self.assertEqual(commands[0][0], 'rpm-ostree')

    def test_missing_accessibility_bus_uses_dependency_install_path(self):
        user = SimpleNamespace(pw_uid=1000, pw_name='test')
        responses = iter((SimpleNamespace(returncode=1, stdout=''), SimpleNamespace(returncode=0, stdout='')))
        with patch.object(install, 'command', return_value=SimpleNamespace(returncode=0, stdout='')), \
             patch.object(install.Services, 'user_command', side_effect=lambda *a, **k: next(responses)) as user_command, \
             patch.object(install, 'dependency_commands', return_value=('apt', [['mock-package-install', 'at-spi2-core']])), \
             patch.object(install.subprocess, 'run') as package_run:
            install.check_dependencies(install_deps=True, user=user)
        package_run.assert_called_once()
        self.assertEqual(user_command.call_count, 2)

    def test_missing_accessibility_bus_without_flag_prints_actionable_command(self):
        user = SimpleNamespace(pw_uid=1000, pw_name='test')
        with patch.object(install, 'command', return_value=SimpleNamespace(returncode=0, stdout='')), \
             patch.object(install.Services, 'user_command', return_value=SimpleNamespace(returncode=1)), \
             patch.object(install, 'dependency_commands', return_value=('apt', [['apt-get', 'install', 'at-spi2-core']])):
            with self.assertRaisesRegex(ValueError, '--install-deps'):
                install.check_dependencies(user=user)

    def test_plasma5_is_rejected_before_dependency_installation(self):
        user = SimpleNamespace(pw_uid=1000, pw_name='test', pw_dir='/home/test')
        def run(argv, **kwargs):
            if '--version' in argv:
                return SimpleNamespace(stdout='Windows Scroll Linux 1.0.0', returncode=0)
            if 'show-seat' in argv:
                return SimpleNamespace(stdout='2\n', returncode=0)
            if 'show-session' in argv:
                return SimpleNamespace(stdout='User=1000\nType=wayland\nRemote=no\nSeat=seat0\n', returncode=0)
            raise AssertionError(argv)
        exists = Path.exists
        with patch.object(install, 'command', run), \
             patch.object(install, 'validate_public_paths'), \
             patch.object(install.Services, 'user_command', return_value=SimpleNamespace(stdout='s "KWin version: 5.27.9\\n"')), \
             patch.object(Path, 'exists', lambda path: True if str(path).startswith('/run/user/') else exists(path)), \
             patch.object(install, 'check_dependencies') as dependencies:
            with self.assertRaisesRegex(ValueError, 'running compositor'):
                install.preflight(self.root, user, install_deps=True)
        dependencies.assert_not_called()

    def test_session_query_uses_separate_loginctl_property_options(self):
        user = SimpleNamespace(pw_uid=1000, pw_name='test', pw_dir='/home/test')
        session_query = ['/usr/bin/loginctl', 'show-session', '2',
                         '--property=User', '--property=Type', '--property=Remote', '--property=Seat']
        def run(argv, **kwargs):
            if '--version' in argv:
                return SimpleNamespace(stdout='Windows Scroll Linux 1.0.0', returncode=0)
            if 'show-seat' in argv:
                return SimpleNamespace(stdout='2\n', returncode=0)
            if 'show-session' in argv:
                self.assertEqual(argv, session_query)
                return SimpleNamespace(stdout='User=1000\nType=wayland\nRemote=no\nSeat=seat0\n', returncode=0)
            raise AssertionError(argv)
        exists = Path.exists
        with patch.object(install, 'command', side_effect=run) as command, \
             patch.object(install, 'validate_public_paths'), \
             patch.object(Path, 'exists', lambda path: True if str(path).startswith('/run/user/') else exists(path)), \
             patch.object(install.Services, 'user_command', side_effect=RuntimeError('session accepted')):
            with self.assertRaisesRegex(RuntimeError, 'session accepted'):
                install.preflight(self.root, user)
        command.assert_any_call(session_query)


class DesktopSetup(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / '.config'
        self.env = self.config / 'plasma-workspace/env/90-windows-scroll-linux.sh'
        self.keys = ((self.config / 'kwinrc', 'Wayland', 'EnablePrimarySelection'),
                     (self.config / 'gtk-3.0/settings.ini', 'Settings', 'gtk-enable-primary-paste'),
                     (self.config / 'gtk-4.0/settings.ini', 'Settings', 'gtk-enable-primary-paste'))
        self.values = {}
        for key, value in (('BASE', self.home / '.local/share/windows-scroll-linux'),
                           ('CONFIG', self.config), ('ENV', self.env), ('KEYS', self.keys)):
            self.patches.enter_context(patch.object(setup, key, value))
        self.patches.enter_context(patch.object(setup.os, 'geteuid', return_value=1000))
        self.patches.enter_context(patch.object(setup, 'a11y', return_value=False))
        self.patches.enter_context(patch.object(setup, 'reconfigure'))
        def get(file, group, key):
            return self.values.get((str(file), group, key), '__absent__')
        def put(file, group, key, value):
            self.values[(str(file), group, key)] = value
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f'[{group}]\n{key}={value}\n')
        self.patches.enter_context(patch.object(setup, 'key_value', get))
        self.patches.enter_context(patch.object(setup, 'set_key', put))

    def test_full_user_setup_rollback_removes_new_files_and_environment(self):
        setup.main('prepare', 'fresh')
        setup.main('apply', 'fresh')
        self.assertEqual(self.env.read_text(), setup.ENV_TEXT)
        setup.main('rollback', 'fresh')
        self.assertFalse(self.env.exists())
        for path, _group, _key in self.keys:
            self.assertFalse(path.exists())

    def test_existing_user_file_bytes_and_mode_restored_on_failure(self):
        path = self.keys[0][0]
        path.parent.mkdir(parents=True)
        original = '# Existing unrelated user settings\n[Other]\nValue=keep\n'
        path.write_text(original)
        path.chmod(0o640)
        setup.main('prepare', 'existing')
        setup.main('apply', 'existing')
        setup.main('rollback', 'existing')
        self.assertEqual(path.read_text(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_failed_commit_does_not_leave_stale_original_preferences(self):
        setup.main('prepare', 'retryable')
        setup.main('apply', 'retryable')
        setup.main('commit', 'retryable')
        setup.main('rollback', 'retryable')
        self.assertFalse((setup.BASE / 'original.json').exists())

    def test_refuses_user_environment_file_with_unrelated_content(self):
        self.env.parent.mkdir(parents=True)
        self.env.write_text('export USER_SETTING=keep\n')
        with self.assertRaisesRegex(ValueError, 'different content'):
            setup.main('prepare', 'collision')
        self.assertEqual(self.env.read_text(), 'export USER_SETTING=keep\n')


if __name__ == '__main__':
    unittest.main()
