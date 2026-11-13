"""Breaks without EXP gain are excluded from hunting time and averages."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tracker


def value(exp, level=176):
    return exp, round(exp/tracker.EXP_TABLE[level]*100, 2)


def hunt(s, exp, start, end, per_second=1000, step=5):
    t = start
    while t < end:
        t += step
        if per_second: exp += per_second*step
        s.accept(value(exp), t)
    return exp


class IdleTimeTests(unittest.TestCase):
    def build(self):
        s = tracker.Session(level=176)
        base = 1000000
        s.accept(value(base), -2); s.accept(value(base), -1)
        s.accept(value(base+1000), 0)            # first increase starts the timer
        exp = hunt(s, base+1000, 0, 300)          # 5 min hunting
        exp = hunt(s, exp, 300, 900, per_second=0)  # 10 min break
        exp = hunt(s, exp, 900, 1200)             # 5 min hunting
        return s, base, exp

    def test_break_beyond_grace_is_excluded(self):
        s, base, exp = self.build()
        # Break: last gain at 300, next gain at 905 -> 605s gap, 545s beyond the 60s grace.
        self.assertAlmostEqual(s.idle(1200), 545)
        self.assertAlmostEqual(s.active_elapsed(1200), 1200-545)
        gained, hourly, _, _ = s.stats(1200)
        self.assertAlmostEqual(hourly, gained/(1200-545)*3600)
        rate = s.rate_estimate(1200)
        self.assertEqual(rate['status'], 'ready')
        self.assertAlmostEqual(rate['duration'], 1201-545)  # anchored at the observation before the first gain

    def test_break_in_progress_freezes_hunting_time(self):
        s = tracker.Session(level=176)
        s.accept(value(1000), -2); s.accept(value(1000), -1); s.accept(value(2000), 0)
        hunt(s, 2000, 0, 100)
        hunt(s, 2000+100000, 100, 400, per_second=0)
        self.assertTrue(s.resting(400))
        self.assertAlmostEqual(s.active_elapsed(400), 160)
        self.assertAlmostEqual(s.active_elapsed(390), 160)

    def test_restore_recomputes_breaks(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            s, base, exp = self.build()
            app = tracker.App.__new__(tracker.App)
            app.session, app.running, app.generation, app.save_warning = s, True, 0, ''
            with patch.object(tracker.time, 'monotonic', return_value=1200):
                app.finish_measurement('사용자 정지')
            restored = tracker.load_saved_session(Path(folder)/'records'/(s.session_id+'.csv'), 176, 5000)
            self.assertAlmostEqual(restored.idle(5000), 545)
            self.assertAlmostEqual(restored.active_elapsed(5000), 655)


if __name__ == '__main__':
    unittest.main()
