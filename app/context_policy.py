"""Conservative, bounded AT-SPI target inspection. Run only in a disposable worker.

No title/class-name guessing authorizes a native click. A native result needs a
verified process, a unique matching window and a complete noneditable ancestry.
"""
import configparser
import os
from pathlib import Path
import time

NATIVE_ROLES = {'page tab', 'link'}
INERT_ROLES = {'page tab list', 'button', 'push button', 'toggle button',
               'check box', 'radio button', 'combo box', 'menu', 'menu item',
               'check menu item', 'radio menu item', 'menu bar', 'popup menu',
               'tool bar', 'slider', 'spin button', 'scroll bar', 'title bar'}
SCROLL_ROLES = {'scroll pane', 'document frame', 'document web', 'document text',
                'document spreadsheet', 'document presentation', 'terminal',
                'list', 'tree', 'tree table', 'table'}
SHELL_APPS = ('plasmashell', 'plasma-keyboard', 'krunner', 'kscreenlocker',
              'org.kde.polkit', 'polkit-kde', 'pinentry')


def decide_roles(roles, editable=False, multiline=False, action_names=()):
    if 'password text' in roles:
        return 'ignore', 'password-entry'
    if editable:
        return ('scroll', 'editable-content') if multiline else ('ignore', 'single-line-entry')
    for role in roles:
        if role in NATIVE_ROLES:
            return 'native', role
    if any(role in SCROLL_ROLES for role in roles):
        return 'scroll', 'scrollable-content'
    if any(role in INERT_ROLES for role in roles):
        return 'ignore', 'non-scrolling-control'
    return 'scroll', 'content-or-unknown-control'


def basic_policy(context, config=None):
    """Cheap policy, no calls into applications. None means inspect further."""
    if not context.get('found'):
        return 'ignore', 'no-window'
    app = (context.get('app', '') + ' ' + context.get('instance', '')).lower()
    if any(s in app for s in SHELL_APPS) or any(context.get(k) for k in ('desktop', 'dock', 'popup')):
        return 'ignore', 'desktop-or-popup'
    try:
        x, y = context['x'], context['y']
        cx, cy, cw, ch = context['client']
        if not (cw > 0 and ch > 0 and cx <= x < cx + cw and cy <= y < cy + ch):
            return 'ignore', 'window-decoration'
    except (KeyError, TypeError, ValueError):
        return 'ignore', 'invalid-window-geometry'
    overrides = (config or {}).get('app_modes', {})
    if isinstance(overrides, dict):
        for match, mode in overrides.items():
            if isinstance(match, str) and match and match.lower() in app and mode in ('scroll', 'ignore'):
                return mode, 'user-override'
    return None


def process_identity(pid, proc=Path('/proc')):
    """Read kernel process identity; no general parent-family equivalence."""
    try:
        pid = int(pid)
        if pid <= 1:
            return None
        root = proc / str(pid)
        status = dict(line.split(':', 1) for line in (root / 'status').read_text().splitlines() if ':' in line)
        uid = int(status['Uid'].split()[0])
        parent = int(status['PPid'].strip())
        stat = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        start = int(stat[19])  # field 22; fields after comm begin at field 3
        executable = str((root / 'exe').readlink())
        cgroup = (root / 'cgroup').read_text().strip()
        flatpak = None
        try:
            info = configparser.ConfigParser(interpolation=None)
            info.read_string((root / 'root/.flatpak-info').read_text())
            app = info.get('Application', 'name')
            instance = info.get('Instance', 'instance-id')
            scope = 'app-flatpak-' + app + '-' + instance + '.scope'
            if instance.isdigit() and cgroup.endswith('/' + scope):
                flatpak = (app, instance, cgroup)
        except (OSError, configparser.Error, ValueError):
            pass
        return {'pid': pid, 'uid': uid, 'parent': parent, 'start': start,
                'exe': executable, 'flatpak': flatpak}
    except (OSError, ValueError, KeyError, IndexError):
        return None


def verified_peer(target_pid, accessible_pid, reader=process_identity):
    target = reader(target_pid)
    peer = reader(accessible_pid)
    if not target or not peer or target['uid'] != peer['uid']:
        return False
    if target['pid'] == peer['pid']:
        return target['start'] == peer['start']
    # Flatpak applications and their AT-SPI D-Bus proxy have the SAME sandbox
    # instance and exact scope. A common launcher, executable name, application
    # ID or ordinary ancestor by itself is never evidence of shared identity.
    if not target['flatpak'] or target['flatpak'] != peer['flatpak']:
        return False
    if peer['exe'] != '/usr/bin/xdg-dbus-proxy':
        return False
    wrapper = reader(peer['parent'])
    return bool(wrapper and wrapper['exe'] == '/usr/bin/bwrap'
                and wrapper['uid'] == peer['uid'])


class InspectionExpired(Exception):
    pass


class InspectionUncertain(Exception):
    pass


