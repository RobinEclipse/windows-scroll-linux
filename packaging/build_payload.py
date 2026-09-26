#!/usr/bin/python3
"""Build a verified updater directory after cargo build --release.

Usage: python3 packaging/build_payload.py [destination]
Destination is an output directory, not the live installation.
"""
import hashlib
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import re

SOURCE = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description='Build the Windows Scroll Linux v1 downloadable installer')
    parser.add_argument('destination', nargs='?', default=str(SOURCE / 'dist'))
    parser.add_argument('--binary', type=Path, default=SOURCE / 'daemon/target/release/midscrolld')
    options = parser.parse_args()
    destination = Path(options.destination).resolve()
    binary = options.binary.resolve()
    if not binary.is_file():
        raise ValueError('build the Rust release binary before packaging')
    for name in ('THIRD_PARTY_NOTICES.md', 'RUST_STDLIB_NOTICES.html'):
        if not (SOURCE / name).is_file():
            raise ValueError('run packaging/generate_notices.py with the build toolchain first')
    version = re.search(r'^version\s*=\s*"([^"]+)"', (SOURCE / 'daemon/Cargo.toml').read_text(), re.MULTILINE).group(1)
    binary_version = subprocess.check_output([str(binary), '--version'], text=True)
    if version not in binary_version:
        raise ValueError(f'binary version does not match source release {version}: {binary_version.strip()}')
    if destination.exists():
        raise ValueError(f'destination already exists: {destination}; choose a new directory')
    destination.mkdir(parents=True)
    payload = destination / 'payload'
    entries = {}

    def add(name, data, mode=0o644):
        target = payload / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
        entries[name] = {'mode': mode, 'sha256': hashlib.sha256(data).hexdigest()}

    add('release/midscrolld', binary.read_bytes(), 0o755)
    # SELinux assigns bin_t here, enabling the normal service domain transition.
    # Executing a binary labeled lib_t below /usr/local/lib does not do that.
    add('integration/usr/local/bin/midscroll', binary.read_bytes(), 0o755)
    add('release/LICENSE', (SOURCE / 'LICENSE').read_bytes())
    add('release/session_setup.py', (SOURCE / 'packaging/session_setup.py').read_bytes())
    for name in ('THIRD_PARTY_NOTICES.md', 'RUST_STDLIB_NOTICES.html', 'README.md', 'REVIEW-RESOLUTION.md'):
        add('release/' + name, (SOURCE / name).read_bytes())
    version_file = SOURCE / 'VERSION'
    add('release/VERSION', version_file.read_bytes() if version_file.exists() else b'Windows Scroll Linux v1\n')
    for file in sorted((SOURCE / 'app').iterdir()):
        if file.is_file() and file.suffix in ('.py', '.svg'):
            add('release/app/' + file.name, file.read_bytes())
    launchers = {
        'midscroll-control': 'exec /usr/bin/python3 -I /usr/local/lib/midscroll/v2/app/control.py "$@"\n',
        'midscroll-settings': 'export GDK_BACKEND=wayland\nexec /usr/bin/python3 -I /usr/local/lib/midscroll/v2/app/settings.py "$@"\n',
        'midscroll-apply': 'exec /usr/bin/python3 -I /usr/local/lib/midscroll/v2/app/apply_settings.py "$@"\n',
        'midscroll-overlay': (
            'export GDK_BACKEND=wayland\n'
            'export PATH=/usr/local/bin:/usr/bin\n'
            'midscroll_layer=$(/usr/bin/python3 -I -c '\
            "'import ctypes.util,gi; gi.require_version(\"Gtk4LayerShell\",\"1.0\"); print(ctypes.util.find_library(\"gtk4-layer-shell\") or \"\")' 2>/dev/null) || midscroll_layer=\n"
            'if [ -n "$midscroll_layer" ]; then\n'
            '    export LD_PRELOAD="$midscroll_layer"\n'
            'elif [ -r /usr/local/lib/midscroll/vendor/usr/lib64/libgtk4-layer-shell.so.0 ] && [ -r /usr/local/lib/midscroll/vendor/usr/lib64/girepository-1.0/Gtk4LayerShell-1.0.typelib ]; then\n'
            '    export LD_LIBRARY_PATH=/usr/local/lib/midscroll/vendor/usr/lib64\n'
            '    export GI_TYPELIB_PATH=/usr/local/lib/midscroll/vendor/usr/lib64/girepository-1.0\n'
            '    export LD_PRELOAD=/usr/local/lib/midscroll/vendor/usr/lib64/libgtk4-layer-shell.so.0\n'
            'fi\n'
            'exec /usr/bin/python3 -I /usr/local/lib/midscroll/v2/app/context_session.py "$@"\n'),
    }
    for name, body in launchers.items():
        add('integration/usr/local/bin/' + name, ('#!/bin/sh\n' + body).encode(), 0o755)
    for name, target in (('midscroll.service', 'system'), ('midscroll-overlay.service', 'user')):
        add(f'integration/etc/systemd/{target}/{name}', (SOURCE / 'packaging' / name).read_bytes())
    for filename, title, executable, comment in (
        ('io.github.gnhen.midscroll.Settings.desktop', 'Windows Scroll Linux Settings', 'midscroll-settings', 'Configure middle-button scrolling'),
        ('midscroll-start.desktop', 'Resume Windows Scroll Linux', 'midscroll-control enable', 'Resume scrolling; middle-click paste remains blocked'),
        ('midscroll-stop.desktop', 'Pause Windows Scroll Linux', 'midscroll-control disable', 'Pause scrolling; middle-click paste remains blocked')):
        desktop = ('[Desktop Entry]\nType=Application\n' + f'Name={title}\nComment={comment}\n'
                   + f'Exec=/usr/local/bin/{executable}\n'
                   + 'Icon=/usr/local/lib/midscroll/v2/app/move-all.svg\nTerminal=false\n'
                   + 'Categories=Settings;HardwareSettings;\nKeywords=mouse;scroll;autoscroll;wheel;\n')
        add('integration/usr/local/share/applications/' + filename, desktop.encode())
    (destination / 'manifest.json').write_text(json.dumps({'format': 1, 'product': 'Windows Scroll Linux', 'version': version, 'files': entries}, indent=2, sort_keys=True) + '\n')
    for name in ('install.py', 'uninstall.py', 'install.sh'):
        shutil.copy2(SOURCE / 'packaging' / name, destination / name)
    (destination / 'install.sh').chmod(0o755)
    for name in ('README.md', 'INSTALL.md', 'LICENSE', 'REVIEW-RESOLUTION.md'):
        shutil.copy2(SOURCE / name, destination / name)
    if (SOURCE / 'docs').is_dir():
        shutil.copytree(SOURCE / 'docs', destination / 'docs')
    print(destination)


if __name__ == '__main__':
    main()
