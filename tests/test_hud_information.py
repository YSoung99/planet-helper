import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from auto_hud import Confirmation, AutomaticCapture, HudNotFound
from hud_information import HudInformation
from test_auto_hud import hud
import tracker
from mac_app import Controller


class InformationTests(unittest.TestCase):
    def observe(self, info, kind='new', stamp=10, generation=1):
        info.observe(dict(ui_kind=kind, game_image_size=(2628, 1642), auto_confirmed=True,
                          auto_detected=True, ui_verified_at=stamp), generation, (100, 1), None, stamp)

    def test_verified_ui_updates_and_stale_reading_is_never_shown_as_current(self):
        info = HudInformation()
        self.assertEqual(info.text(running=True, automatic=True, generation=1, now=10), '인식 UI: 확인 중')
        self.observe(info)
        self.assertEqual(info.text(running=True, automatic=True, generation=1, now=12), '인식 UI: 새로운 UI')
        self.assertIn('재확인 중', info.text(running=True, automatic=True, generation=1, now=25))
        self.assertIn('재확인 중', info.text(running=True, automatic=True, generation=2, now=12))
        self.assertIn('측정 정지', info.text(running=False, automatic=True, generation=1, now=12))
        self.assertIn('UI 미확인', info.text(running=True, automatic=False, generation=1, now=12))

    def test_failed_discovery_shows_reconfirmation_until_new_ui_is_verified(self):
        info = HudInformation(); self.observe(info)
        info.observe({}, 1, None, 'looking', 11)
        self.assertIn('재확인 중', info.text(running=True, automatic=True, generation=1, now=11))
        self.observe(info, 'classic', 12)
        self.assertEqual(info.text(running=True, automatic=True, generation=1, now=12), '인식 UI: 클래식 UI')

    def test_ui_switch_requires_three_new_confirmations_even_if_values_are_identical(self):
        gate = Confirmation(); classic = hud(ui_kind='new'); modern = replace(classic, ui_kind='classic')
        for stamp in (1, 2, 3): gate.accept(classic, stamp)
        self.assertFalse(gate.accept(modern, 4))
        self.assertFalse(gate.accept(modern, 5))
        self.assertTrue(gate.accept(modern, 6))

    def test_unconfirmed_or_unknown_style_does_not_override_confirmed_information(self):
        info = HudInformation(); self.observe(info)
        info.observe(dict(ui_kind='classic', auto_detected=True), 1, (200, 2), None, 11)
        self.assertEqual(info.ui_kind, 'new')
        self.observe(info, 'unknown', 12)
        self.assertEqual(info.ui_kind, 'new')
        self.assertIn('재확인 중', info.text(running=True, automatic=True, generation=1, now=12))

    def test_live_capture_state_reconfirms_ui_switch_without_changing_generation(self):
        from PIL import Image
        from unittest.mock import Mock
        image = Image.new('RGB', (1600, 1000)); image.info['window_id'] = 100
        client = Mock(); client.snapshot.return_value = image
        locator = Mock(); locator.locate.return_value = hud(ui_kind='new')
        now = [1]
        capture = AutomaticCapture(client, locator, lambda: now[0])
        for stamp in (1, 2):
            now[0] = stamp
            with self.assertRaises(HudNotFound): capture.read(7, 170, False, True)
        now[0] = 3; first = capture.read(7, 170, False, True)
        self.assertEqual(first.info['ui_kind'], 'new')
        locator.locate.return_value = hud(ui_kind='classic', exp_roi=dict(hud().exp_roi, top=.90))
        for stamp in (8, 9):
            now[0] = stamp
            with self.assertRaises(HudNotFound): capture.read(7, 170, True, False)
        now[0] = 10; second = capture.read(7, 170, True, False)
        self.assertEqual(second.info['ui_kind'], 'classic')
        self.assertEqual(capture.generation, 7)
        self.assertNotIn('full_game_image', second.info)


class InformationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(tracker, 'BASE', Path(self.temp.name)); self.patch.start()
        self.c = Controller(self.app, start_worker=False, restore=False); self.c.timer.stop()
    def tearDown(self):
        if self.c.info_dialog is not None:
            self.c.info_dialog.close()
        self.c.key_repeat.shutdown(); self.c.start_captures.close(); self.c.window.hide()
        self.c.window.deleteLater(); self.app.processEvents(); self.patch.stop(); self.temp.cleanup()

    def test_information_button_opens_one_live_dialog_and_tracks_real_observation_metadata(self):
        c = self.c; c.running = True
        c.events.put((c.generation, (34514347, 8.06), 'fixture', .99, None, 1,
                      dict(detected_level=170, auto_detected=True, auto_confirmed=True,
                           ui_kind='classic', game_image_size=(2484,1432), temporal={'selected':1})))
        c.poll()
        self.assertEqual(c.ui_label.text(), '인식 UI: 클래식 UI')
        with patch('mac_app.native_overlay'):
            c.show_information(); first = c.info_dialog
            c.show_information(); self.assertIs(c.info_dialog, first)
        self.assertTrue(first.isVisible())
        from PySide6.QtWidgets import QLabel
        text = '\n'.join(label.text() for label in first.findChildren(QLabel))
        self.assertIn('인식 UI: 클래식 UI', text)
        self.assertIn('2484 × 1432', text)
        c.events.put((c.generation, None, '', 0, 'looking', 2, {})); c.poll()
        self.assertIn('재확인 중', c.ui_label.text())

    def test_same_level_ui_switch_keeps_baseline_and_gain(self):
        c = self.c; c.running = True; generation = c.generation
        for stamp, kind, value in [(1,'new',(34514347,8.06)), (2,'classic',(34515347,8.06))]:
            c.events.put((generation,value,'fixture',.99,None,stamp,
                          dict(detected_level=170,auto_detected=True,auto_confirmed=True,
                               ui_kind=kind,game_image_size=(2484,1432),temporal={'selected':1})))
            c.poll()
        self.assertEqual(c.session.first,(34514347,8.06))
        self.assertEqual(c.session.stats(2)[0],1000)
        self.assertEqual(c.generation,generation)
        self.assertEqual(c.ui_label.text(),'인식 UI: 클래식 UI')
