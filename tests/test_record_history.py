import tempfile
import unittest
from pathlib import Path

import record_history


class RecordHistoryTests(unittest.TestCase):
    def test_memos_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as base:
            self.assertEqual(record_history.save_memo('20261101-120000-1', '  하늘   정원 ', base), '하늘 정원')
            self.assertEqual(record_history.load_memos(base), {'20261101-120000-1': '하늘 정원'})
            record_history.save_memo('20261101-120000-1', '', base)
            self.assertEqual(record_history.load_memos(base), {})

    def test_spot_average_is_weighted_by_hunting_time(self):
        rows = [dict(memo='A', gained=600, active=60), dict(memo='A', gained=0, active=60),
                dict(memo='B', gained=100, active=60), dict(memo='', gained=999, active=60)]
        self.assertEqual(record_history.by_memo(rows), [('A', 300.0, 120, 2), ('B', 100.0, 60, 1)])


if __name__ == '__main__':
    unittest.main()
