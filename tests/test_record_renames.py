import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import record_history
import tracker


def record(session_id, character=None, memo_rate=600):
    return dict(session_id=session_id, started=datetime(2026, 11, 18, 20, 0), level=122, active=600, idle=0,
                character_name=character, exp_buff_intervals={}, gained=6000, per_minute=memo_rate,
                rate_gain=6000, rate_seconds=600, gap=0, gap_gain=0, blocked=False, stop_reason='',
                changes=[])


class CharacterNameStorageTests(unittest.TestCase):
    def test_bulk_names_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as base:
            saved = record_history.save_character_names({'a': '  곰곰  스렁 ', 'b': '곰곰스렁'}, base)
            self.assertEqual(saved, {'a': '곰곰 스렁', 'b': '곰곰스렁'})
            record_history.save_character_names({'a': ''}, base)
            self.assertEqual(record_history.load_character_names(base), {'b': '곰곰스렁'})

    def test_bulk_memos_share_the_single_memo_rules(self):
        with tempfile.TemporaryDirectory() as base:
            record_history.save_memos({'a': '사자왕의 성 · 낮은 성벽2', 'b': '사자왕의 성 · 낮은 성벽2'}, base)
            record_history.save_memo('a', '', base)
            self.assertEqual(record_history.load_memos(base), {'b': '사자왕의 성 · 낮은 성벽2'})

    def test_collect_prefers_the_typed_name_and_keeps_the_detected_one(self):
        with tempfile.TemporaryDirectory() as base:
            folder = Path(base)/'records'
            folder.mkdir()
            for name in ('20261118-200000-1', '20261118-210000-2'):
                (folder/f'{name}.csv').write_text('', encoding='utf-8')
            detected = {'20261118-200000-1': None, '20261118-210000-2': 'OCR이름'}
            record_history.save_character_names({'20261118-200000-1': '곰곰스렁'}, base)
            with patch.object(record_history, 'summarize',
                              lambda path: record(path.stem, detected[path.stem])):
                rows = {row['session_id']: row for row in record_history.collect(base)}
            self.assertEqual(rows['20261118-200000-1']['character_name'], '곰곰스렁')
            self.assertIsNone(rows['20261118-200000-1']['detected_character'])
            self.assertEqual(rows['20261118-210000-2']['character_name'], 'OCR이름')


class RecordDialogRenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        self.addCleanup(self.base.cleanup)
        (Path(self.base.name)/'records').mkdir()
        patcher = patch.object(tracker, 'BASE', Path(self.base.name))
        patcher.start(); self.addCleanup(patcher.stop)
        rows = [record('20261118-210000-2', None), record('20261118-200000-1', None),
                record('20261117-200000-3', '곰곰스렁')]
        def collect(base=None):
            names = record_history.load_character_names(base)
            memos = record_history.load_memos(base)
            fresh = [dict(row) for row in rows]
            for row in fresh:
                row['detected_character'] = row['character_name']
                row['character_name'] = names.get(row['session_id'], row['character_name'])
                row['memo'] = memos.get(row['session_id'], '')
            return fresh
        patcher = patch.object(record_history, 'collect', collect)
        patcher.start(); self.addCleanup(patcher.stop)
        controller = SimpleNamespace(window=None, session=SimpleNamespace(character_name=None))
        self.dialog = record_history.build_dialog(controller)
        self.addCleanup(self.dialog.close)
        from PySide6.QtWidgets import QTableWidget, QComboBox
        self.table = self.dialog.findChild(QTableWidget)
        self.picker = self.dialog.findChild(QComboBox)

    def settle(self):
        for _ in range(3): self.app.processEvents()

    def picker_labels(self):
        return [self.picker.itemText(i) for i in range(self.picker.count())]

    def test_double_click_edit_of_character_cell_saves_and_updates_filter(self):
        cell = self.table.item(0, 1)
        self.assertTrue(cell.flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertFalse(self.table.item(0, 2).flags() & Qt.ItemFlag.ItemIsEditable)
        cell.setText('새캐릭')
        self.settle()
        self.assertEqual(record_history.load_character_names(), {'20261118-210000-2': '새캐릭'})
        self.assertIn('새캐릭', self.picker_labels())
        self.assertIn('캐릭터 미확인', self.picker_labels())

    def test_clearing_the_cell_falls_back_to_the_detected_name(self):
        record_history.save_character_names({'20261117-200000-3': '임시'})
        self.dialog.close()
        controller = SimpleNamespace(window=None, session=SimpleNamespace(character_name=None))
        self.dialog = record_history.build_dialog(controller)
        from PySide6.QtWidgets import QTableWidget
        self.table = self.dialog.findChild(QTableWidget)
        self.table.item(2, 1).setText('')
        self.settle()
        self.assertEqual(record_history.load_character_names(), {})
        self.assertEqual(self.table.item(2, 1).text(), '곰곰스렁')

    def bulk(self, rows, label_start, answer):
        # Qt's own exec() would block offscreen, so the dialog gets plain fakes.
        class FakeAction:
            def __init__(self, text): self.label = text
            def text(self): return self.label

        class FakeMenu:
            def __init__(self, parent): self.items = []
            def addAction(self, text):
                self.items.append(FakeAction(text)); return self.items[-1]
            def exec(self, position):
                return next(action for action in self.items if action.text().startswith(label_start))

        class FakeInput:
            @staticmethod
            def getItem(*args): return answer, True

        self.dialog.close()
        controller = SimpleNamespace(window=None, session=SimpleNamespace(character_name=None))
        with patch('PySide6.QtWidgets.QMenu', FakeMenu), patch('PySide6.QtWidgets.QInputDialog', FakeInput):
            self.dialog = record_history.build_dialog(controller)
        from PySide6.QtWidgets import QTableWidget, QComboBox
        self.table = self.dialog.findChild(QTableWidget)
        self.picker = self.dialog.findChild(QComboBox)
        selection = self.table.selectionModel()
        for row in rows:
            selection.select(self.table.model().index(row, 0),
                             selection.SelectionFlag.Select | selection.SelectionFlag.Rows)
        self.table.customContextMenuRequested.emit(self.table.visualItemRect(self.table.item(rows[0], 0)).center())
        self.settle()

    def test_right_click_renames_every_selected_record(self):
        self.bulk([0, 1], '캐릭터 이름 바꾸기', '곰곰스렁')
        self.assertEqual(record_history.load_character_names(),
                         {'20261118-210000-2': '곰곰스렁', '20261118-200000-1': '곰곰스렁'})
        self.assertNotIn('캐릭터 미확인', self.picker_labels())
        self.bulk([0, 1, 2], '사냥터 메모 바꾸기', '사자왕의 성 · 낮은 성벽2')
        self.assertEqual(set(record_history.load_memos().values()), {'사자왕의 성 · 낮은 성벽2'})
        self.assertEqual(len(record_history.load_memos()), 3)
        self.bulk([0], '캐릭터 이름을 자동 인식 값으로', None)
        self.assertEqual(record_history.load_character_names(), {'20261118-200000-1': '곰곰스렁'})

    def test_memo_edit_still_saves(self):
        self.table.item(1, 12).setText('사자왕의 성 · 낮은 성벽2')
        self.settle()
        self.assertEqual(record_history.load_memos(), {'20261118-200000-1': '사자왕의 성 · 낮은 성벽2'})


if __name__ == '__main__':
    unittest.main()
