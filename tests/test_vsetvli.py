"""Check the independent guest oracle and exclusion of diagnostic samples."""
import argparse
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run
import vsetvli


class VsetvliTests(unittest.TestCase):
    def test_zero_and_full_vl_alternation_with_tail_preservation(self):
        # Seven iterations activate four bytes three times. The final VL is 0,
        # while tail-undisturbed policy preserves the three accumulated adds.
        expected = struct.pack('<QQQQ', 12, 0, 6, 0) + b'\x03' * 4
        self.assertEqual(vsetvli.expected_output('case3-small', 7), expected)
        self.assertEqual(vsetvli.expected_output('case1-register', 7), expected)

    def test_zero_rd_keeps_csr_and_vector_checks(self):
        self.assertEqual(vsetvli.expected_output('case4-small', 2),
                         struct.pack('<QQQQ', 0, 4, 6, 0) + b'\x01' * 4)
        self.assertEqual(vsetvli.expected_output('case2-x0-x0', 7),
                         struct.pack('<QQQQ', 0, 4, 6, 0) + b'\x07' * 4)

    def test_scalar_control_and_max_avl(self):
        self.assertEqual(vsetvli.expected_output('scalar-control', 7),
                         struct.pack('<QQQQ', 16, 0, 0, 0) + bytes(4))
        self.assertEqual(vsetvli.expected_output('case5-max', 257),
                         struct.pack('<QQQQ', 4112, 16, 0, 0) + b'\x01' * 16)

    def test_summary_uses_matched_pairs_and_measured_phase_only(self):
        rows = [dict(case='case3-small', variant=label, wall_seconds=seconds,
                     pair=pair, phase=phase) for label, seconds, pair, phase in (
                         ('base', 999, 1, 'calibration'), ('new', 888, 1, 'warmup'),
                         ('base', 10, 1, 'measured'), ('new', 8, 1, 'measured'),
                         ('new', 4, 2, 'measured'), ('base', 5, 2, 'measured'))]
        result = vsetvli.summarize(rows, ['case3-small'], ['base', 'new'])
        self.assertEqual(result[0]['median_seconds'], 7.5)
        self.assertEqual(result[1]['median_seconds'], 6)
        self.assertEqual(result[1]['runs'], 2)
        self.assertEqual(result[1]['faster_pairs'], 2)
        for value in result[1]['paired_reduction_percent']:
            self.assertAlmostEqual(value, 20)

    def test_qemu_has_no_workspace_default(self):
        with self.assertRaisesRegex(RuntimeError, 'Pass --qemu'):
            run.variants(argparse.Namespace(qemu=None, qemu_commit=[]))


if __name__ == '__main__':
    unittest.main()