class Inspector:
    def __init__(self, atspi=None, desktop=None, peer_check=verified_peer):
        if atspi is None:
            import gi
            gi.require_version('Atspi', '2.0')
            from gi.repository import Atspi
            atspi = Atspi
        self.api = atspi
        self.api.set_timeout(35, 60)
        self.desktop = desktop if desktop is not None else self.api.get_desktop(0)
        self.peer_check = peer_check

    def warm(self, deadline):
        """Prime application metadata outside the interactive request budget."""
        try:
            count = self.call(deadline, self.desktop.get_child_count)
            for index in range(min(max(0, count), 128)):
                try:
                    app = self.call(deadline, self.desktop.get_child_at_index, index)
                    self.call(deadline, app.get_process_id)
                    self.call(deadline, app.get_child_count)
                except InspectionExpired:
                    break
                except Exception:
                    continue
        except Exception:
            pass

    @staticmethod
    def call(deadline, method, *args):
        if time.monotonic() >= deadline:
            raise InspectionExpired()
        result = method(*args)
        if time.monotonic() >= deadline:
            raise InspectionExpired()
        return result

    def children(self, node, deadline, maximum):
        count = self.call(deadline, node.get_child_count)
        if not 0 <= count <= maximum:
            raise InspectionUncertain('too-many-' + self.call(deadline, node.get_role_name) + '-children')
        for index in range(count):
            yield self.call(deadline, node.get_child_at_index, index)

    def frame(self, context, deadline):
        """No best-score heuristic: tied titles remain ambiguous even if active."""
        caption = context.get('caption')
        if context.get('window_matches') != 1:
            raise InspectionUncertain('ambiguous-compositor-window')
        if not caption or not context.get('pid'):
            raise InspectionUncertain('window-identity-unavailable')
        frames = []
        for app in self.children(self.desktop, deadline, 128):
            try:
                apid = self.call(deadline, app.get_process_id)
            except InspectionExpired:
                raise
            except Exception:
                # Registry peers may outlive their AT-SPI socket. They cannot
                # establish identity; never substitute their names or classes.
                continue
            if not self.call(deadline, self.peer_check, context['pid'], apid):
                continue
            for frame in self.children(app, deadline, 128):
                state = self.call(deadline, frame.get_state_set)
                if not state.contains(self.api.StateType.SHOWING):
                    continue
                if self.call(deadline, frame.get_role_name) not in ('frame', 'dialog', 'window'):
                    continue
                if self.call(deadline, frame.get_name) == caption:
                    frames.append(frame)
        if len(frames) != 1:
            raise InspectionUncertain('ambiguous-window' if frames else 'unidentified-window')
        frame = frames[0]
        component = self.call(deadline, frame.get_component_iface)
        if not component:
            raise InspectionUncertain('no-window-component')
        extent = self.call(deadline, component.get_extents, self.api.CoordType.WINDOW)
        _, _, width, height = context['client']
        # WINDOW coordinates avoid fabricated SCREEN coordinates on Wayland.
        # Size agreement is additional evidence, never a title tie breaker.
        if abs(extent.width - width) > 2 or abs(extent.height - height) > 2:
            raise InspectionUncertain('window-size-mismatch')
        return frame, extent

    @staticmethod
    def contains(rect, x, y):
        return rect.width > 0 and rect.height > 0 and rect.x <= x < rect.x + rect.width and rect.y <= y < rect.y + rect.height

    def inspect(self, context, deadline):
        frame, extent = self.frame(context, deadline)
        cx, cy, _, _ = context['client']
        x, y = context['x'] - cx + extent.x, context['y'] - cy + extent.y
        if not self.contains(extent, x, y):
            raise InspectionUncertain('pointer-outside-frame')
        node = frame
        seen = []
        for _ in range(32):
            if node in seen:
                raise InspectionUncertain('cyclic-target-tree')
            seen.append(node)
            component = self.call(deadline, node.get_component_iface)
            direct = self.call(deadline, component.get_accessible_at_point, x, y, self.api.CoordType.WINDOW) if component else None
            if direct is not None and direct != node:
                target_component = self.call(deadline, direct.get_component_iface)
                state = self.call(deadline, direct.get_state_set)
                if (not target_component or not state.contains(self.api.StateType.SHOWING)
                        or not self.contains(self.call(deadline, target_component.get_extents, self.api.CoordType.WINDOW), x, y)):
                    raise InspectionUncertain('invalid-hit-test')
                node = direct
                continue
            hits = []
            for child in self.children(node, deadline, 128):
                state = self.call(deadline, child.get_state_set)
                if not state.contains(self.api.StateType.SHOWING):
                    continue
                component = self.call(deadline, child.get_component_iface)
                if component and self.contains(self.call(deadline, component.get_extents, self.api.CoordType.WINDOW), x, y):
                    hits.append(child)
            if len(hits) > 1:
                raise InspectionUncertain('overlapping-hit-targets')
            if not hits:
                break
            node = hits[0]
        else:
            raise InspectionUncertain('target-too-deep')
        roles = []
        editable = multiline = False
        seen = []
        for _ in range(48):
            if node is None or node in seen:
                raise InspectionUncertain('incomplete-target-ancestry')
            seen.append(node)
            roles.append(self.call(deadline, node.get_role_name))
            states = self.call(deadline, node.get_state_set)
            editable |= states.contains(self.api.StateType.EDITABLE)
            multiline |= states.contains(self.api.StateType.MULTI_LINE)
            if node == frame:
                return decide_roles(roles, editable, multiline)
            node = self.call(deadline, node.get_parent)
        raise InspectionUncertain('ancestry-too-deep')

    def classify(self, context, config=None, deadline=None):
        simple = basic_policy(context, config)
        if simple:
            return simple
        deadline = time.monotonic() + .12 if deadline is None else deadline
        try:
            return self.inspect(context, deadline)
        except InspectionExpired:
            return 'scroll', 'inspection-timeout'
        except InspectionUncertain as exc:
            return 'scroll', str(exc)
        except Exception:
            return 'scroll', 'accessibility-unavailable'
