import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import tracker
from mac_app import Controller


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=patch.object(tracker,'BASE',Path(self.temp.name));self.base.start()
        self.c=Controller(self.app,start_worker=False,restore=False);self.c.timer.stop()
    def tearDown(self):
        self.c.key_repeat.shutdown();self.c.start_captures.close();self.c.window.hide();self.c.window.deleteLater()
        self.app.processEvents();self.base.stop();self.temp.cleanup()
    def test_compact_full_transition_preserves_current_measurement_and_settings(self):
        session=self.c.session;helper=self.c.key_repeat
        self.assertTrue(self.c.compact);self.assertEqual(self.c.window.width(),360)
        self.assertLess(self.c.window.height(),180)
        self.c.toggle_display();self.assertFalse(self.c.compact);self.assertEqual(self.c.window.width(),640)
        self.assertLess(self.c.window.height(),480)  # two-column detailed view stays short
        self.assertIs(self.c.session,session);self.assertIs(self.c.key_repeat,helper)
        self.c.toggle_pin(False)
        clean,warning=tracker.load_settings(tracker.BASE/'settings.json')
        self.assertFalse(clean['always_on_top']);self.assertFalse(clean['compact']);self.assertIsNone(warning)
        self.c.toggle_display();self.assertLess(self.c.window.height(),180)
    def test_rate_display_changes_on_ten_second_observation_buckets_only(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        s.accept((172680232,42.55),3);c.update_rate_display(3)
        self.assertEqual(c.minute_label.text(),'집계 중')
        for stamp in range(4,62):s.accept((172680232,42.55),stamp)
        c.update_rate_display(61);self.assertEqual(c.minute_label.text(),'100')
        self.assertEqual(c.compact_six_average.text(),'600')
        s.accept((172680332,42.55),62);c.update_rate_display(62)
        self.assertEqual(c.minute_label.text(),'100')
        for stamp in range(63,72):s.accept((172680332,42.55),stamp)
        c.update_rate_display(71);self.assertEqual(c.minute_label.text(),'171')
        self.assertEqual(c.compact_six_average.text(),'1,029')
        c.update_rate_display(78);self.assertEqual(c.minute_label.text(),'인식 지연')

    def test_stopped_rate_reappears_with_asof_and_gap_even_in_same_display_bucket(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        for stamp in range(2,62):s.accept((172680232,42.55),stamp)
        c.update_rate_display(70)
        self.assertEqual(c.minute_label.text(),'인식 지연')
        s.finish(200,'사용자 정지');c.update_rate_display(200)
        self.assertEqual(c.minute_label.text(),'100')
        self.assertEqual(c.compact_six_average.text(),'600')
        self.assertIn('마지막 확인 0:59',c.rate_info.text())
        self.assertIn('139초 관측 공백',c.rate_info.text())

    def test_worker_corroboration_reaches_small_decrease_safe_stop(self):
        c=self.c;s=c.session
        s.accept((172680132,42.55),0);s.accept((172680132,42.55),1)
        s.accept((172685132,42.55),2)
        c.running=True
        from temporal_ocr import verify
        def f(value,stamp):
            return dict(value=value,score=.99,raw='fixture',observed_at=stamp)
        # A real rejected burst must not be delivered as a corroborated value.
        first=f((172680132,42.55),2.1)
        captures=iter([f(None,2.2),f(None,2.3)])
        selected,evidence,_=verify(first,lambda force:next(captures),s.total,s.last,
                                   lambda:False,lambda delay:False,level=s.level)
        self.assertIsNone(selected)
        c.events.put((c.generation,None,'fixture',.99,None,2.3,{'temporal':evidence}))
        c.poll();self.assertEqual(s.small_decrease_count,0)
        for stamp in (3,4,5):
            first=f((172680132,42.55),stamp)
            selected,evidence,_=verify(first,lambda force:first,s.total,s.last,
                                       lambda:False,lambda delay:False,level=s.level)
            self.assertIsNotNone(selected)
            c.events.put((c.generation,selected['value'],'fixture',.99,None,stamp,{'temporal':evidence}))
        c.poll()
        self.assertTrue(s.blocked)
        self.assertFalse(c.running)
        self.assertIsNotNone(s.ended)
        self.assertEqual(s.stats(6)[0],5000)
        self.assertIn('감소 충돌',c.footer.text())
        self.assertIn('확인 필요',c.compact_total.toolTip())
        c.save_warning='자동 저장 실패'
        c.audit_warning='진단 저장 실패'
        c.poll()
        c.window.show();self.app.processEvents()
        self.assertTrue(c.footer.isVisibleTo(c.window))
        self.assertIn('감소 충돌',c.footer.text())
        self.assertIn('자동 저장 실패',c.footer.toolTip())
