import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

import tracker
from mac_app import Controller


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

    def test_menu_groups_actions_and_keeps_frequent_actions_at_the_top(self):
        menu = self.c.build_context_menu()
        self.addCleanup(menu.deleteLater)
        actions = {action.text(): action for action in menu.actions() if not action.isSeparator()}
        self.assertEqual(list(actions), ['사냥 기록 비교', '이번 사냥터 메모', '측정·화면', '게임 인식',
                                         '메소·버프', '기록·백업', '도움말·권한', '종료'])
        records = {a.text() for a in actions['기록·백업'].menu().actions()}
        self.assertTrue({'오늘·이번 주 요약', '백업 파일로 내보내기…',
                         '백업 파일에서 가져오기 (합치기)…', 'CSV 저장…'} <= records)
        recognition = {a.text() for a in actions['게임 인식'].menu().actions()}
        self.assertTrue({'현재 캐릭터: 지정 안 함…', '현재 닉네임 등록·수정…',
                         '경험치 영역 지정…'} <= recognition)

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
