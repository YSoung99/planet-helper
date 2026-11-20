"""Rests (EXP unchanged 3+ minutes) are excluded from hunting time and averages."""
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

    def test_whole_rest_from_last_gain_is_excluded(self):
        s, base, exp = self.build()
        # Rest: last gain at 300, next gain at 905 -> the whole 605s counts as rest.
        self.assertAlmostEqual(s.idle(1200), 605)
        self.assertAlmostEqual(s.active_elapsed(1200), 1200-605)
        gained, hourly, _, _ = s.stats(1200)
        self.assertAlmostEqual(hourly, gained/(1201-605)*3600)
        rate = s.rate_estimate(1200)
        self.assertEqual(rate['status'], 'ready')
        self.assertAlmostEqual(rate['duration'], 1201-605)

    def test_pause_shorter_than_three_minutes_stays_hunting_time(self):
        s = tracker.Session(level=176)
        s.accept(value(1000), -2); s.accept(value(1000), -1); s.accept(value(2000), 0)
        exp = hunt(s, 2000, 0, 100)
        exp = hunt(s, exp, 100, 270, per_second=0)  # 170s without EXP
        hunt(s, exp, 270, 300)
        self.assertEqual(s.idle(300), 0)
        self.assertAlmostEqual(s.active_elapsed(300), 300)

    def test_six_minute_window_skips_rest(self):
        s, base, exp = self.build()
        # 300s hunting after the rest + the last 60s before it, all at 1000 EXP/s.
        gain, covered = s.hunt_gain(360, 1200)
        self.assertEqual(covered, 360)
        self.assertAlmostEqual(gain, 360*1000, delta=5000)
        one, _ = s.hunt_gain(60, 1200)
        self.assertAlmostEqual(one, 60000, delta=5000)  # anchored at the observation before the first gain

    def test_break_in_progress_freezes_hunting_time(self):
        s = tracker.Session(level=176)
        s.accept(value(1000), -2); s.accept(value(1000), -1); s.accept(value(2000), 0)
        hunt(s, 2000, 0, 100)
        hunt(s, 2000+100000, 100, 400, per_second=0)
        self.assertTrue(s.resting(400))
        self.assertAlmostEqual(s.active_elapsed(400), 100)
        self.assertAlmostEqual(s.active_elapsed(390), 100)

    def test_restore_recomputes_breaks(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            s, base, exp = self.build()
            app = tracker.App.__new__(tracker.App)
            app.session, app.running, app.generation, app.save_warning = s, True, 0, ''
            with patch.object(tracker.time, 'monotonic', return_value=1200):
                app.finish_measurement('사용자 정지')
            restored = tracker.load_saved_session(Path(folder)/'records'/(s.session_id+'.csv'), 176, 5000)
            self.assertAlmostEqual(restored.idle(5000), 605)
            self.assertAlmostEqual(restored.active_elapsed(5000), 595)
            self.assertEqual(restored.hunt_gain(360, 5000), s.hunt_gain(360, 1200))


if __name__ == '__main__':
    unittest.main()
