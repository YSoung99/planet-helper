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
from linux_app import Controller


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
        with patch('linux_app.native_overlay'):
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
