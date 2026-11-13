import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qt_controller
import tracker


class ResilienceTests(unittest.TestCase):
    def test_damaged_settings_allow_startup_with_a_visible_warning(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            path = Path(folder)/'settings.json'
            self.assertEqual(tracker.load_settings(path), ({}, None))
            for damaged in ('[]', 'null', '{"region":', '\ufffd'):
                with self.subTest(damaged=damaged):
                    path.write_text(damaged, encoding='utf-8')
                    config, warning = tracker.load_settings(path)
                    self.assertEqual(config, {})
                    self.assertTrue(warning)
                    self.assertIn('설정 읽기', (Path(folder)/'last-error.log').read_text())

    def test_invalid_roi_is_removed_without_losing_valid_level_or_position(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            path = Path(folder)/'settings.json'
            for region in ([], {'left': 0, 'top': 0, 'width': -1, 'height': 20},
                           {'left': 0, 'top': 0, 'width': '185', 'height': 23}):
                with self.subTest(region=region):
                    path.write_text(json.dumps({'region': region, 'level': 169, 'position': [-200, 20]}))
                    config, warning = tracker.load_settings(path)
                    self.assertNotIn('region', config)
                    self.assertEqual(config['position'], [-200, 20])
                    self.assertEqual(config['level'], 169)
                    self.assertTrue(warning)

    def test_failed_export_preserves_existing_file_and_reports_the_error(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            target = Path(folder)/'export.csv'
            target.write_text('previous export')
            messages = []
            app = qt_controller.SharedController.__new__(qt_controller.SharedController)
            app.window = None
            app.status = SimpleNamespace(set=messages.append)
            app.session = tracker.Session()
            app.session.accept((1000, 10), -1)
            app.session.accept((1000, 10), 0)
            with patch.object(qt_controller.QFileDialog, 'getSaveFileName', return_value=(str(target), 'CSV (*.csv)')), \
                    patch.object(Path, 'replace', side_effect=PermissionError('read only destination')):
                app.export()
            self.assertEqual(target.read_text(), 'previous export')
            self.assertIn('CSV 저장 실패', messages[-1])
            self.assertIn('PermissionError', (Path(folder)/'last-error.log').read_text())
            self.assertEqual(list(Path(folder).glob('.planet-exp-*.tmp')), [])
            with patch.object(qt_controller.QFileDialog, 'getSaveFileName', return_value=(str(target), 'CSV (*.csv)')):
                app.export()
            with target.open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[1][1:3], ['1000', '10'])
            self.assertEqual(messages[-1], 'CSV 저장 완료')

    def test_failed_settings_replace_preserves_previous_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'BASE', Path(folder)):
            path = Path(folder)/'settings.json'
            path.write_text('{"level":169}')
            messages = []
            app = qt_controller.SharedController.__new__(qt_controller.SharedController)
            app.window = SimpleNamespace(x=lambda: 3, y=lambda: 542)
            app.region = {'left': 683, 'top': 1370, 'width': 185, 'height': 23}
            app.window_region, app.compact, app.always_on_top = None, True, True
            app.window_opacity, app.auto_hud = .9, True
            app.session = tracker.Session(level=169)
            app.status = SimpleNamespace(set=messages.append)
            with patch.object(Path, 'replace', side_effect=PermissionError('read only destination')):
                app.save_config()
            self.assertEqual(json.loads(path.read_text()), {'level': 169})
            self.assertIn('저장 실패', messages[-1])
            app.save_config()
            config, warning = tracker.load_settings(path)
            self.assertIsNone(warning)
            self.assertEqual(config['region'], app.region)
            self.assertEqual(config['position'], [3, 542])


if __name__ == '__main__':
    unittest.main()
