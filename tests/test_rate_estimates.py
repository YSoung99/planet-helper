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
