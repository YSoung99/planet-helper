"""A level-up continues the measurement instead of ending it."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tracker


def value(exp, level):
    return exp, round(exp/tracker.EXP_TABLE[level]*100, 2)


class LevelUpTests(unittest.TestCase):
    def run_session(self):
        s = tracker.Session(level=176)
        need = tracker.EXP_TABLE[176]
        start = need-300000  # level-up at second 120
        s.accept(value(start, 176), -2); s.accept(value(start, 176), -1)
        exp = start
        for second in range(1, 241):  # 2,500 EXP per second for 4 minutes
            exp += 2500
            if exp >= need and s.level == 176:
                self.assertTrue(s.level_up(177))
            raw = exp-need if s.level == 177 else exp
            self.assertNotIn('보류', s.accept(value(raw, s.level), second) or '')
        return s, start, exp

    def test_totals_and_minutes_continue_across_level_up(self):
        s, start, exp = self.run_session()
        self.assertEqual(s.level, 177)
        self.assertEqual(s.total, tracker.EXP_TABLE[177])
        self.assertEqual(s.stats(240)[0], exp-start)
        self.assertEqual(s.minute_result()['one'], 150000)
        self.assertEqual(s.minute_result()['six'], 2500+180*2500)  # started at first increase (t=1)
        self.assertFalse(s.blocked)
        self.assertEqual(s.level_ups, [(176, 177, tracker.EXP_TABLE[176])])

    def test_rejects_level_down_and_unknown_levels(self):
        s = tracker.Session(level=176)
        s.accept(value(1000, 176), -2); s.accept(value(1000, 176), -1)
        self.assertFalse(s.level_up(175))
        self.assertFalse(s.level_up(10**6))
        self.assertFalse(tracker.Session(level=176).level_up(177))  # no baseline yet

    def test_restore_after_level_up_keeps_total(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            s, start, exp = self.run_session()
            app = tracker.App.__new__(tracker.App)
            app.session, app.running, app.generation, app.save_warning = s, True, 0, ''
            with patch.object(tracker.time, 'monotonic', return_value=240):
                app.finish_measurement('사용자 정지')
            restored = tracker.load_saved_session(Path(folder)/'records'/(s.session_id+'.csv'), 177, 1000)
            self.assertEqual(restored.stats(1000)[0], exp-start)
            self.assertEqual(restored.last[0], exp-tracker.EXP_TABLE[176])
            self.assertEqual(restored.elapsed(1000), 239)
            self.assertEqual(restored.stop_reason, '사용자 정지')

    def test_first_gain_recorded_for_projection(self):
        s, start, exp = self.run_session()
        self.assertEqual(s.first_gain, 2500)


class StartCapturePruneTests(unittest.TestCase):
    def test_keeps_newest_sessions_and_foreign_files(self):
        from start_captures import StartCaptures
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            names = [f'2026110{i}-120000-00000000{i}' for i in range(1, 6)]
            for name in names:
                (root/name).mkdir()
                for stage in StartCaptures.STAGES:
                    (root/name/(stage+'.png')).write_bytes(b'x'); (root/name/(stage+'.json')).write_text('{}')
            (root/names[0]/'notes.txt').write_text('keep me')
            (root/'other-folder').mkdir()
            captures = StartCaptures(root, keep=2); captures.flush(); captures.close()
            self.assertEqual(sorted(p.name for p in root.iterdir()), [names[0], names[3], names[4], 'other-folder'])
            self.assertEqual([p.name for p in (root/names[0]).iterdir()], ['notes.txt'])


if __name__ == '__main__':
    unittest.main()
