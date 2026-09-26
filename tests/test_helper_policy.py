import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from context_policy import Inspector, decide_roles, verified_peer, basic_policy

class States:
    def __init__(self, *states): self.states = set(states)
    def contains(self, state): return state in self.states

class API:
    StateType = SimpleNamespace(SHOWING='showing', ACTIVE='active', EDITABLE='editable', MULTI_LINE='multiline')
    CoordType = SimpleNamespace(WINDOW='window')
    @staticmethod
    def set_timeout(*args): pass

class Node:
    def __init__(self, role, name='', pid=123, children=(), states=('showing',), width=800, height=600):
        self.role, self.name, self.pid = role, name, pid
        self.children = list(children)
        self.states = States(*states)
        self.parent = None
        self.width, self.height = width, height
        for child in children: child.parent = self
    def get_role_name(self): return self.role
    def get_name(self): return self.name
    def get_process_id(self): return self.pid
    def get_child_count(self): return len(self.children)
    def get_child_at_index(self, index): return self.children[index]
    def get_state_set(self): return self.states
    def get_component_iface(self): return self
    def get_extents(self, coords): return SimpleNamespace(x=0, y=0, width=self.width, height=self.height)
    def get_accessible_at_point(self, *args): return self.children[0] if self.children else self
    def get_parent(self): return self.parent

CONTEXT = {'found': True, 'app': 'testapp', 'instance': 'testapp', 'pid': 123,
           'caption': 'Document', 'window_matches': 1, 'id': 'window2', 'active': 'window2',
           'x': 100, 'y': 100, 'client': [0, 0, 800, 600], 'frame': [0, 0, 800, 600]}

def inspector(frames, apid=123, peer=lambda a, b: a == b):
    app = Node('application', 'testapp', pid=apid, children=frames)
    return Inspector(API, Node('desktop', children=[app]), peer)

class Policies(unittest.TestCase):
    def test_single_unique_verified_link_is_native(self):
        self.assertEqual(inspector([Node('frame', 'Document', children=[Node('link')])]).classify(CONTEXT), ('native', 'link'))
    def test_tab_text_is_native(self):
        self.assertEqual(decide_roles(['text', 'page tab'])[0], 'native')
    def test_editable_tab_text_cannot_pass(self):
        self.assertEqual(decide_roles(['text', 'page tab'], editable=True)[0], 'ignore')
    def test_editable_link_cannot_pass(self):
        self.assertEqual(decide_roles(['text', 'link'], editable=True)[0], 'ignore')
    def test_generic_actionable_file_does_not_authorize_native(self):
        self.assertEqual(decide_roles(['icon', 'scroll pane'], action_names=['open'])[0], 'scroll')
    def test_single_line_entry_ignored(self):
        self.assertEqual(decide_roles(['text'], editable=True)[0], 'ignore')
    def test_multiline_entry_scrolls(self):
        self.assertEqual(decide_roles(['text'], editable=True, multiline=True)[0], 'scroll')
    def test_password_never_passes(self):
        self.assertEqual(decide_roles(['password text', 'link'], editable=True, multiline=True)[0], 'ignore')
    def test_missing_accessibility_window_cannot_hide_compositor_ambiguity(self):
        instance = inspector([Node('frame', 'Document', children=[Node('link')])])
        self.assertEqual(instance.classify(dict(CONTEXT, window_matches=2)), ('scroll', 'ambiguous-compositor-window'))
    def test_missing_compositor_uniqueness_cannot_authorize_native(self):
        instance = inspector([Node('frame', 'Document', children=[Node('link')])])
        context = dict(CONTEXT)
        context.pop('window_matches')
        self.assertEqual(instance.classify(context)[0], 'scroll')
    def test_stale_unrelated_registry_peer_cannot_substitute_identity(self):
        instance = inspector([Node('frame', 'Document', children=[Node('link')])])
        stale = Node('application', 'testapp', pid=456)
        def disconnected(): raise OSError('old AT-SPI socket disappeared')
        stale.get_process_id = disconnected
        instance.desktop.children.insert(0, stale)
        self.assertEqual(instance.classify(CONTEXT), ('native', 'link'))
    def test_duplicate_titles_cannot_select_first_frame(self):
        a = Node('frame', 'Document', children=[Node('link')])
        b = Node('frame', 'Document', children=[Node('text', states=('showing', 'editable'))])
        self.assertEqual(inspector([a, b]).classify(CONTEXT), ('scroll', 'ambiguous-window'))
    def test_duplicate_titles_cannot_use_active_or_size_tiebreaker(self):
        a = Node('frame', 'Document', children=[Node('link')], states=('showing', 'active'))
        b = Node('frame', 'Document', children=[Node('text')], width=799)
        self.assertEqual(inspector([a, b]).classify(CONTEXT)[1], 'ambiguous-window')
    def test_shared_app_name_never_matches_unrelated_pid(self):
        self.assertEqual(inspector([Node('frame', 'Document', children=[Node('link')])], apid=456).classify(CONTEXT)[0], 'scroll')
    def test_wrong_title_cannot_pass(self):
        self.assertEqual(inspector([Node('frame', 'Other title', children=[Node('link')])]).classify(CONTEXT)[0], 'scroll')
    def test_size_mismatch_cannot_pass(self):
        self.assertEqual(inspector([Node('frame', 'Document', children=[Node('link')], width=600)]).classify(CONTEXT)[1], 'window-size-mismatch')
    def test_truncated_ancestry_never_authorizes_native(self):
        link = Node('link')
        frame = Node('frame', 'Document', children=[link])
        link.parent = None
        self.assertEqual(inspector([frame]).classify(CONTEXT)[1], 'incomplete-target-ancestry')
    def test_hidden_target_never_authorizes_native(self):
        self.assertEqual(inspector([Node('frame', 'Document', children=[Node('link', states=())])]).classify(CONTEXT)[1], 'invalid-hit-test')
    def test_editable_ancestor_found_after_link_suppresses(self):
        frame = Node('frame', 'Document', children=[Node('panel', children=[Node('link')], states=('showing', 'editable'))])
        self.assertEqual(inspector([frame]).classify(CONTEXT)[0], 'ignore')
    def test_frame_enumeration_deadline_includes_every_call(self):
        instance = inspector([Node('frame', 'Document', children=[Node('link')]) for _ in range(40)])
        app = instance.desktop.children[0]
        original = app.get_child_at_index
        app.get_child_at_index = lambda i: (time.sleep(.02), original(i))[1]
        start = time.monotonic()
        result = instance.classify(CONTEXT, deadline=start + .055)
        self.assertEqual(result[1], 'inspection-timeout')
        self.assertLess(time.monotonic() - start, .13)
    def test_too_many_frames_cannot_allow_native_from_partial_scan(self):
        instance = inspector([Node('frame', 'Document', children=[Node('link')]) for _ in range(129)])
        self.assertEqual(instance.classify(CONTEXT)[0], 'scroll')
    def test_unknown_window_scrolls(self):
        self.assertEqual(inspector([]).classify(CONTEXT)[0], 'scroll')
    def test_shell_popup_and_decorations_do_not_paste(self):
        for context in (dict(CONTEXT, dock=True), dict(CONTEXT, popup=True), dict(CONTEXT, x=-1), {'found': False}):
            self.assertEqual(basic_policy(context)[0], 'ignore')
    def test_native_override_is_not_accepted(self):
        self.assertIsNone(basic_policy(CONTEXT, {'app_modes': {'testapp': 'native'}}))

