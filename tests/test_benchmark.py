"""Checks around source boundaries, dispatch failures and measurement isolation."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run
import toolchain


class BenchmarkTests(unittest.TestCase):
    def test_dependency_alternatives_and_cycles(self):
        packages = {
            'cc1': {'Package': 'cc1', 'Depends': 'missing | libc (>= 1), libgcc'},
            'libc': {'Package': 'libc', 'Pre-Depends': 'libgcc'},
            'libgcc': {'Package': 'libgcc', 'Depends': 'libc'},
        }
        self.assertEqual([p['Package'] for p in toolchain.closure(packages, 'cc1')],
                         ['cc1', 'libc', 'libgcc'])
        with self.assertRaisesRegex(RuntimeError, 'Unresolved'):
            toolchain.closure({'cc1': {'Package': 'cc1', 'Depends': 'missing'}}, 'cc1')

    def test_offline_corrupt_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive'
            path.write_bytes(b'corrupt')
            with patch('urllib.request.urlopen', side_effect=AssertionError('network used')):
                with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                    toolchain.download('https://example.invalid/a', path, '0' * 64, True)
                with self.assertRaisesRegex(RuntimeError, 'Offline cache miss'):
                    toolchain.download('https://example.invalid/a', Path(tmp) / 'missing', None, True)

    def test_source_archive_cannot_escape_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'cache'
            archive = cache / 'downloads' / 'bad-1.tar.gz'
            archive.parent.mkdir(parents=True)
            with tarfile.open(archive, 'w:gz') as stream:
                item = tarfile.TarInfo('../../escaped')
                item.size = 3
                stream.addfile(item, io.BytesIO(b'bad'))
            spec = {'version': '1', 'url': 'https://example.invalid',
                    'sha256': toolchain.sha256(archive)}
            args = argparse.Namespace(source_dir=None, offline=True)
            with patch.dict(run.REGISTRY, {'bad': spec}):
                with self.assertRaises(tarfile.FilterError):
                    run.source_tree('bad', args, cache)
            self.assertFalse((Path(tmp) / 'escaped').exists())

    def test_source_mirror_must_match_the_same_pinned_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrong = root / 'html-response'
            wrong.write_bytes(b'<html>not the requested release</html>')
            good = root / 'release.tar.gz'
            with tarfile.open(good, 'w:gz') as stream:
                item = tarfile.TarInfo('example-1/code.c')
                item.size = 3
                stream.addfile(item, io.BytesIO(b'yes'))
            spec = {'version': '1', 'url': wrong.as_uri(),
                    'mirrors': [good.as_uri()], 'sha256': toolchain.sha256(good)}
            args = argparse.Namespace(source_dir=None, offline=False)
            with patch.dict(run.REGISTRY, {'example': spec}), patch('sys.stdout', io.StringIO()):
                tree, metadata = run.source_tree('example', args, root / 'cache')
            self.assertEqual((tree / 'code.c').read_bytes(), b'yes')
            self.assertEqual(metadata['archive_sha256'], toolchain.sha256(good))
            self.assertEqual(metadata['download_url'], good.as_uri())

    def test_emulator_failure_does_not_fall_back_to_host_cc1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            emulator = root / 'fake-qemu'
            emulator.write_text('#!' + sys.executable + '\nimport os,sys\n'
                                'assert "QEMU_LOG" not in os.environ\n'
                                'assert sys.argv[-1] == "-fsyntax-only"\n'
                                'sys.exit(41)\n')
            emulator.chmod(0o755)
            events = root / 'events.jsonl'
            config = {'qemu': str(emulator), 'cpu': 'rv64', 'guest_sysroot': str(root),
                      'guest_cc1': '/guest/cc1', 'events': str(events), 'default_pic_flags': ['-fPIE']}
            cfg = root / 'config.json'
            cfg.write_text(json.dumps(config))
            env = dict(os.environ, QEMU_LOG='unwanted')
            result = subprocess.run([sys.executable, str(run.HERE / 'compiler.py'), 'dispatch',
                                     str(cfg), '/host/cc1', '-fsyntax-only'], env=env)
            self.assertEqual(result.returncode, 41)
            event = json.loads(events.read_text())
            self.assertTrue(event['emulated'])
            self.assertIn('/guest/cc1', event['argv'])
            self.assertNotIn('/host/cc1', event['argv'])
            self.assertIn('-fPIE', event['argv'])

    def test_changed_qemu_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'qemu'
            path.write_bytes(b'baseline')
            variant = {'path': str(path), 'sha256': toolchain.sha256(path)}
            run.verify_qemu(variant)
            path.write_bytes(b'other commit')
            with self.assertRaisesRegex(RuntimeError, 'changed during'):
                run.verify_qemu(variant)

    def test_modified_extracted_source_is_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'cache'
            archive = cache / 'downloads' / 'example-1.tar.gz'
            archive.parent.mkdir(parents=True)
            with tarfile.open(archive, 'w:gz') as stream:
                directory = tarfile.TarInfo('example-1')
                directory.type = tarfile.DIRTYPE
                stream.addfile(directory)
                item = tarfile.TarInfo('example-1/code.c')
                item.size = 3
                stream.addfile(item, io.BytesIO(b'old'))
            spec = {'version': '1', 'url': 'https://example.invalid',
                    'sha256': toolchain.sha256(archive)}
            args = argparse.Namespace(source_dir=None, offline=True)
            with patch.dict(run.REGISTRY, {'example': spec}):
                tree, _ = run.source_tree('example', args, cache)
                (tree / 'code.c').write_bytes(b'new')
                with self.assertRaisesRegex(RuntimeError, 'was modified'):
                    run.source_tree('example', args, cache)

    def test_unmarked_work_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'work/lz4'
            work.mkdir(parents=True)
            sentinel = work / 'user-file'
            sentinel.write_text('keep')
            source, session = root / 'source', root / 'session'
            source.mkdir()
            session.mkdir()
            args = argparse.Namespace(data_dir=root)
            with self.assertRaisesRegex(RuntimeError, 'unmarked'):
                run.build_once('lz4', source, {'label': 'base'}, {}, {}, args,
                               session, 1, False)
            self.assertEqual(sentinel.read_text(), 'keep')

    def test_local_cmake_cache_is_removed_only_in_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source'
            (source / 'build').mkdir(parents=True)
            (source / 'build/CMakeCache.txt').write_text('old compiler')
            work = Path(tmp) / 'work'
            run.copy_source(source, work, True)
            self.assertFalse((work / 'build').exists())
            self.assertEqual((source / 'build/CMakeCache.txt').read_text(), 'old compiler')
            with self.assertRaisesRegex(RuntimeError, 'overlap'):
                run.copy_source(work, work, True)

    def test_local_zlib_placeholder_is_configured_before_cleaning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, work = root / 'source', root / 'work'
            source.mkdir()
            placeholder = 'all:\n\t@echo "Please use ./configure first."\n'
            (source / 'Makefile').write_text(placeholder)
            (source / 'stale.o').write_bytes(b'previous compiler output')
            run.copy_source(source, work, False)
            args = argparse.Namespace(source_dir=source, jobs=1, timeout=1)
            commands = []

            def invoke(command, cwd, env, log, timeout):
                commands.append(command)
                if command == ['./configure', '--static']:
                    (cwd / 'Makefile').write_text('all:\nclean:\n\trm -f stale.o\n')
                elif command == ['make', 'clean']:
                    self.assertIn('clean:', (cwd / 'Makefile').read_text())
                    (cwd / 'stale.o').unlink()
                else:
                    self.fail(f'Unexpected preparation command: {command}')

            with patch('run.invoke', side_effect=invoke):
                build = run.prepare_build('zlib', args, root / 'cc', {}, work, {}, root / 'log')
            self.assertEqual(commands, [['./configure', '--static'], ['make', 'clean']])
            self.assertEqual(build, ['make', '-j1'])
            self.assertFalse((work / 'stale.o').exists())
            self.assertEqual((source / 'Makefile').read_text(), placeholder)
            self.assertTrue((source / 'stale.o').exists())

    def test_local_sqlite_clean_uses_the_benchmark_makefile(self):
        args = argparse.Namespace(source_dir=Path('/local/source'), jobs=1, timeout=1)
        with patch('run.invoke') as invoke:
            run.prepare_build('sqlite', args, Path('/cc'), {}, Path('/work'), {}, Path('/log'))
        self.assertEqual(invoke.call_args.args[0],
                         ['make', '-f', str(run.HERE / 'sqlite-benchmark.mk'), 'clean'])

    def test_fresh_upstream_zlib_configures_without_local_tree_cleanup(self):
        args = argparse.Namespace(source_dir=None, jobs=1, timeout=1)
        with patch('run.invoke') as invoke:
            run.prepare_build('zlib', args, Path('/cc'), {}, Path('/work'), {}, Path('/log'))
        self.assertEqual([entry.args[0] for entry in invoke.call_args_list], [['./configure', '--static']])

    def test_warmups_and_coverage_do_not_enter_statistics(self):
        with tempfile.TemporaryDirectory() as tmp:
            def row(label, seconds, warmup=False, coverage=False):
                return {'package': 'lz4', 'variant': label, 'iteration': 1,
                        'wall_seconds': seconds, 'user_seconds': seconds, 'sys_seconds': 0,
                        'cc1_invocations': 1, 'cc1_wall_sum': seconds,
                        'warmup': warmup, 'coverage_run': coverage,
                        'artifacts': {'lib.a': {'sha256': 'same'}},
                        'vset_counts': {'vsetivli': 123}}
            run.report([row('base', 100, warmup=True), row('base', 10), row('new', 5),
                        row('profile', 1000, coverage=True)], Path(tmp), 'complete')
            result = json.loads((Path(tmp) / 'results.json').read_text())
            self.assertEqual(len(result['statistics']), 2)
            self.assertEqual(result['statistics'][1]['wall_ratio'], 0.5)
            self.assertEqual(result['statistics'][1]['wall_change_percent'], -50)
            self.assertEqual(result['statistics'][0]['runs'], 1)
            summary = (Path(tmp) / 'summary.md').read_text()
            self.assertIn('Wall / baseline', summary)
            self.assertIn('| n/a | -50.00% | 0.500x |', summary)


if __name__ == '__main__':
    unittest.main()
