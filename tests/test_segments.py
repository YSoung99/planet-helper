import tempfile
import unittest

import segments


def minutes(values):
    return [{'index': i+1, 'one': v} for i, v in enumerate(values)]


class SegmentTests(unittest.TestCase):
    def test_detects_a_lasting_drop(self):
        values = [600, 620, 580, 610, 590, 605, 300, 320, 290, 310, 305, 295]
        changes = segments.find_changes(minutes(values))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['minute'], 7)
        self.assertLess(changes[0]['after'], changes[0]['before'])

    def test_steady_hunting_and_breaks_are_not_changes(self):
        steady = [600, 640, 570, 610, 590, 630, 560, 600, 0, 0, 0, 0, 610, 590, 620, 580, 600, 610]
        self.assertEqual(segments.find_changes(minutes(steady)), [])

    def test_minutes_next_to_breaks_are_ignored(self):
        points = segments.active_minutes(minutes([600, 600, 100, 0, 200, 600, 600]))
        self.assertEqual([i for i, _ in points], [1, 2, 6, 7])  # 3 and 5 touch the break

    def test_tags_round_trip(self):
        with tempfile.TemporaryDirectory() as base:
            segments.save_tag('s1', 7, '  버프   끝 ', base)
            self.assertEqual(segments.load_tags(base), {'s1': {'7': '버프 끝'}})


if __name__ == '__main__':
    unittest.main()
