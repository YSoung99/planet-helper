import unittest

from recent_exp import RecentExp


class RecentExpTests(unittest.TestCase):
    def test_only_points_inside_five_minutes_are_available(self):
        recent = RecentExp()
        for stamp in range(0, 310, 2):
            recent.observe('window', stamp, (stamp, stamp/100))
        self.assertTrue(recent.available(308))
        self.assertEqual(recent.points[0][0], 8)
        self.assertFalse(recent.available(315))

    def test_discontinuities_never_count_toward_new_recording(self):
        for source, stamp, value in [('other', 4, (300, 30)), ('window', 13, (300, 30)),
                                     ('window', 4, (190, 19))]:
            with self.subTest(source=source, stamp=stamp, value=value):
                recent = RecentExp()
                recent.observe('window', 0, (100, 10))
                recent.observe('window', 2, (200, 20))
                self.assertTrue(recent.available(2))
                recent.observe(source, stamp, value)
                self.assertFalse(recent.available(stamp))
                self.assertEqual(list(recent.points), [(stamp, value)])
