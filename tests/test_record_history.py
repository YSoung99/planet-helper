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
        rows = [dict(memo='A', gained=9999, active=60, rate_gain=600, rate_seconds=60, per_minute=600, blocked=False),
                dict(memo='A', gained=200, active=120, rate_gain=200, rate_seconds=120, per_minute=100, blocked=False),
                dict(memo='A', gained=9999, active=60, rate_gain=9999, rate_seconds=60, per_minute=9999, blocked=True),
                dict(memo='A', gained=200, active=10, rate_gain=200, rate_seconds=10, per_minute=None, blocked=False)]
        self.assertEqual(record_history.by_memo(rows), [('A', 800/3, 180, 2)])


if __name__ == '__main__':
    unittest.main()
