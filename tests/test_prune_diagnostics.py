import tempfile
import unittest
from pathlib import Path

import tracker


class PruneDiagnosticsTests(unittest.TestCase):
    def test_keeps_newest_diagnostics_and_never_records(self):
        with tempfile.TemporaryDirectory() as base:
            records = Path(base)/'records'; records.mkdir()
            ids = [f'2026110{i}-120000-00000000{i}' for i in range(1, 6)]
            for session_id in ids:
                for suffix in ('.csv', '.json', '-audit.jsonl', '-last-accepted.png'):
                    (records/(session_id+suffix)).write_text('x')
            self.assertEqual(tracker.prune_diagnostics(base, keep=2), 6)
            left = sorted(p.name for p in records.iterdir())
            for session_id in ids:
                self.assertIn(session_id+'.csv', left); self.assertIn(session_id+'.json', left)
            self.assertEqual([n for n in left if n.endswith('-audit.jsonl')],
                             [ids[3]+'-audit.jsonl', ids[4]+'-audit.jsonl'])

    def test_launch_log_is_trimmed_to_whole_lines(self):
        with tempfile.TemporaryDirectory() as base:
            log = Path(base)/'launch.log'
            log.write_text(''.join(f'line {i}\n' for i in range(5000)))
            tracker.prune_diagnostics(base, log_limit=1000, log_keep=100)
            text = log.read_text()
            self.assertLessEqual(len(text), 100)
            self.assertTrue(text.startswith('line ') and text.endswith('line 4999\n'))


if __name__ == '__main__':
    unittest.main()
