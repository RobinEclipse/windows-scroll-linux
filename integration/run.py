#!/usr/bin/python3
"""Live v2 integration checks; explicitly run only after installing the candidate.

Owns both GUI windows and synthetic mice; never clicks an existing user tab/link.
Requires an interactive KDE Wayland session and one pkexec authentication prompt.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import dbus
import gi
from gi.repository import GLib
try:
    gi.require_version('GLibUnix', '2.0')
    from gi.repository import GLibUnix
    add_unix_signal = GLibUnix.signal_add
except (ImportError, ValueError):
    add_unix_signal = GLib.unix_signal_add
from evdev import ecodes as e
from kwin_bridge import KWinBridge

ROOT = Path(__file__).resolve().parent
SYN = [e.EV_SYN, e.SYN_REPORT, 0]
BUTTONS = (e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE, e.BTN_SIDE, e.BTN_EXTRA)
DAEMON_STATUS = Path('/run/midscroll/status.json')


def wait_until(check, timeout=4, message='condition did not become true', cancelled=lambda: False):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if cancelled():
            raise InterruptedError('integration interrupted; restoring desktop')
        try:
            last = check()
            if last:
                return last
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(.035)
    raise AssertionError(message + (': ' + str(last) if last else ''))


class Runner:
    def __init__(self, output, bridge, directory, selection="all"):
        self.output, self.bridge, self.directory = output, bridge, directory
        self.selection = selection
        self.current_case = None
        self.results = {'format': 1, 'test': 'autoscroll-v2-disposable-integration', 'cases': [], 'status': 'running', 'selection': selection}
        self.fixture_processes = {}
        self.windows = {}
        self.commands = {'known': 0, 'unknown': 0}
        self.injector = None
        self.connection = None
        self.stream = None
        self.saved_pointer = None
        self.saved_focus = None
        self.saved_enabled = None
        self.abort = threading.Event()
        self.cleaning = False
        self.client = dbus.Interface(bridge.bus.get_object('org.midscroll.Control', '/org/midscroll/Control'), 'org.midscroll.Control')
        self.save()

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(self.output.suffix + '.tmp')
        temporary.write_text(json.dumps(self.results, indent=2) + '\n')
        temporary.replace(self.output)

    def check_abort(self):
        if not self.cleaning and self.abort.is_set():
            raise InterruptedError('integration interrupted; restoring desktop')

    def wait(self, check, **kwargs):
        return wait_until(check, cancelled=lambda: not self.cleaning and self.abort.is_set(), **kwargs)

    def control(self):
        self.check_abort()
        return json.loads(str(self.client.Status(timeout=.5)))

    def enabled(self, value):
        self.client.SetEnabled(dbus.Boolean(value), timeout=.5)
        self.wait(lambda: self.control()['enabled'] == value, message='control state not updated')
        time.sleep(.07)

    def request(self, operation, mouse=0, **fields):
        self.check_abort()
        self.connection.sendall(json.dumps({'op': operation, 'mouse': mouse, **fields}).encode() + b'\n')
        line = self.stream.readline(4097)
        if not line or len(line) > 4096:
            raise OSError('injector disconnected or malformed reply')
        value = json.loads(line)
        if value.get('ok') is not True:
            raise OSError('injector rejected operation')
        return value

    def events(self, events, mouse=0):
        self.request('events', mouse=mouse, events=events)

    def click(self, code, mouse=0):
        self.events([[e.EV_KEY, code, 1], SYN], mouse)
        time.sleep(.025)
        self.events([[e.EV_KEY, code, 0], SYN], mouse)

    def move(self, x, y):
        for _ in range(50):
            pointer = self.bridge.snapshot()
            dx, dy = x - pointer['x'], y - pointer['y']
            if abs(dx) <= 2 and abs(dy) <= 2:
                return
            events = []
            for code, delta in ((e.REL_X, dx), (e.REL_Y, dy)):
                if abs(delta) > 1:
                    amount = max(-400, min(400, int(delta * .45)))
                    events.append([e.EV_REL, code, amount or (1 if delta > 0 else -1)])
            self.events(events + [SYN])
            time.sleep(.025)
        raise AssertionError('pointer positioning failed; a capture may still be active')

    def read(self, mode):
        self.check_abort()
        return json.loads((self.directory / (mode + '-status.json')).read_text())

    def fixture_command(self, mode, **fields):
        self.commands[mode] += 1
        sequence = self.commands[mode]
        path = self.directory / (mode + '-command.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'id': sequence, **fields}))
        temp.replace(path)
        if not fields.get('quit'):
            self.wait(lambda: self.read(mode)['ack'] == sequence, message='fixture command did not complete')
            time.sleep(.06)

    def aim(self, mode, target):
        self.bridge.activate(self.windows[mode])
        time.sleep(.055)
        context = self.bridge.snapshot(self.fixture_processes[mode].pid)
        assert context.get('window', {}).get('id') == self.windows[mode], 'fixture window identity changed'
        client = context['window']['client']
        rect = self.read(mode)['rectangles'][target]
        x, y = client[0] + rect[0] + rect[2] / 2, client[1] + rect[1] + rect[3] / 2
        self.move(x, y)
        time.sleep(.055)  # compositor has committed motion; native quiet-period guard
        self.guard(mode)

    def guard(self, mode):
        context = self.bridge.snapshot(self.fixture_processes[mode].pid)
        assert context.get('hover') == self.windows[mode], 'pointer left disposable fixture; refusing to click'
        assert context.get('active') == self.windows[mode], 'fixture lost focus; refusing to click'

    def no_middle(self, mode, before):
        data = self.read(mode)
        assert data['entry'] == 'unchanged', 'fixture input contents changed'
        assert not any(event['button'] == 2 for event in data['buttons'][before:]), 'unexpected middle event reached fixture'
        return data

    def stable(self, mode):
        time.sleep(.4)
        value = self.read(mode)['scroll']
        time.sleep(.3)
        assert abs(self.read(mode)['scroll'] - value) < 2, 'autoscroll continued after cancellation'

    def begin_scroll(self, mode='unknown'):
        self.fixture_command(mode, reset=True)
        self.aim(mode, 'content')
        before = self.read(mode)
        self.click(e.BTN_MIDDLE)
        time.sleep(.28)
        self.events([[e.EV_REL, e.REL_Y, 100], SYN])
        time.sleep(.30)
        after = self.no_middle(mode, len(before['buttons']))
        assert abs(after['scroll'] - before['scroll']) > 4, 'quick click did not latch autoscroll'
        # The actual pointer must still be over the fixture while the hand offset changes.
        self.guard(mode)
        return before, after

    def case(self, name, function):
        if self.selection == 'native' and not name.startswith('native-'):
            return
        if self.selection == 'hotplug' and not name.startswith('hotplug-'):
            return
        self.check_abort()
        self.current_case = name
        start = time.monotonic()
        try:
            details = function() or {}
        except BaseException as error:
            self.results['cases'].append({'name': name, 'status': 'fail', 'seconds': round(time.monotonic() - start, 3), 'error': str(error)})
            raise
        self.results['cases'].append({'name': name, 'status': 'pass', 'seconds': round(time.monotonic() - start, 3), **details})
        self.save()
        print('PASS ' + name, flush=True)

    def test_stop(self, stop, mouse):
        before, scrolling = self.begin_scroll()
        count = len(scrolling['buttons'])
        self.guard('unknown')
        self.click(stop, mouse)
        self.stable('unknown')
        data = self.no_middle('unknown', len(before['buttons']))
        assert len(data['buttons']) == count, 'stopping click reached the application'
        self.guard('unknown')
        self.click(e.BTN_LEFT, mouse)
        time.sleep(.08)
        delivered = self.read('unknown')['buttons'][count:]
        assert len(delivered) == 1 and delivered[0]['button'] == 1, 'follow-up left click was swallowed or duplicated'
        return {'source': mouse, 'button': stop, 'followup_left_delivered': True}

    def test_wheel(self, mouse):
        before, _ = self.begin_scroll()
        self.guard('unknown')
        self.events([[e.EV_REL, e.REL_WHEEL, -1], SYN], mouse)
        self.stable('unknown')
        self.no_middle('unknown', len(before['buttons']))
        return {'source': mouse}

    def test_pause(self):
        self.enabled(False)
        self.aim('unknown', 'entry')
        before = self.read('unknown')
        self.click(e.BTN_MIDDLE)
        time.sleep(.3)
        self.no_middle('unknown', len(before['buttons']))
        self.enabled(True)
        return {'middle_events_delivered': 0}

    def test_pending_cancel(self):
        self.fixture_command('unknown', reset=True)
        self.aim('unknown', 'content')
        before = self.read('unknown')
        # One batch starts and cancels before another poll can authorize a stale result.
        self.events([[e.EV_KEY, e.BTN_MIDDLE, 1], SYN, [e.EV_KEY, e.BTN_MIDDLE, 0], SYN,
                     [e.EV_KEY, e.BTN_RIGHT, 1], SYN, [e.EV_KEY, e.BTN_RIGHT, 0], SYN])
        time.sleep(.35)
        self.events([[e.EV_REL, e.REL_Y, 40], SYN])
        time.sleep(.25)
        data = self.no_middle('unknown', len(before['buttons']))
        assert len(data['buttons']) == len(before['buttons']), 'pending cancellation click leaked'
        assert abs(data['scroll'] - before['scroll']) < 2, 'cancelled pending request resurrected'
        self.guard('unknown')
        return {'resurrected': False}

    def test_pause_during_request(self):
        self.fixture_command('unknown', reset=True)
        self.aim('unknown', 'content')
        before = self.read('unknown')
        self.events([[e.EV_KEY, e.BTN_MIDDLE, 1], SYN, [e.EV_KEY, e.BTN_MIDDLE, 0], SYN])
        self.enabled(False)
        time.sleep(.28)
        self.events([[e.EV_REL, e.REL_Y, 40], SYN])
        time.sleep(.25)
        self.no_middle('unknown', len(before['buttons']))
        assert abs(self.read('unknown')['scroll'] - before['scroll']) < 2, 'request reactivated after pause'
        self.enabled(True)
        return {'resurrected': False, 'note': 'Live timing is nondeterministic; deterministic in-flight race is also covered by unit tests.'}

    def test_unknown_entry(self):
        self.aim('unknown', 'entry')
        before = self.read('unknown')
        self.click(e.BTN_MIDDLE)
        time.sleep(.25)
        self.guard('unknown')
        self.click(e.BTN_RIGHT)
        time.sleep(.08)
        data = self.no_middle('unknown', len(before['buttons']))
        assert len(data['buttons']) == len(before['buttons']), 'unknown entry or stop click received mouse button'

    def test_known_entry(self):
        self.aim('known', 'entry')
        before = self.read('known')
        self.click(e.BTN_MIDDLE)
        time.sleep(.28)
        self.no_middle('known', len(before['buttons']))
        diagnostic = self.control()['last']
        assert diagnostic['mode'] == 'ignore', 'recognized entry did not suppress middle click'
        return {'classification': diagnostic}

    def test_native(self, target):
        # Positive observer control: ordinary left down/up must reach the GTK observer before
        # a native-middle assertion is allowed to claim missing transport.
        self.aim('known', 'content')
        observer_before = self.read('known')
        self.click(e.BTN_LEFT)
        time.sleep(.08)
        observer = self.read('known')['gesture_events'][len(observer_before['gesture_events']):]
        assert [(event['type'], event['button']) for event in observer] == [('press', 1), ('release', 1)], 'GTK gesture observer positive control failed'
        self.aim('known', target)
        before = self.read('known')
        self.click(e.BTN_MIDDLE)
        time.sleep(.28)
        data = self.read('known')
        delivered = data['buttons'][len(before['buttons']):]
        releases = data['releases'][len(before['releases']):]
        diagnostic = self.control()['last']
        events = data['gesture_events'][len(before['gesture_events']):]
        self.results['latest_native_evidence'] = {'target': target, 'classification': diagnostic, 'presses': delivered, 'releases': releases, 'gesture_events': events}
        self.save()
        assert len(releases) == 1 and releases[0]['button'] == 2, 'native middle release was missing or duplicated'
        assert len(delivered) == 1 and delivered[0] == {'button': 2, 'target': target}, 'native action was not delivered to the owned control'
        assert diagnostic['mode'] == 'native', 'native target was not positively classified'
        return {'classification': diagnostic, 'native_balanced_pair_delivered': True, 'action_claimed_by_fixture': True, 'gesture_events': events}

    def test_hotplug(self):
        self.enabled(False)
        old = self.request('create', mouse=0)
        # Root daemon is tracking devices independently of the user helper.
        time.sleep(.7)
        self.request('close', mouse=0)
        replacement = self.request('create', mouse=0)
        time.sleep(1.0)
        self.enabled(True)
        self.test_stop(e.BTN_RIGHT, 0)
        return {'device_name': replacement['name'], 'old_path': old['path'], 'replacement_path': replacement['path'],
                'path_reused': old['path'] == replacement['path']}

    def launch(self):
        info = self.control()
        daemon = json.loads(DAEMON_STATUS.read_text())
        assert info.get('protocol') == 2 and daemon.get('backend') == 'rust', 'v2 must be installed and running before live tests'
        assert info['connected'] and not info.get('locked'), 'session helper is unavailable or locked'
        self.results['initial_daemon'] = daemon
        self.saved_enabled = info['enabled']
        previous = self.bridge.snapshot()
        self.saved_pointer = (previous['x'], previous['y'])
        self.saved_focus = previous['active']
        self.results['initial_desktop'] = {**previous, 'enabled': self.saved_enabled}
        self.save()
        token = uuid.uuid4().hex[:12]
        for mode in ('known', 'unknown'):
            environment = dict(os.environ, GDK_BACKEND='wayland')
            if mode == 'unknown':
                environment['GTK_A11Y'] = 'none'
                environment['NO_AT_BRIDGE'] = '1'
            else:
                environment.pop('GTK_A11Y', None)
                environment.pop('NO_AT_BRIDGE', None)
                environment['ACCESSIBILITY_ENABLED'] = '1'
            log = open(self.directory / (mode + '.log'), 'w')
            process = subprocess.Popen([sys.executable, str(ROOT / 'fixture.py'), '--directory', str(self.directory), '--token', token, '--mode', mode], env=environment, stdout=log, stderr=subprocess.STDOUT)
            log.close()
            self.fixture_processes[mode] = process
            self.wait(lambda: self.read(mode).get('ready'), timeout=8, message=mode + ' fixture did not initialize')
            window = self.wait(lambda: self.bridge.snapshot(process.pid).get('window'), message='fixture not found in KWin')
            self.windows[mode] = window['id']
        self.injector = subprocess.Popen(['pkexec', '/usr/bin/python3', '-I', str(ROOT / 'injector.py'), '--socket', str(self.directory / 'inject.sock')])
        self.wait(lambda: (self.directory / 'inject.sock').exists(), timeout=45, message='injector was not authorized or failed to start')
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.settimeout(3)
        self.connection.connect(str(self.directory / 'inject.sock'))
        self.stream = self.connection.makefile('rb')
        self.request('create', mouse=0)
        self.request('create', mouse=1)
        self.wait(lambda: json.loads(DAEMON_STATUS.read_text()).get('attached_mice', 0) >= daemon['attached_mice'] + 2, timeout=4, message='daemon did not attach both disposable mice')
        self.enabled(True)
        # Give AT-SPI registration a chance to settle after both fixture startups.
        time.sleep(.3)

    def run(self):
        try:
            self.launch()
            for mouse in (0, 1):
                for button in BUTTONS:
                    self.case('click-latch-stop-button-%d-source-%d' % (button, mouse), lambda b=button, m=mouse: self.test_stop(b, m))
                self.case('physical-wheel-cancels-source-%d' % mouse, lambda m=mouse: self.test_wheel(m))
            self.case('pause-blocks-middle', self.test_pause)
            self.case('pending-click-cancellation', self.test_pending_cancel)
            self.case('pause-during-request-no-resurrection', self.test_pause_during_request)
            self.case('unknown-entry-no-middle-or-paste', self.test_unknown_entry)
            self.case('known-entry-no-middle-or-paste', self.test_known_entry)
            self.case('native-disposable-tab', lambda: self.test_native('tab'))
            self.case('native-disposable-link', lambda: self.test_native('link'))
            self.case('hotplug-same-name-reconnect', self.test_hotplug)
            self.results['status'] = 'pass'
        except BaseException as error:
            self.results['status'] = 'fail'
            self.results['error'] = type(error).__name__ + ': ' + str(error)
            failure = {'case': self.current_case, 'fixtures': {}}
            for mode in self.fixture_processes:
                try:
                    failure['fixtures'][mode] = json.loads((self.directory / (mode + '-status.json')).read_text())
                except (OSError, ValueError):
                    pass
            try:
                failure['control'] = json.loads(str(self.client.Status(timeout=.5)))
            except Exception:
                pass
            self.results['failure_evidence'] = failure
            print(traceback.format_exc(), file=sys.stderr)
        finally:
            self.cleanup()
            self.save()
        return self.results['status'] == 'pass'

    def cleanup(self):
        self.cleaning = True
        errors, steps = [], []
        def step(name, operation):
            started = time.monotonic()
            try:
                value = operation()
                steps.append({'name': name, 'status': 'pass', 'seconds': round(time.monotonic() - started, 3)})
                return value
            except Exception as error:
                description = type(error).__name__ + ': ' + str(error)
                steps.append({'name': name, 'status': 'fail', 'error': description})
                errors.append(name + ': ' + description)
                return None
        # These operations deliberately remain independent. In particular,
        # a compositor timeout must NEVER skip restoring the enabled setting.
        if self.saved_enabled is not None:
            step('pause-capture-for-cleanup', lambda: self.enabled(False))
        if self.connection:
            for mouse in (0, 1):
                step('release-buttons-source-' + str(mouse), lambda m=mouse: self.request('release_all', mouse=m))
            time.sleep(.05)
            if self.saved_pointer:
                step('restore-pointer', lambda: self.move(*self.saved_pointer))
            # No injection is needed after restoring the pointer. Close the
            # privileged source before potentially slow window-manager retries.
            try:
                self.request('quit')
            except (OSError, ValueError):
                pass
        if self.stream:
            step('close-injector-stream', self.stream.close)
        if self.connection:
            step('close-injector-socket', self.connection.close)
        for mode, process in self.fixture_processes.items():
            def close_fixture(p=process):
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(timeout=2)
            step('close-fixture-' + mode, close_fixture)
        if self.injector:
            step('wait-for-injector-exit', lambda: self.injector.wait(timeout=2))
        if self.saved_focus:
            def restore_focus():
                result = self.bridge.activate(self.saved_focus)
                if not result.get('found'):
                    raise RuntimeError('original window no longer exists')
                time.sleep(.06)
                if self.bridge.snapshot().get('active') != self.saved_focus:
                    raise RuntimeError('original window did not become active')
            step('restore-focus', restore_focus)
        if self.saved_enabled is not None:
            def restore_enabled():
                for attempt in range(3):
                    try:
                        self.enabled(self.saved_enabled)
                        return
                    except (OSError, AssertionError, dbus.DBusException):
                        if attempt == 2:
                            raise
                        time.sleep(.05 * (attempt + 1))
            step('restore-enabled-setting', restore_enabled)
        final = {}
        desktop = step('verify-final-pointer-and-focus', self.bridge.snapshot)
        if desktop:
            final.update(desktop)
            if self.saved_pointer and any(abs(desktop[axis] - coordinate) > 2 for axis, coordinate in zip(('x', 'y'), self.saved_pointer)):
                errors.append('final pointer position differs from the saved position')
            if self.saved_focus and desktop.get('active') != self.saved_focus:
                errors.append('final active window differs from the saved window')
        control = step('verify-final-enabled-setting', self.control)
        if control:
            final['enabled'] = control.get('enabled')
            final['helper_connected'] = control.get('connected')
            if self.saved_enabled is not None and control.get('enabled') != self.saved_enabled:
                errors.append('final enabled setting differs from the saved setting')
        self.results['cleanup_steps'] = steps
        self.results['restored_desktop'] = final
        if errors:
            self.results['cleanup_errors'] = errors
            self.results['status'] = 'fail'
        self.results['restored'] = not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--only', choices=('all', 'native', 'hotplug'), default='all')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit('Run as the desktop user; the mouse injector requests pkexec separately.')
    if os.environ.get('XDG_SESSION_TYPE') != 'wayland':
        raise SystemExit('This live test requires the desktop KDE Wayland session environment.')
    runtime = Path('/run/user') / str(os.getuid())
    directory = Path(tempfile.mkdtemp(prefix='autoscroll-v2-integration-', dir=runtime))
    directory.chmod(0o700)
    loop = GLib.MainLoop()
    bridge = KWinBridge(directory)
    runner = Runner(args.output.resolve(), bridge, directory, args.only)
    result = []
    def test():
        try: result.append(runner.run())
        finally: GLib.idle_add(loop.quit)
    # Keep GLib running during cancellation so restoration can query KWin.
    # The test thread notices the flag and enters its single cleanup path.
    def interrupt():
        runner.abort.set()
        return True
    add_unix_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, interrupt)
    add_unix_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, interrupt)
    threading.Thread(target=test, daemon=True).start()
    try:
        loop.run()
    finally:
        if not result or not result[0]:
            for log in directory.glob('*.log'):
                shutil.copyfile(log, args.output.with_name(args.output.stem + '-' + log.name))
        shutil.rmtree(directory, ignore_errors=True)
    raise SystemExit(0 if result and result[0] else 1)

if __name__ == '__main__':
    main()