class ProcessIdentity(unittest.TestCase):
    def ident(self, pid, parent=10, exe='/usr/bin/app', flatpak=None, start=1):
        return dict(pid=pid, uid=1000, parent=parent, exe=exe, flatpak=flatpak, start=start)
    def test_ordinary_siblings_never_match(self):
        procs = {11: self.ident(11), 12: self.ident(12), 10: self.ident(10)}
        self.assertFalse(verified_peer(11, 12, procs.get))
    def test_exact_pid_matches(self):
        procs = {11: self.ident(11)}
        self.assertTrue(verified_peer(11, 11, procs.get))
    def test_verified_flatpak_proxy_matches(self):
        sandbox = ('org.example.App', '1234', '/app-flatpak-org.example.App-1234.scope')
        procs = {11: self.ident(11, flatpak=sandbox), 12: self.ident(12, parent=13, exe='/usr/bin/xdg-dbus-proxy', flatpak=sandbox), 13: self.ident(13, exe='/usr/bin/bwrap')}
        self.assertTrue(verified_peer(11, 12, procs.get))
    def test_other_flatpak_instance_does_not_match(self):
        procs = {11: self.ident(11, flatpak=('app', '1', 'scope1')), 12: self.ident(12, parent=13, exe='/usr/bin/xdg-dbus-proxy', flatpak=('app', '2', 'scope2')), 13: self.ident(13, exe='/usr/bin/bwrap')}
        self.assertFalse(verified_peer(11, 12, procs.get))
    def test_flatpak_peer_requires_proxy_executable_and_wrapper(self):
        sandbox = ('app', '1', 'scope1')
        procs = {11: self.ident(11, flatpak=sandbox), 12: self.ident(12, flatpak=sandbox), 10: self.ident(10)}
        self.assertFalse(verified_peer(11, 12, procs.get))
        procs[12]['exe'] = '/usr/bin/xdg-dbus-proxy'
        self.assertFalse(verified_peer(11, 12, procs.get))

if __name__ == '__main__': unittest.main()
