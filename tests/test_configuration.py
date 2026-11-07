"""Configuration and affinity checks; no QEMU, builds, downloads or real affinity changes."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bench
import configuration as config
import run


class ConfigurationTests(unittest.TestCase):
    def test_all_examples_resolve_without_executing_or_creating_directories(self):
        for runner in bench.MODULES:
            path = config.HERE / 'experiments' / (runner + '.example.yml')
            with self.subTest(runner=runner), \
                 patch('subprocess.Popen', side_effect=AssertionError('process launched')), \
                 patch('os.sched_setaffinity', side_effect=AssertionError('affinity changed')), \
                 patch.object(Path, 'mkdir', side_effect=AssertionError('directory created')):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(bench.main(['--config', str(path), '--dry-run']), 0)
                plan = json.loads(stdout.getvalue())
                self.assertEqual(plan['action'], 'dry-run')
                self.assertEqual(plan['runner'], runner)
                self.assertEqual(plan['reference'], 'baseline')
                self.assertIsNone(plan['affinity']['effective'])

    def test_cli_overrides_config_and_keeps_each_qemu_identity(self):
        resolved = bench.resolve(['--config', str(config.HERE / 'experiments/packages.example.yml'),
                                  'zlib', '--qemu', 'candidate=/nonexistent/after',
                                  '--qemu', 'baseline=/nonexistent/before',
                                  '--qemu-commit', 'baseline=' + 'a' * 40,
                                  '--affinity', '2-3,6', '--repetitions', '4', '--jobs', '2', '--dry-run'])
        args = resolved['args']
        self.assertEqual(args.packages, ['zlib'])
        self.assertEqual((args.repetitions, args.jobs), (4, 2))
        self.assertEqual(args.affinity, '2,3,6')
        self.assertEqual(args.qemu, ['baseline=/nonexistent/before', 'candidate=/nonexistent/after'])
        self.assertEqual(args.qemu_commit, ['baseline=' + 'a' * 40])

    def test_relative_paths_resolve_from_experiment_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'experiment.yml'
            path.write_text('schema: 1\nname: relative\nrunner: packages\nworkloads: [zlib]\n'
                            'qemu:\n  baseline:\n    path: bin/qemu\nreference: baseline\n'
                            'storage:\n  data_dir: outputs\nhost:\n  affinity: [3]\n')
            resolved = bench.resolve(['--config', str(path), '--dry-run'])
            self.assertEqual(resolved['args'].qemu, ['baseline=' + str(Path(tmp) / 'bin/qemu')])
            self.assertEqual(resolved['args'].data_dir, Path(tmp) / 'outputs')
            self.assertEqual(resolved['args'].affinity, '3')

    def test_unfilled_example_fails_before_runner_execution(self):
        with patch('run.main', side_effect=AssertionError('runner started')):
            with self.assertRaisesRegex(ValueError, 'Set QEMU paths'):
                bench.main(['--config', str(config.HERE / 'experiments/packages.example.yml')])

    def test_duplicate_yaml_keys_and_unknown_experiment_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.yml'
            path.write_text('schema: 1\nname: first\nname: second\n')
            with self.assertRaisesRegex(ValueError, 'Duplicate YAML key'):
                config.read_yaml(path)
            path.write_text('schema: 1\nname: typo\nrunner: packages\nworkloads: [zlib]\n'
                            'host:\n  affinty: [2]\n')
            with self.assertRaisesRegex(ValueError, 'affinty'):
                bench.resolve(['--config', str(path), '--dry-run'])
            path.write_text('schema: 1\nname: !!python/object/apply:os.system [false]\n')
            with self.assertRaisesRegex(ValueError, 'Invalid YAML'):
                config.read_yaml(path)

    def test_yaml_fixture_hashes_match_preserved_assembly(self):
        for name, case in config.micro_definition('vsetvli')['cases'].items():
            self.assertEqual(hashlib.sha256((config.HERE / 'vsetvli' / (name + '.S')).read_bytes()).hexdigest(),
                             case['source_sha256'])
        source = config.micro_definition('vsetivli')['source']
        self.assertEqual(hashlib.sha256((config.HERE / source['path']).read_bytes()).hexdigest(), source['sha256'])

    def test_explicit_reference_reorders_variants_without_a_real_binary(self):
        args = argparse.Namespace(qemu=['new=/not/real/new', 'old=/not/real/old'],
                                  qemu_commit=[], reference='old')
        with patch('run.qemu_info', side_effect=lambda label, path: dict(label=label, path=str(path), commit=None)):
            self.assertEqual([v['label'] for v in run.variants(args)], ['old', 'new'])


class AffinityTests(unittest.TestCase):
    def test_cpu_list_and_ranges(self):
        self.assertEqual(config.parse_affinity('2-3,6'), {2, 3, 6})
        self.assertEqual(config.parse_affinity([2, 3, 6]), {2, 3, 6})
        for value in ('', '3-2', '-1', '2,,3', [], [True], [-1]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.parse_affinity(value)

    def test_requested_mask_is_applied_and_read_back(self):
        with patch('os.sched_getaffinity', side_effect=[{2, 3}, {2}]), patch('os.sched_setaffinity') as setter:
            self.assertEqual(config.apply_affinity('2'), dict(requested=[2], inherited=[2, 3], effective=[2]))
            setter.assert_called_once_with(0, {2})

    def test_unavailable_cpu_does_not_fall_back_silently(self):
        with patch('os.sched_getaffinity', return_value={2, 3}), patch('os.sched_setaffinity') as setter:
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                config.apply_affinity('0')
            setter.assert_not_called()

    def test_inheritance_needs_no_setter(self):
        with patch('os.sched_getaffinity', return_value={2, 3}), patch('os.sched_setaffinity') as setter:
            self.assertEqual(config.apply_affinity(None), dict(requested=None, inherited=[2, 3], effective=[2, 3]))
            setter.assert_not_called()

    def test_partial_application_is_rejected_and_original_mask_restored(self):
        with patch('os.sched_getaffinity', side_effect=[{2, 3}, {3}]), patch('os.sched_setaffinity') as setter:
            with self.assertRaisesRegex(RuntimeError, 'exactly'):
                config.apply_affinity('2')
            self.assertEqual(setter.call_args_list, [call(0, {2}), call(0, {2, 3})])


if __name__ == '__main__':
    unittest.main()
