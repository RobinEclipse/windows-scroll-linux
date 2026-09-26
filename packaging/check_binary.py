#!/usr/bin/env python3
"""Check the public x86-64 GNU daemon's runtime ABI and record its digest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def inspect(binary):
    binary = Path(binary)
    env = dict(os.environ, LC_ALL='C')
    def readelf(*args):
        return subprocess.check_output(['readelf', *args, str(binary)], text=True, env=env)
    header = readelf('--file-header')
    if 'Advanced Micro Devices X86-64' not in header:
        raise ValueError('public prebuilt daemon must target Linux x86-64')
    if 'DYN (Position-Independent Executable file)' not in header:
        raise ValueError('public prebuilt daemon must be a position-independent executable')
    versions = set(re.findall(r'GLIBC_(\d+(?:\.\d+)+)', readelf('--version-info')))
    if not versions:
        raise ValueError('expected a dynamically linked glibc binary')
    latest = max(versions, key=lambda text: tuple(map(int, text.split('.'))))
    if tuple(map(int, latest.split('.'))) > (2, 34):
        raise ValueError(f'glibc requirement {latest} exceeds the supported binary baseline 2.34')
    needed = re.findall(r'Shared library: \[([^]]+)\]', readelf('--dynamic'))
    for name in needed:
        if name not in {'libc.so.6', 'libm.so.6', 'libgcc_s.so.1', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1'}:
            raise ValueError(f'unexpected linked runtime library: {name}')
    return {
        'product': 'Windows Scroll Linux', 'version': '1.0.0',
        'architecture': 'x86_64', 'target': 'x86_64-unknown-linux-gnu',
        'minimum_glibc_from_elf': latest,
        'build_baseline': 'Ubuntu 22.04 (glibc 2.35)',
        'needed_libraries': sorted(needed),
        'dynamically_loaded_libraries': ['libsystemd.so.0', 'libudev.so.1'],
        'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
        'bytes': binary.stat().st_size,
        'scope': 'Daemon ABI only; the desktop helper has additional KDE Wayland and Python dependencies.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        data = json.dumps(inspect(args.binary), indent=2, sort_keys=True) + '\n'
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Binary compatibility check failed: {exc}\n')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(data)
    else:
        print(data, end='')


if __name__ == '__main__':
    main()
