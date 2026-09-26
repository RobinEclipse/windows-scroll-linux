#!/usr/bin/python3
"""Collect license texts from the exact locked Cargo graph and local toolchain.

Run with the same cargo/rustc on PATH that build the distributed binary. This
does not download dependencies: cargo metadata is both --locked and --offline.
"""
import json
from pathlib import Path
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parent.parent


def output(argv):
    return subprocess.check_output(argv, text=True, cwd=SOURCE)


def generate():
    metadata = json.loads(output(['cargo', 'metadata', '--manifest-path',
                                 'daemon/Cargo.toml', '--locked', '--offline',
                                 '--format-version', '1']))
    toolchain = output(['rustc', '--version']).strip()
    sysroot = Path(output(['rustc', '--print', 'sysroot']).strip())
    standard = sysroot / 'share/doc/rust/COPYRIGHT-library.html'
    if not standard.is_file():
        raise RuntimeError('the Rust toolchain standard-library copyright notice is missing')
    parts = [
        '# Third-party notices\n',
        'This source and updater include a locally rewritten autoscroll daemon '
        'and desktop helper. Project code is under the Unlicense; see LICENSE.\n',
        'The earlier implementation and overlay design were adapted from '
        '[gnhen/midscroll](https://github.com/gnhen/midscroll), version 1.15, '
        'commit ac76b554cf6b279d646dee8336b45390d9db3597, under the Unlicense. '
        'The original project LICENSE is retained. The four-direction SVG in '
        'this version is a locally drawn replacement; the upstream Lucide '
        'move-vertical icon is not included in this payload.\n',
        'Python, PyGObject, GTK, AT-SPI, D-Bus Python bindings, Pycairo/Cairo, '
        'Gtk4LayerShell, libsystemd, libudev, and system libraries are runtime '
        'dependencies supplied by the existing system. Their libraries are not '
        'copied into this updater. They retain their respective licenses.\n',
        f'Rust toolchain used to collect notices: `{toolchain}`. The accompanying '
        '`RUST_STDLIB_NOTICES.html` is copied verbatim from that toolchain and '
        'covers the standard library and its bundled dependencies.\n',
        'The sections below include every package in Cargo.lock\'s resolved '
        'dependency graph, including build-time and platform-specific packages '
        'that may not be linked into the Linux binary. License and notice texts '
        'are copied verbatim from each downloaded crate.\n',
    ]
    for package in sorted(metadata['packages'], key=lambda item: (item['name'], item['version'])):
        if Path(package['manifest_path']).resolve() == (SOURCE / 'daemon/Cargo.toml').resolve():
            continue
        root = Path(package['manifest_path']).parent
        files = sorted(path for path in root.rglob('*') if path.is_file()
                       and path.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')))
        license_file = package.get('license_file')
        if license_file:
            explicit = root / license_file
            if explicit not in files:
                files.append(explicit)
        if not files:
            raise RuntimeError(f'no license text found for {package["name"]} {package["version"]}')
        parts.append(f'\n## {package["name"]} {package["version"]}\n\n')
        parts.append(f'Declared license: `{package.get("license") or "see license file"}`.\n\n')
        if package.get('repository'):
            parts.append(f'Upstream: {package["repository"]}\n\n')
        patches = root / 'MIDSCROLL-PATCH.md'
        if patches.is_file():
            parts.append('### Local modifications\n\n')
            parts.append(patches.read_text())
            parts.append('\n')
        for file in files:
            parts.append(f'### {file.relative_to(root).as_posix()}\n\n```text\n')
            parts.append(file.read_text())
            parts.append('\n```\n')
    (SOURCE / 'THIRD_PARTY_NOTICES.md').write_text('\n'.join(parts))
    shutil.copyfile(standard, SOURCE / 'RUST_STDLIB_NOTICES.html')
    print('Generated THIRD_PARTY_NOTICES.md and RUST_STDLIB_NOTICES.html')


if __name__ == '__main__':
    generate()
