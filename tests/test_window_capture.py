import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tracker
from window_capture import WindowCapture, region_from_desktop, valid_roi, pixel_rect


def window(number,bundle,title='MapleStory Worlds',width=1200,height=700):
    return SimpleNamespace(windowID=lambda:number,title=lambda:title,
        owningApplication=lambda:SimpleNamespace(bundleIdentifier=lambda:bundle,processID=lambda:123),
        frame=lambda:SimpleNamespace(size=SimpleNamespace(width=width,height=height)))


class WindowCaptureTests(unittest.TestCase):
    def test_pixel_crop_uses_the_complete_window_coordinate_space(self):
        roi=dict(left=.44,top=.97,width=.11,height=.025)
        self.assertEqual(pixel_rect(roi,2400,1400),(1056,1358,264,35))
        left,top,width,height=pixel_rect(dict(left=.9,top=.9,width=.1,height=.1),2484,1432)
        self.assertEqual(left+width,2484)
        self.assertEqual(top+height,1432)

    def test_update_handoff_keeps_gain_and_elapsed_time_across_restart(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker,'BASE',Path(folder)), \
                patch.object(tracker,'current_boot_id',return_value='same-boot'):
            app=tracker.App.__new__(tracker.App)
            app.session=tracker.Session(level=170)
            app.status=SimpleNamespace(set=lambda text:None)
            app.value=SimpleNamespace(set=lambda text:None)
            app.session.accept((2353472,.55),0)
            app.session.accept((2353472,.55),1)
            app.session.accept((2353572,.55),3)
            app.session.finish(13,'update')
            app.save_observation()
            session_id=app.session.session_id
            checkpoint=Path(folder)/'pending-update.json'
            checkpoint.write_text(json.dumps({'session_id':session_id,'baseline':[2353472,.55],
                'running_before_update':True,'snapshot_monotonic':13,'boot_id':'same-boot'}))
            app.running=False
            with patch.object(tracker.time,'monotonic',return_value=15):
                app.restore_latest_record()
                self.assertTrue(app.resume_handoff(checkpoint))
            self.assertEqual(app.session.session_id,session_id)
            self.assertTrue(app.running)
            self.assertEqual(app.session.elapsed(15),12)
            app.session.accept((2353672,.55),16)
            self.assertEqual(app.session.stats(16)[0],200)
            app.running=False
            with patch.object(tracker.time,'monotonic',return_value=200):
                self.assertFalse(app.resume_handoff(checkpoint))
            self.assertFalse(app.running)

    def test_only_the_game_bundle_can_be_selected(self):
        client=WindowCapture()
        client.content=SimpleNamespace(windows=lambda:[window(1,'com.other','MapleStory Worlds',4000,3000),window(2,'com.nexon.mod.inhouse')])
        client.content_at=time.monotonic()
        self.assertEqual(client.find_window().windowID(),2)

    def test_selected_window_stays_selected_when_another_client_is_larger(self):
        client=WindowCapture(preferred_id=2)
        client.content=SimpleNamespace(windows=lambda:[window(2,'com.nexon.mod.inhouse'),window(3,'com.nexon.mod.inhouse',width=2000,height=1600)])
        client.content_at=time.monotonic()
        self.assertEqual(client.find_window().windowID(),2)

    def test_missing_game_never_falls_back_to_another_app(self):
        client=WindowCapture()
        client.content=SimpleNamespace(windows=lambda:[window(1,'com.other')])
        client.content_at=time.monotonic()
        with self.assertRaisesRegex(RuntimeError,'게임 창 대기'): client.find_window()

    def test_desktop_roi_migrates_at_negative_monitor_origin(self):
        roi=region_from_desktop(dict(left=-1800,top=760,width=200,height=20),(-1920,0,1920,1080))
        self.assertAlmostEqual(roi['left'],120/1920)
        self.assertAlmostEqual(roi['top'],760/1080)
        self.assertTrue(valid_roi(roi))

    def test_one_pixel_selection_border_is_clipped(self):
        roi=region_from_desktop(dict(left=567,top=1220,width=141,height=19),(3,522,1242,716))
        self.assertAlmostEqual(roi['top']+roi['height'],1)

    def test_unrelated_desktop_selection_requires_new_selection(self):
        with self.assertRaises(ValueError):
            region_from_desktop(dict(left=0,top=0,width=200,height=20),(400,400,1000,600))

    def test_bad_window_settings_preserve_level_and_other_fields(self):
        for invalid in ({'left':0,'top':0,'width':2,'height':.1,'reference_aspect':1.7},
                        {'left':True,'top':0,'width':.1,'height':.1,'reference_aspect':1.7},
                        {'left':0,'top':0,'width':.1,'height':.1,'reference_aspect':float('nan')}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'settings.json'
                path.write_text(json.dumps({'level':170,'window_region':invalid}))
                with patch.object(tracker,'BASE',Path(folder)):
                    clean,warning=tracker.load_settings(path)
                self.assertEqual(clean['level'],170)
                self.assertNotIn('window_region',clean)
                self.assertIsNotNone(warning)

    def test_window_roi_is_restored_from_settings(self):
        roi=dict(left=.2,top=.9,width=.2,height=.02,reference_aspect=1.7)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json'
            path.write_text(json.dumps({'level':170,'window_region':roi}))
            clean,warning=tracker.load_settings(path)
            self.assertEqual(clean['window_region'],roi)
            self.assertIsNone(warning)
