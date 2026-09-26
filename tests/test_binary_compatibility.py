"""ABI release gates can be checked without running foreign binaries."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'binary_compatibility', Path(__file__).resolve().parents[1] / 'packaging/check_binary.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BinaryCompatibility(unittest.TestCase):
    def inspect(self, *, machine='Advanced Micro Devices X86-64',
                kind='DYN (Position-Independent Executable file)',
                versions='GLIBC_2.2.5 GLIBC_2.9 GLIBC_2.34',
                libraries='Shared library: [libc.so.6]\nShared library: [libgcc_s.so.1]'):
        def readelf(argv, **kwargs):
            return {'--file-header': f'Machine: {machine}\nType: {kind}',
                    '--version-info': versions, '--dynamic': libraries}[argv[1]]
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'midscrolld'
            binary.write_bytes(b'fixed-test-binary')
            with patch.object(module.subprocess, 'check_output', side_effect=readelf):
                return module.inspect(binary)

    def test_numeric_version_order_and_metadata(self):
        result = self.inspect()
        self.assertEqual(result['minimum_glibc_from_elf'], '2.34')
        self.assertEqual(result['architecture'], 'x86_64')
        self.assertEqual(len(result['sha256']), 64)
        self.assertEqual(result['bytes'], 17)

    def test_newer_glibc_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exceeds'):
            self.inspect(versions='GLIBC_2.34 GLIBC_2.35')

    def test_other_architecture_is_not_mislabeled(self):
        with self.assertRaisesRegex(ValueError, 'x86-64'):
            self.inspect(machine='AArch64')

    def test_non_pie_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'position-independent'):
            self.inspect(kind='EXEC (Executable file)')

    def test_missing_glibc_is_not_assumed_portable(self):
        with self.assertRaisesRegex(ValueError, 'glibc'):
            self.inspect(versions='No version information found')

    def test_unexpected_linked_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            self.inspect(libraries='Shared library: [libunexpected.so]')
