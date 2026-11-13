import sys as _sys
import unittest as _unittest
if not _sys.platform.startswith('linux'):
    raise _unittest.SkipTest('Linux-only (X11/evdev) tests')
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
import linux_app,tracker
from linux_key_repeat import ConfiguredRepeater,KeyRepeatBridge,DEFAULT_SETTINGS,E
from linux_capture import WindowCapture,pixel_rect

class InputTests(unittest.TestCase):
    def setUp(self):
        self.events=[]; self.now=0.;self.focus=True
        output=SimpleNamespace(write=lambda *event:self.events.append(event),syn=lambda:None)
        self.r=ConfiguredRepeater(output,lambda:self.focus,dict(DEFAULT_SETTINGS),lambda:self.now)
    def test_release_and_focus_loss_release_all_injected_keys(self):
        self.assertTrue(self.r.press('keyboard',code=E.KEY_A))
        self.assertTrue(self.r.states[E.KEY_A].down)
        self.r.release('keyboard',code=E.KEY_A)
        self.assertFalse(self.r.down)
        self.r.press('keyboard',code=E.KEY_ENTER)
        self.focus=False;self.r.tick()
        self.assertFalse(self.r.down);self.assertFalse(self.r.running)
        self.assertEqual(self.events[-1],(E.EV_KEY,E.KEY_ENTER,0))
    def test_delay_and_no_input_when_disabled_or_not_game(self):
        self.r.enabled=False;self.assertFalse(self.r.press('k'));self.assertEqual(self.events,[])
        self.r.enabled=True;self.focus=False;self.assertFalse(self.r.press('k'))
        self.focus=True;self.r.press('k');self.now=.22;self.r.tick();self.assertTrue(self.r.down)
        self.now=.231;self.r.tick();self.assertFalse(self.r.down)
        self.now=.262;self.r.tick();self.assertTrue(self.r.down)
        self.r.stop(disable=True);self.assertFalse(self.r.down)
    def test_bridge_start_does_not_open_input_devices(self):
        with tempfile.TemporaryDirectory() as d,patch('linux_key_repeat.UInput') as output:
            bridge=KeyRepeatBridge(d);bridge.start()
            self.assertFalse(bridge.state['enabled']);output.assert_not_called();bridge.shutdown()
    def test_capture_refuses_inactive_game_before_reading_screen(self):
        capture=WindowCapture.__new__(WindowCapture)
        capture.require_active = True
        capture.composite_depths = set()
        capture.display=SimpleNamespace(intern_atom=lambda name:name)
        capture.root=SimpleNamespace(get_full_property=lambda *args:SimpleNamespace(value=[2]))
        capture.game=lambda:(SimpleNamespace(id=1),dict(left=0,top=0,width=100,height=100))
        with patch.object(tracker,'capture_image') as read:
            with self.assertRaisesRegex(RuntimeError,'활성화'):capture.snapshot(roi=dict(left=0,top=0,width=1,height=1))
            with self.assertRaisesRegex(RuntimeError,'활성화'):capture.snapshot()
            read.assert_not_called()

    def test_window_capture_reads_the_hud_without_desktop_focus(self):
        from PIL import Image
        capture = WindowCapture.__new__(WindowCapture)
        window = SimpleNamespace(id=1)
        bounds = dict(left=40, top=20, width=200, height=100)
        capture.game = lambda: (window, bounds)
        roi = dict(left=.1, top=.8, width=.5, height=.1, reference_aspect=2)
        full = Image.new('RGB', (200, 100))
        with patch.object(capture, 'window_image', return_value=full) as read, \
                patch.object(capture, 'desktop_snapshot') as desktop:
            frame = capture.snapshot(roi=roi, include_full=True)
        read.assert_called_once_with(window, (200, 100), (0, 0, 200, 100))
        desktop.assert_not_called()
        self.assertEqual(frame.size, (100, 10))
        self.assertEqual(frame.info['capture_backend'], 'xcomposite-game-window')
        self.assertIs(frame.info['full_game_image'], full)
    def test_roi_tracks_geometry_and_rejects_changed_aspect(self):
        roi=dict(left=.1,top=.8,width=.5,height=.1,reference_aspect=2)
        self.assertEqual(pixel_rect(roi,1000,500),(100,400,500,50))
        self.assertEqual(pixel_rect(roi,2000,1000),(200,800,1000,100))
        with self.assertRaises(RuntimeError):pixel_rect(roi,1000,1000)

class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_records_and_display_survive_restart_repeat_remains_off(self):
        with tempfile.TemporaryDirectory() as d,patch.object(tracker,'BASE',Path(d)):
            c=linux_app.Controller(self.app,start_worker=False,restore=False)
            c.session.accept((172680132,42.55),0);c.session.accept((172680132,42.55),1)
            c.session.accept((172680232,42.55),2)
            c.save_observation();c.window_opacity=.7;c.toggle_display();c.toggle_pin(False)
            self.assertTrue(c.shutdown())
            restored=linux_app.Controller(self.app,start_worker=False,restore=True)
            self.assertEqual(restored.session.stats(3)[0],100)
            self.assertFalse(restored.compact);self.assertFalse(restored.always_on_top)
            self.assertEqual(restored.window_opacity,.7)
            self.assertFalse(restored.running);self.assertFalse(restored.key_repeat.state['enabled'])
            restored.shutdown()

class LevelDiagnosticTests(unittest.TestCase):
    def test_actual_reading_is_rejected_at_169_and_valid_at_171(self):
        from temporal_ocr import decide
        frames=[dict(value=(89714269,19.87),score=.97338) for _ in range(3)]
        self.assertIsNone(decide(frames,tracker.EXP_TABLE[169],169)[0])
        self.assertIsNotNone(decide(frames,tracker.EXP_TABLE[171],171)[0])
        details=dict(temporal=dict(frames=frames))
        hint=linux_app.level_mismatch(details,169,tracker.EXP_TABLE[169])
        self.assertIn('Lv.169',hint);self.assertIn('Lv.171',hint)
        self.assertIsNone(linux_app.level_mismatch(details,171,tracker.EXP_TABLE[171]))
