"""No test in this module modifies installed files or real services."""
import hashlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
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


install = load('installer_tests_module', 'packaging/install.py')
settings = load('apply_settings_tests_module', 'app/apply_settings.py')
uninstall = load('uninstall_tests_module', 'packaging/uninstall.py')


class FakeServices:
    def __init__(self, fail_health=False):
        self.calls = []
        self.fail_health = fail_health
        self.before = {'system': {'active': True, 'enabled': 'enabled'},
                       'user': {'active': False, 'enabled': 'disabled'}}

    def snapshot(self):
        return self.before

    def stop(self):
        self.calls.append('stop')

    def reload(self):
        self.calls.append('reload')

    def restore_activity(self, states):
        self.calls.append(('restore', states))

    def health(self, states):
        self.calls.append('health')
        if self.fail_health:
            self.fail_health = False
            raise RuntimeError('injected new service failure')


class InstallTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'transactions'
        self.state.mkdir()
        self.originals = {}
        for index, absolute in enumerate(install.INTEGRATION):
            target = self.root / absolute.lstrip('/')
            if index % 3 == 0:
                self.originals[absolute] = None
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                content = f'original {index}\n'.encode()
                target.write_bytes(content)
                target.chmod(0o640)
                self.originals[absolute] = content
        legacy = self.root / 'usr/local/lib/midscroll/context/original.py'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('untouched legacy source')

    def stage(self, name):
        stage = self.root / name
        (stage / 'release/app').mkdir(parents=True)
        (stage / 'release/app/new.py').write_text('new version')
        for absolute in install.INTEGRATION:
            target = stage / f'integration{absolute}'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('new integration')
            target.chmod(0o755 if '/bin/' in absolute else 0o644)
        return stage

    def assert_original(self):
        for absolute, content in self.originals.items():
            target = self.root / absolute.lstrip('/')
            if content is None:
                self.assertFalse(os.path.lexists(target), absolute)
            else:
                self.assertEqual(target.read_bytes(), content, absolute)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual((self.root / 'usr/local/lib/midscroll/context/original.py').read_text(), 'untouched legacy source')

    def transaction(self, name, services):
        transaction = install.Transaction(self.state / name, root=self.root, services=services)
        release = install.BASE / 'releases' / name
        transaction.prepare(release, (*install.INTEGRATION, str(install.BASE / 'v2')))
        return transaction, release

    def test_failed_upgrade_restores_every_created_or_existing_file_and_retry_works(self):
        failed, release = self.transaction('first-attempt', FakeServices(fail_health=True))
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            failed.install(self.stage('stage-first'), release)
        self.assert_original()
        self.assertFalse(os.path.lexists(self.root / str(install.BASE / 'v2').lstrip('/')))
        self.assertFalse((self.root / str(release).lstrip('/')).exists())
        self.assertEqual(json.loads(failed.journal_path.read_text())['phase'], 'rolled_back')
        retry, release = self.transaction('second-attempt', FakeServices())
        retry.install(self.stage('stage-second'), release)
        self.assertEqual(json.loads(retry.journal_path.read_text())['phase'], 'committed')
        self.assertEqual(os.readlink(self.root / str(install.BASE / 'v2').lstrip('/')), str(release))

    def test_prior_version_symlink_restored_exactly(self):
        link = self.root / str(install.BASE / 'v2').lstrip('/')
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to('releases/previous')
        transaction, release = self.transaction('failed', FakeServices(fail_health=True))
        with self.assertRaises(RuntimeError):
            transaction.install(self.stage('stage'), release)
        self.assertEqual(os.readlink(link), 'releases/previous')
        self.assert_original()

    def test_retry_recovers_interrupted_installation(self):
        services = FakeServices()
        transaction, release = self.transaction('interrupted', services)
        transaction.journal['phase'] = 'installing'
        transaction.save()
        overwritten = self.root / install.INTEGRATION[1].lstrip('/')
        overwritten.write_text('interrupted write')
        release_path = self.root / str(release).lstrip('/')
        release_path.mkdir(parents=True)
        (release_path / 'artifact').write_text('new')
        install.recover_pending(self.state, self.root, lambda _journal: services)
        self.assert_original()
        self.assertFalse(release_path.exists())
        self.assertEqual(json.loads(transaction.journal_path.read_text())['phase'], 'rolled_back')

    def test_inactive_service_states_are_preserved(self):
        services = FakeServices()
        transaction, release = self.transaction('success', services)
        transaction.install(self.stage('stage'), release)
        self.assertIn(('restore', services.before), services.calls)
        self.assertFalse(services.before['user']['active'])

    def test_release_and_backup_may_be_on_different_filesystems(self):
        transaction, release = self.transaction('cross-device', FakeServices())
        stage = self.stage('cross-stage')
        real_replace = os.replace
        def replace(source, destination):
            if Path(source) == stage / 'release':
                raise OSError(errno.EXDEV, 'simulated filesystem boundary')
            return real_replace(source, destination)
        with patch.object(os, 'replace', replace):
            transaction.install(stage, release)
        installed = self.root / str(release).lstrip('/') / 'app/new.py'
        self.assertEqual(installed.read_text(), 'new version')
        self.assertEqual(json.loads(transaction.journal_path.read_text())['phase'], 'committed')

    def test_symlink_targets_restored_without_following(self):
        absolute = install.INTEGRATION[0]
        target = self.root / absolute.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to('../private-target')
        transaction, release = self.transaction('symlink', FakeServices(fail_health=True))
        with self.assertRaises(RuntimeError):
            transaction.install(self.stage('stage'), release)
        self.assertEqual(os.readlink(target), '../private-target')

    def test_root_direct_invocation_has_no_uid1000_fallback(self):
        with self.assertRaisesRegex(ValueError, 'invoking'):
            install.invoking_user({})
        with self.assertRaises(ValueError):
            install.invoking_user({'PKEXEC_UID': '0'})
        with patch.object(install.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir='/home/test', pw_uid=2001)):
            self.assertEqual(install.invoking_user({'SUDO_UID': '2001'}).pw_uid, 2001)

    def test_stop_attempts_input_service_even_if_session_stop_fails(self):
        services = install.Services(SimpleNamespace(pw_uid=2001, pw_name='mock'))
        calls = []
        def control(which, operation):
            calls.append((which, operation))
            if which == 'user':
                raise subprocess.CalledProcessError(1, ['mock-user-stop'])
        with patch.object(services, 'control', control), patch.object(services, 'show', return_value='LoadState=loaded'):
            with self.assertRaises(RuntimeError):
                services.stop()
        self.assertEqual(calls, [('user', 'stop'), ('system', 'stop')])

    def test_install_health_spans_watchdog_interval_without_real_sleep(self):
        services = install.Services(SimpleNamespace(pw_uid=2001, pw_name='mock'))
        calls, delays = [], []
        def run(argv):
            calls.append(argv)
            if argv[-1] == '--status':
                return SimpleNamespace(stdout=json.dumps({'pid': 20, 'backend': 'rust',
                                                         'seat': 'seat0', 'attached_mice': 1}))
            return SimpleNamespace(stdout='ActiveState=active\nSubState=running\nMainPID=20\nNRestarts=0\n')
        with patch.object(install, 'command', run), patch.object(install.time, 'sleep', delays.append):
            services.health({'system': {'active': True}, 'user': {'active': False}})
        self.assertEqual(sum(argv[-1] != '--status' for argv in calls), 3)
        self.assertGreater(sum(delays), 3)

    def test_running_daemon_without_attached_mouse_fails_install_health(self):
        services = install.Services(SimpleNamespace(pw_uid=2001, pw_name='mock'))
        def run(argv):
            if argv[-1] == '--status':
                return SimpleNamespace(stdout=json.dumps({'pid': 20, 'backend': 'rust',
                                                         'seat': 'seat0', 'attached_mice': 0}))
            return SimpleNamespace(stdout='ActiveState=active\nSubState=running\nMainPID=20\nNRestarts=0\n')
        with patch.object(install, 'command', run), patch.object(install.time, 'sleep', lambda _: None):
            with self.assertRaisesRegex(RuntimeError, 'no verified attached mouse'):
                services.health({'system': {'active': True}, 'user': {'active': False}})

    def test_helper_unit_preserves_host_uid_namespace_for_root_authentication(self):
        unit = (SOURCE / 'packaging/midscroll-overlay.service').read_text()
        for setting in ('ProtectSystem', 'ProtectKernelTunables', 'ProtectKernelLogs',
                        'ProtectKernelModules', 'ProtectControlGroups', 'ProtectClock',
                        'ProtectHostname', 'PrivateUsers', 'PrivateTmp', 'CapabilityBoundingSet'):
            self.assertNotIn('\n' + setting + '=', unit)
        for setting in ('NoNewPrivileges=yes', 'AmbientCapabilities=',
                        'RestrictNamespaces=yes', 'SystemCallFilter=@system-service'):
            self.assertIn(setting, unit)


class PayloadValidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.destination = self.base / 'verified'
        self.destination.mkdir()
        self.entries = {}
        required = install.REQUIRED_RELEASE | {f'integration{path}' for path in install.INTEGRATION}
        for name in required:
            target = self.source / 'payload' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = name.encode()
            target.write_bytes(data)
            self.entries[name] = {'mode': 0o644, 'sha256': hashlib.sha256(data).hexdigest()}
        self.manifest()

    def manifest(self):
        (self.source / 'manifest.json').write_text(json.dumps({'format': 1, 'files': self.entries}))

    def test_complete_verified_payload_is_copied_without_original_permissions(self):
        name = 'release/app/context_session.py'
        (self.source / 'payload' / name).chmod(0o777)
        install.verified_stage(self.source, self.destination)
        self.assertEqual(stat.S_IMODE((self.destination / name).stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE((self.destination / 'release/app').stat().st_mode), 0o755)

    def test_changed_payload_is_rejected(self):
        (self.source / 'payload/release/VERSION').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            install.verified_stage(self.source, self.destination)

    def test_leaf_symlink_is_rejected(self):
        target = self.source / 'payload/release/VERSION'
        target.unlink()
        target.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):
            install.verified_stage(self.source, self.destination)

    def test_ancestor_symlink_is_rejected(self):
        original = self.source / 'payload/release/app'
        renamed = self.source / 'app-real'
        original.rename(renamed)
        original.symlink_to(renamed)
        with self.assertRaises(OSError):
            install.verified_stage(self.source, self.destination)

    def test_manifest_cannot_write_outside_payload(self):
        self.entries['release/app/../../etc/shadow'] = {'mode': 0o644, 'sha256': '0' * 64}
        self.manifest()
        with self.assertRaises(ValueError):
            install.verified_stage(self.source, self.destination)

    def test_noncanonical_duplicate_path_is_rejected(self):
        self.entries['release/app//context_session.py'] = self.entries['release/app/context_session.py']
        self.manifest()
        with self.assertRaisesRegex(ValueError, 'canonical'):
            install.verified_stage(self.source, self.destination)

    def test_built_service_executes_real_binary_from_normal_selinux_executable_path(self):
        binary = SOURCE / 'daemon/target-ubuntu2204/release/midscrolld'
        if not binary.is_file():
            binary = SOURCE / 'daemon/target/release/midscrolld'
        if not binary.is_file():
            self.skipTest('build the release binary before testing the distribution payload')
        distribution = self.base / 'built-distribution'
        subprocess.run(['/usr/bin/python3', str(SOURCE / 'packaging/build_payload.py'),
                        str(distribution), '--binary', str(binary)], check=True, capture_output=True, text=True)
        executable = distribution / 'payload/integration/usr/local/bin/midscroll'
        unit = distribution / 'payload/integration/etc/systemd/system/midscroll.service'
        self.assertEqual(executable.read_bytes(), binary.read_bytes())
        self.assertTrue(executable.read_bytes().startswith(b'\x7fELF'))
        self.assertIn('ExecStart=/usr/local/bin/midscroll\n', unit.read_text())
        for name in ('install.sh', 'README.md', 'INSTALL.md', 'LICENSE', 'REVIEW-RESOLUTION.md'):
            self.assertTrue((distribution / name).is_file())
        stage = self.base / 'built-stage'
        stage.mkdir()
        install.verified_stage(distribution, stage)


class SettingsTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'midscroll.conf'
        self.original = b'# user comments stay on rollback\nSPEED_MULT=.012\nIGNORE_DEVICES=CORSAIR STRAFE\n'
        self.config.write_bytes(self.original)
        self.config.chmod(0o640)
        self.values = settings.parse_assignments(['SPEED_MULT=0.02'], settings.read_values(self.config))
        self.calls = []

    def runner(self, argv):
        self.calls.append(argv)
        return SimpleNamespace(stdout='')

    def apply(self, runner=None, health=None):
        settings.apply_transaction(self.values, self.config, Path('/mock/midscrolld'),
                                   runner or self.runner, health or (lambda _runner: None),
                                   require_trusted=False)

    def test_success_preserves_omitted_supported_settings(self):
        self.apply()
        values = settings.read_values(self.config)
        self.assertEqual(values['SPEED_MULT'], 0.02)
        self.assertEqual(values['IGNORE_DEVICES'], 'CORSAIR STRAFE')
        self.assertEqual(self.calls[0][1], '--check-config')
        self.assertEqual(len(self.calls), 2)

    def test_success_and_rollback_preserve_extended_attributes(self):
        try:
            os.setxattr(self.config, 'user.midscroll-test', b'original label')
        except OSError as exc:
            self.skipTest(f'filesystem does not support user xattrs: {exc}')
        self.apply()
        self.assertEqual(os.getxattr(self.config, 'user.midscroll-test'), b'original label')
        calls = []
        def fail_once(_runner):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('injected failure')
        with self.assertRaises(RuntimeError):
            self.apply(health=fail_once)
        self.assertEqual(os.getxattr(self.config, 'user.midscroll-test'), b'original label')

    def test_health_check_rejects_silent_pid_restarts(self):
        outputs = iter(('ActiveState=active\nSubState=running\nMainPID=20\nNRestarts=0\n',
                        'ActiveState=active\nSubState=running\nMainPID=21\nNRestarts=1\n'))
        with self.assertRaisesRegex(RuntimeError, 'restarted'):
            settings.health(lambda _argv: SimpleNamespace(stdout=next(outputs)), delay=0)

    def test_rust_validation_failure_does_not_write_or_restart(self):
        def reject(argv):
            self.calls.append(argv)
            raise subprocess.CalledProcessError(2, argv)
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(reject)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(len(self.calls), 1)

    def test_restart_failure_restores_exact_bytes_and_mode_and_restarts_old(self):
        def fail_once(argv):
            self.calls.append(argv)
            if len(self.calls) == 2:
                raise subprocess.CalledProcessError(1, argv)
            return SimpleNamespace(stdout='')
        with self.assertRaises(subprocess.CalledProcessError):
            self.apply(fail_once)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(len(self.calls), 3)

    def test_health_failure_rolls_back_and_can_retry(self):
        seen = []
        def fail_once(_runner):
            seen.append(1)
            if len(seen) == 1:
                raise RuntimeError('unhealthy daemon')
        with self.assertRaisesRegex(RuntimeError, 'unhealthy'):
            self.apply(health=fail_once)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.apply()
        self.assertEqual(settings.read_values(self.config)['SPEED_MULT'], 0.02)

    def test_interruption_immediately_after_atomic_replace_restores_original(self):
        original_write = settings.atomic_write
        calls = []
        def interrupted_write(*args, **kwargs):
            original_write(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise InterruptedError('interrupted after rename')
        with patch.object(settings, 'atomic_write', interrupted_write):
            with self.assertRaises(InterruptedError):
                self.apply()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(len(calls), 2)

    def test_nonfinite_unbounded_duplicate_and_injected_settings_rejected(self):
        for args in (['SPEED_EXP=1000'], ['SPEED_MULT=nan'], ['TICK_HZ=0'],
                     ['GHOST_SCALE=inf'], ['NATURAL=true', 'NATURAL=false'],
                     ['EXTRA_DEVICES=Mouse\nALLOW_KEYBOARDS=true'], ['BLACKLIST=foo'],
                     ['TOGGLE_MODE=false'], ['ALLOW_KEYBOARDS=true'],
                     ['EXTRA_DEVICES=/dev/input/'], ['EXTRA_DEVICES=/dev/input/./mouse']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                settings.parse_assignments(args)

    def test_legacy_controls_are_read_but_never_serialized(self):
        self.config.write_text('TOGGLE_MODE=false\nDESKTOP_SCROLL=true\nBLACKLIST=anything\n')
        rendered = settings.serialize(settings.read_values(self.config))
        for key in settings.LEGACY:
            self.assertNotIn(key + '=', rendered)

    def test_each_numeric_bound_matches_documented_limits(self):
        for key, (lower, upper, _default) in settings.NUMERIC.items():
            for value in (lower, upper):
                args = [f'{key}={value}']
                if key == 'MAX_DRAG_PX':
                    args.append('DEADZONE_PX=0')
                settings.parse_assignments(args)
            for value in (lower - max(lower, 1) / 2, upper + 1):
                with self.assertRaises(ValueError):
                    settings.parse_assignments([f'{key}={value}'])


class UninstallPreferences(unittest.TestCase):
    def test_only_known_preferences_and_unmodified_managed_script_are_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            base = home / '.local/share/midscroll'
            base.mkdir(parents=True)
            (base / 'user-settings-before.json').write_text(json.dumps([
                {'file': str(home / '.config/kwinrc'), 'group': 'Wayland',
                 'key': 'EnablePrimarySelection', 'value': '__absent__'},
                {'file': str(home / '.config/unrelated'), 'group': 'Other',
                 'key': 'Enabled', 'value': 'true'},
            ]))
            managed = home / '.config/plasma-workspace/env/90-midscroll-accessibility.sh'
            managed.parent.mkdir(parents=True)
            modified = '# Managed by the context-aware midscroll setup.\nexport MY_SETTING=yes\n'
            managed.write_text(modified)
            calls = []
            def run(argv, **_kwargs):
                calls.append(argv)
                return SimpleNamespace(stdout='false\n', returncode=0)
            with patch.object(Path, 'home', return_value=home), patch.object(subprocess, 'run', run):
                exec(compile(uninstall.RESTORE_USER, '<restore-test>', 'exec'), {})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1][-1], '--delete')
            self.assertEqual(managed.read_text(), modified)
            self.assertTrue(all('unrelated' not in ' '.join(argv) for argv in calls))


if __name__ == '__main__':
    unittest.main()
