#!/usr/bin/python3
"""Disposable GTK controls: records events before claiming all middle clicks."""
import argparse
import json
import os
from pathlib import Path
import time
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--mode', choices=('known', 'unknown'), required=True)
    args = parser.parse_args()
    status_path = args.directory / (args.mode + '-status.json')
    command_path = args.directory / (args.mode + '-command.json')
    status = {'pid': os.getpid(), 'mode': args.mode, 'ready': False, 'buttons': [], 'releases': [], 'gesture_events': [],
              'scroll': 0., 'maximum': 0., 'entry': 'unchanged', 'rectangles': {}, 'ack': 0}
    app = Gtk.Application(application_id='org.midscroll.IntegrationFixture' + args.token + args.mode)
    widgets = {}
    dirty = [True]
    def save():
        tmp = status_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(status))
        tmp.replace(status_path)
    def activate(app):
        window = Gtk.ApplicationWindow(application=app, title='Disposable autoscroll test ' + args.mode + ' ' + args.token)
        window.set_default_size(800, 650)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for method in ('set_margin_top', 'set_margin_bottom', 'set_margin_start', 'set_margin_end'):
            getattr(box, method)(12)
        box.append(Gtk.Label(label='Temporary autoscroll verification. Do not move or click the mouse during this test.'))
        notebook = Gtk.Notebook()
        page = Gtk.Label(label='These tabs belong only to the disposable verification window.')
        notebook.append_page(page, Gtk.Label(label='Disposable tab A'))
        notebook.append_page(Gtk.Label(label='Second test page.'), Gtk.Label(label='Disposable tab B'))
        widgets['tab'] = notebook.get_tab_label(page)
        box.append(notebook)
        link = Gtk.LinkButton(uri='https://example.invalid/', label='Disposable link (activation is intercepted)')
        link.connect('activate-link', lambda *unused: True)
        widgets['link'] = link
        box.append(link)
        entry = Gtk.Entry()
        entry.set_text('unchanged')
        widgets['entry'] = entry
        box.append(entry)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        text = Gtk.TextView()
        text.set_editable(False)
        text.set_cursor_visible(False)
        text.get_buffer().set_text('\n'.join('Disposable scrolling line %04d' % i for i in range(1000)))
        scroll.set_child(text)
        widgets['content'] = scroll
        box.append(scroll)
        adjustment = scroll.get_vadjustment()
        adjustment.connect('value-changed', lambda *unused: dirty.__setitem__(0, True))
        window.set_child(box)
        gesture = Gtk.GestureClick()
        gesture.set_button(0)
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        def target_at(x, y):
            targets = [name for name, rect in status['rectangles'].items()
                       if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]]
            return targets[0] if targets else 'other'
        def press(controller, count, x, y):
            button = int(controller.get_current_button())
            target = target_at(x, y)
            status['buttons'].append({'button': button, 'target': target})
            status['gesture_events'].append({'type': 'press', 'button': button, 'target': target})
            if button == 2:
                # Prevent any clipboard paste or URI activation if interception
                # fails, after recording the delivered press as a test failure.
                controller.set_state(Gtk.EventSequenceState.CLAIMED)
            dirty[0] = True
        def release(controller, count, x, y):
            button = int(controller.get_current_button())
            status['releases'].append({'button': button})
            status['gesture_events'].append({'type': 'release', 'button': button, 'target': target_at(x, y)})
            dirty[0] = True
        gesture.connect('pressed', press)
        gesture.connect('released', release)
        window.add_controller(gesture)
        window.present()
        def pulse():
            rectangles = {}
            for name, widget in widgets.items():
                ok, rect = widget.compute_bounds(window)
                if ok:
                    rectangles[name] = [rect.get_x(), rect.get_y(), rect.get_width(), rect.get_height()]
            status['rectangles'] = rectangles
            status['ready'] = len(rectangles) == 4 and rectangles['content'][3] > 100
            status['entry'] = entry.get_text()
            status['scroll'] = adjustment.get_value()
            status['maximum'] = max(0, adjustment.get_upper() - adjustment.get_page_size())
            try:
                command = json.loads(command_path.read_text())
                if command['id'] > status['ack']:
                    if command.get('quit'):
                        app.quit()
                        return False
                    if command.get('reset'):
                        adjustment.set_value(status['maximum'] / 2)
                    status['ack'] = command['id']
                    dirty[0] = True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            status['scroll'] = adjustment.get_value()
            # Bound output size; the runner expects far fewer than 200 clicks.
            if len(status['buttons']) > 1000:
                app.quit()
                return False
            if dirty[0] or not status_path.exists():
                save()
                dirty[0] = False
            return True
        GLib.timeout_add(25, pulse)
        GLib.timeout_add_seconds(240, lambda: (app.quit(), False)[1])
    app.connect('activate', activate)
    app.run([])

if __name__ == '__main__':
    main()
