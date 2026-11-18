import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

import qt_controller
import tracker
from mac_app import Controller


class FakeMenu:
    """Records labels; Qt's exec() would block offscreen."""
    labels = []

    def __init__(self, parent=None, title=''):
        if title: FakeMenu.labels.append(title)

    def addAction(self, label, *args):
        FakeMenu.labels.append(label)
        return qt_controller.QAction(label)

    def addMenu(self, title): return FakeMenu(title=title)
    def addSeparator(self): pass
    def exec(self, *args): return None


class RecordsMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = patch.object(tracker, 'BASE', Path(self.temp.name)); self.base.start()
        self.c = Controller(self.app, start_worker=False, restore=False); self.c.timer.stop()

    def tearDown(self):
        self.c.key_repeat.shutdown(); self.c.start_captures.close(); self.c.window.hide(); self.c.window.deleteLater()
        self.app.processEvents(); self.base.stop(); self.temp.cleanup()

    def test_menu_lists_the_new_record_tools(self):
        FakeMenu.labels = []
        with patch.object(qt_controller, 'QMenu', FakeMenu):
            self.c.context_menu(self.c.window.rect().center())
        for label in ('오늘·이번 주 요약', '현재 캐릭터: 지정 안 함…', '기록 백업·옮기기',
                      '백업 파일로 내보내기…', '백업 파일에서 가져오기 (합치기)…'):
            self.assertIn(label, FakeMenu.labels)

    def test_choosing_a_character_is_saved_and_survives_restart(self):
        with patch.object(QInputDialog, 'getItem', staticmethod(lambda *args: ('  곰곰스렁 ', True))):
            self.c.choose_character()
        self.assertEqual(self.c.current_character, '곰곰스렁')
        self.assertEqual(tracker.load_settings(tracker.BASE/'settings.json')[0]['current_character'], '곰곰스렁')
        with patch.object(QInputDialog, 'getItem', staticmethod(lambda *args: ('', True))):
            self.c.choose_character()
        self.assertIsNone(self.c.current_character)

    def test_period_summary_opens_without_records(self):
        shown = []
        with patch.object(QMessageBox, 'information', staticmethod(lambda *args: shown.append(args[2]))):
            self.c.show_period_summary()
        self.assertIn('오늘 · 사냥 기록 없음', shown[0])


if __name__ == '__main__':
    unittest.main()
