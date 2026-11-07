"""Checks for mechanism evidence, the scalar control and timing boundaries."""
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import microbench


class MicrobenchTests(unittest.TestCase):
    def test_checksum_matches_signed_32bit_guest_operations(self):
        self.assertEqual(struct.unpack('<QQQ', microbench.expected_output('vsetivli-chain', 37)),
                         (37, 8, 0xc8))
        self.assertEqual(struct.unpack('<QQQ', microbench.expected_output('scalar-control', 1 << 31)),
                         (0xffffffff80000000, 0, 0))
        self.assertEqual(struct.unpack('<QQQ', microbench.expected_output('scalar-control', (1 << 32) + 7)),
                         (7, 0, 0))

    def test_trace_resolves_future_destinations_and_ignores_other_links(self):
        symbols = {'benchmark_loop': 0x12000, 'vector_successor_first': 0x12004,
                   'vector_successor_second': 0x12014}
        trace = ('Trace 0: 0xA0 [00000000/0000000000012000/00000000/00000000]\n'
                 'Linking TBs 0xA0 index 0 -> 0xB0\n'
                 'Trace 0: 0xB0 [00000000/0000000000012004/00000000/00000000]\n'
                 'Linking TBs 0xB0 index 0 -> 0xC0\n'
                 'Trace 0: 0xC0 [00000000/0000000000012014/00000000/00000000]\n'
                 'Linking TBs 0xC0 index 1 -> 0xA0\n')
        edges = microbench.parse_chain_edges(trace, symbols)
        self.assertEqual([(e['source_pc'], e['destination_pc']) for e in edges],
                         [(0x12000, 0x12004), (0x12004, 0x12014)])
        self.assertEqual(microbench.parse_chain_edges('', symbols), [])

    def test_control_must_have_exactly_zero_vector_configurations(self):
        microbench.validate_counts('scalar-control', 37,
                                  {'vsetivli': 0, 'vsetvli': 0, 'vsetvl': 0})
        microbench.validate_counts('vsetivli-chain', 37,
                                  {'vsetivli': 74, 'vsetvli': 0, 'vsetvl': 0})
        with self.assertRaisesRegex(RuntimeError, 'coverage'):
            microbench.validate_counts('scalar-control', 37,
                                      {'vsetivli': 1, 'vsetvli': 0, 'vsetvl': 0})

    def test_calibration_and_warmup_are_excluded_from_wall_statistics(self):
        rows = [{'case': 'scalar-control', 'variant': 'base', 'wall_seconds': seconds,
                 'phase': phase} for phase, seconds in
                [('calibration', 100), ('warmup', 200), ('measured', 2), ('measured', 4)]]
        rows += [{'case': 'scalar-control', 'variant': 'new', 'wall_seconds': 1.5,
                  'phase': 'measured'}]
        result = microbench.summarize(rows, [{'label': 'base'}, {'label': 'new'}])
        self.assertEqual(result[0]['runs'], 2)
        self.assertEqual(result[0]['median_seconds'], 3)
        self.assertEqual(result[1]['wall_ratio'], 0.5)


if __name__ == '__main__':
    unittest.main()
