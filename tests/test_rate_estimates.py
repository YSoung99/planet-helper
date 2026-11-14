from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tracker
from rates import estimate


class RateTests(unittest.TestCase):
    def session(self):
        s=tracker.Session(level=170)
        s.accept((178583288,41.72),0);s.accept((178583288,41.72),1)
        return s

    def test_first_packet_time_is_paired_and_early_extrapolation_is_withheld(self):
        s=self.session();s.accept((179084320,41.84),3)
        self.assertEqual(s.rate_anchor,(1,178583288));self.assertEqual(s.started,3)
        self.assertEqual(s.stats(8)[0],501032)
        self.assertEqual(s.rate_estimate(8)['status'],'warming')
        for t in range(4,62):s.accept((179084320,41.84),t)
        rate=s.rate_estimate(61)
        self.assertEqual(rate['duration'],60);self.assertEqual(rate['cumulative'],501032)

    def test_long_idle_before_first_gain_does_not_enter_average(self):
        s=self.session();s.accept((178583288,41.72),98);s.accept((179084320,41.84),100)
        self.assertEqual(s.rate_anchor[0],98)
        s=self.session();s.accept((179084320,41.84),100)
        self.assertEqual(s.rate_anchor,(100,179084320));self.assertEqual(s.rate_anchor_kind,'first_increase')

    def test_capture_time_freezes_denominator_until_next_observation_and_marks_stale(self):
        pts=[(t,1000+t*10) for t in range(0,201)]
        a=estimate(pts,(0,1000),200);self.assertEqual(a['cumulative'],600)
        b=estimate(pts,(0,1000),205)
        self.assertEqual(b['cumulative'],a['cumulative']);self.assertEqual(b['recent'],600)
        self.assertIsNone(estimate(pts,(0,1000),207)['cumulative'])

    def test_observed_zero_growth_is_included_not_suppressed(self):
        pts=[(t,1000+min(t,60)*10) for t in range(0,241)]
        rate=estimate(pts,(0,1000),240)
        self.assertEqual(rate['cumulative'],150);self.assertEqual(rate['recent'],0)

    def test_long_gap_keeps_whole_total_but_withholds_recent_estimate(self):
        pts=[(0,1000),(1,1010),(60,1600),(61,1610)]
        rate=estimate(pts,(0,1000),61)
        self.assertEqual(rate['cumulative'],600)
        self.assertIsNone(rate['recent']);self.assertEqual(rate['recent_status'],'gap')

    def test_rate_anchor_survives_sample_pruning_and_saved_record_restore(self):
        s=self.session();s.accept((178583388,41.72),3)
        for t in range(4,705):s.accept((178583388+(t-3)*10,41.72),t)
        expected=s.rate_estimate(704)
        self.assertEqual(s.rate_anchor[0],1);self.assertGreater(s.samples[0][0],1)
        with tempfile.TemporaryDirectory() as folder,patch.object(tracker,'BASE',Path(folder)):
            app=tracker.App.__new__(tracker.App);app.session=s
            with patch.object(tracker.time,'monotonic',return_value=704):app.save_observation()
            restored=tracker.load_saved_session(Path(folder)/'records'/(s.session_id+'.csv'),170,1000)
            actual=restored.rate_estimate(1000)
            self.assertAlmostEqual(actual['cumulative'],expected['cumulative'],delta=.1)
            self.assertAlmostEqual(actual['recent'],expected['recent'],delta=.1)
            self.assertEqual(restored.stats(1000)[0],s.stats(704)[0])

    def test_gap_excludes_both_sides_and_history_replays_the_same_rate(self):
        import record_history
        s = self.session()
        exp = s.last[0]+100
        s.accept((exp, 41.72), 2)
        for stamp in range(3, 73):
            exp += 10
            s.accept((exp, 41.72), stamp)
        before = s.rate_estimate(72)
        exp += 50000
        s.accept((exp, round(exp/s.total*100, 2)), 164)
        recovered = s.rate_estimate(164)
        self.assertEqual(recovered['cumulative'], before['cumulative'])
        self.assertEqual(recovered['recent_status'], 'gap')
        self.assertEqual(s.gap_seconds(164), 92)
        self.assertEqual(s.gap_gain, 50000)
        self.assertEqual(s.idle(164), 0)
        self.assertEqual(s.stats(164)[0], 50800)
        for stamp in range(165, 225):
            exp += 10
            s.accept((exp, round(exp/s.total*100, 2)), stamp)
        s.finish(270, '사용자 정지')
        rate = s.rate_estimate(270)
        self.assertEqual(rate['rate_gain'], 1400)
        self.assertEqual(rate['duration'], 131)
        self.assertEqual(s.active_elapsed(270)+s.idle(270)+s.gap_seconds(270), s.elapsed(270))
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            app = tracker.App.__new__(tracker.App); app.session = s
            app.save_observation()
            path = Path(folder)/'records'/(s.session_id+'.csv')
            row = record_history.summarize(path)
            self.assertEqual(row['per_minute'], rate['cumulative'])
            self.assertEqual(row['gained'], 51400)
            self.assertEqual(row['gap'], 138)
            self.assertEqual(row['gap_gain'], 50000)

    def test_gap_restarts_rest_grace_and_exactly_ten_seconds_is_continuous(self):
        s = self.session()
        exp = s.last[0]+100
        s.accept((exp, 41.72), 2)
        for stamp in (12, 22, 32, 42):
            s.accept((exp, 41.72), stamp)
        self.assertEqual(s.gap_seconds(42), 0)
        s.accept((exp, 41.72), 52.001)
        for stamp in (62.001, 72.001, 82.001, 92.001):
            s.accept((exp, 41.72), stamp)
        self.assertEqual(s.gap_seconds(92.001), 10.001)
        self.assertEqual(s.idle(92.001), 0)
        self.assertFalse(s.resting(92.001))

    def test_short_resume_boundary_is_excluded_after_save_and_reload(self):
        s = self.session()
        exp = s.last[0]+100
        for stamp in range(2, 63):
            s.accept((exp+stamp, 41.72), stamp)
        s.resume_boundary_pending = True
        s.accept((exp+500, 41.72), 66)
        self.assertEqual(s.rows[-1][-1], 'hunting_resume')
        self.assertEqual(s.gap_seconds(66), 4)
        self.assertEqual(s.gap_gain, 438)
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            app = tracker.App.__new__(tracker.App); app.session = s
            s.finish(66, '사용자 정지'); app.save_observation()
            path = Path(folder)/'records'/(s.session_id+'.csv')
            restored = tracker.load_saved_session(path, s.level, 1000)
            self.assertEqual(restored.gap_gain, 438)
            self.assertEqual(restored.gap_seconds(1000), 4)
            self.assertEqual(restored.rate_estimate(1000)['cumulative'], s.rate_estimate(66)['cumulative'])
