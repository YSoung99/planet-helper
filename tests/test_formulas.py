"""Compare against hand-calculated answers and independent closed-form trajectories."""
import random
import unittest
from decimal import Decimal

from unittest.mock import patch

import tracker

NO_BREAKS = patch.object(tracker, 'IDLE_AFTER', float('inf'))  # arithmetic checks with sparse samples


def seed(session, baseline):
    value = (baseline, round(baseline/session.total*100, 2))
    session.accept(value, -2)
    session.accept(value, -1)


class FormulaTests(unittest.TestCase):
    def test_eight_minute_hand_calculation_including_first_gain_and_sliding_window(self):
        s = tracker.Session(level=169)
        seed(s, 1000000)
        exp = 1006000
        s.accept((exp, round(exp/s.total*100, 2)), 0)
        expected_minutes = [12000, 12000, 18000, 24000, 30000, 36000, 42000, 48000]
        for second in range(1, 481):
            exp += ((second-1)//60+1)*100
            s.accept((exp, round(exp/s.total*100, 2)), second)
            if second % 60 == 0:
                completed = second//60
                snapshot = s.minute_result()
                self.assertEqual(snapshot['one'], expected_minutes[completed-1])
                self.assertEqual(snapshot['six'], sum(expected_minutes[max(0, completed-6):completed]))
        gained, hourly, _, _ = s.stats(480)
        self.assertEqual(gained, 222000)
        self.assertEqual(s.elapsed(480), 480)
        self.assertEqual(hourly/60, 27750)
        self.assertEqual(hourly/10, 166500)
        self.assertEqual(s.minute_result()['one'], 48000)
        self.assertEqual(s.minute_result()['six'], 198000)

    def test_irregular_capture_intervals_match_constant_rate_closed_form(self):
        rng = random.Random(20261031)
        for case in range(50):
            with self.subTest(case=case):
                rate, first_gain = rng.randint(1, 1000), rng.randint(1, 10000)
                s = tracker.Session(level=169)
                seed(s, 172680132)
                exp = 172680132+first_gain
                s.accept((exp, round(exp/s.total*100, 2)), 0)
                stamp, target = 0, rng.randint(365, 800)
                while stamp < target:
                    stamp = min(target, stamp+rng.randint(1, 5))
                    exp = 172680132+first_gain+rate*stamp
                    s.accept((exp, round(exp/s.total*100, 2)), stamp)
                completed = stamp//60
                expected_one = 60*rate
                expected_six = (first_gain+60*completed*rate) if completed <= 6 else 360*rate
                self.assertAlmostEqual(s.minute_result()['one'], expected_one, delta=1e-6)
                self.assertAlmostEqual(s.minute_result()['six'], expected_six, delta=1e-6)
                self.assertEqual(s.stats(stamp)[0], first_gain+rate*stamp)

    @NO_BREAKS
    def test_fractional_seconds_match_decimal_average_and_eta(self):
        s = tracker.Session(level=169)
        seed(s, 172680132)
        s.accept((172692477, round(172692477/s.total*100, 2)), 0)
        last = 172746798
        s.accept((last, round(last/s.total*100, 2)), 125.75)
        gained, hourly, _, eta = s.stats(125.75)
        expected_minute = Decimal(66666)*60/Decimal('125.75')
        expected_six = Decimal(66666)*360/Decimal('125.75')
        self.assertEqual(gained, 66666)
        self.assertAlmostEqual(hourly/60, float(expected_minute), places=7)
        self.assertAlmostEqual(hourly/10, float(expected_six), places=7)
        expected_eta = Decimal(s.total-last)*Decimal('125.75')/Decimal(66666)
        self.assertAlmostEqual(eta, float(expected_eta), places=7)

    def test_first_gain_is_intentionally_included_in_first_minute(self):
        s = tracker.Session(level=169)
        seed(s, 172680132)
        s.accept((172680232, 42.55), 0)
        s.accept((172680332, 42.55), 60)
        self.assertEqual(s.stats(60)[0], 200)
        self.assertEqual(s.stats(60)[1]/60, 200)
        self.assertEqual(s.minute_result()['one'], 200)
        s.accept((172680332, 42.55), 120)
        self.assertEqual(s.minute_result()['one'], 0)
        self.assertEqual(s.minute_result()['six'], 200)

    @NO_BREAKS
    def test_six_minute_average_uses_unrounded_rate_not_rounded_display(self):
        s = tracker.Session(level=169)
        seed(s, 172680132)
        s.accept((172680233, 42.55), 0)
        per_minute = s.stats(120)[1]/60
        per_six = per_minute*6
        self.assertEqual(per_minute, 50.5)
        self.assertEqual(per_six, 303)
        shown_minute, shown_six = int(f'{per_minute:.0f}'), int(f'{per_six:.0f}')
        self.assertEqual(abs(shown_six-shown_minute*6), 3)


if __name__ == '__main__':
    unittest.main()
