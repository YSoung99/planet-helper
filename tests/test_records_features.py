"""Current character, period summaries, spot spellings and record backups."""
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import map_name
import record_history
import records_backup
import tracker


def write_record(base, level=170, character=None, gain=5000):
    """A real saved record through the same writer the app uses."""
    Path(base).mkdir(parents=True, exist_ok=True)
    with patch.object(tracker, 'BASE', Path(base)):
        session = tracker.Session(level=level)
        for stamp, value in ((0, (219671043, 51.32)), (1, (219671043, 51.32)),
                             (2, (219671043+gain, 51.32)), (62, (219671043+gain*2, 51.33))):
            session.accept(value, stamp)
        writer = tracker.App.__new__(tracker.App)
        writer.session = session
        if character: writer.current_character = character
        with patch.object(tracker.time, 'monotonic', return_value=62):
            writer.save_observation()
    return session.session_id


def row(session_id, started, memo='', character=None, gained=6000, rate=600, level_ups=0):
    return dict(session_id=session_id, started=started, level=122, active=600, idle=0,
                character_name=character, gained=gained, per_minute=rate, rate_gain=rate*10,
                rate_seconds=600, blocked=False, memo=memo, level_ups=level_ups)


class CurrentCharacterTests(unittest.TestCase):
    def test_setting_round_trip_and_invalid_values(self):
        # log_error writes to the real data folder unless patched.
        with tempfile.TemporaryDirectory() as base, patch.object(tracker, 'log_error'):
            path = Path(base)/'settings.json'
            path.write_text(json.dumps({'current_character': '  곰곰  스렁 '}), encoding='utf-8')
            self.assertEqual(tracker.load_settings(path)[0]['current_character'], '곰곰 스렁')
            for bad in (12, '   ', 'x'*25):
                path.write_text(json.dumps({'current_character': bad}), encoding='utf-8')
                config, warning = tracker.load_settings(path)
                self.assertNotIn('current_character', config)
                self.assertIsNotNone(warning)

    def test_new_record_carries_the_assigned_name_below_ocr_and_edits(self):
        with tempfile.TemporaryDirectory() as base:
            assigned = write_record(base, character='곰곰스렁')
            plain = write_record(base)
            summary = json.loads((Path(base)/'records'/f'{assigned}.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['character_assigned'], '곰곰스렁')
            rows = {r['session_id']: r for r in record_history.collect(base)}
            self.assertEqual(rows[assigned]['character_name'], '곰곰스렁')
            self.assertIsNone(rows[plain]['character_name'])
            record_history.save_character_names({assigned: '부캐'}, base)
            rows = {r['session_id']: r for r in record_history.collect(base)}
            self.assertEqual(rows[assigned]['character_name'], '부캐')
            self.assertEqual(record_history.known_characters(base), ['곰곰스렁', '부캐'])


class PeriodSummaryTests(unittest.TestCase):
    def test_today_and_week_totals(self):
        now = datetime(2026, 11, 18, 21, 0)   # a Wednesday
        rows = [row('a', datetime(2026, 11, 18, 20, 0), '사자왕의 성', '곰곰스렁', rate=900, level_ups=1),
                row('b', datetime(2026, 11, 18, 19, 0), '사자왕의성', '곰곰스렁', rate=300),
                row('c', datetime(2026, 11, 16, 20, 0), '미니던전', None, rate=500),
                row('d', datetime(2026, 11, 15, 20, 0), '예전', None, rate=9999)]   # Sunday, last week
        today = record_history.period_summary(rows, datetime(2026, 11, 18))
        self.assertEqual((today['count'], today['active'], today['gained'], today['level_ups']), (2, 1200, 12000, 1))
        self.assertEqual(today['per_minute'], 600)
        self.assertEqual(today['best_spot'][0], '사자왕의 성')   # spacing variants merged
        week = record_history.period_summary(rows, datetime(2026, 11, 16))
        self.assertEqual(week['count'], 3)
        text = record_history.period_text(rows, now)
        self.assertIn('오늘 · 2회', text)
        self.assertIn('이번 주 (월요일부터) · 3회', text)
        self.assertNotIn('예전', text)
        self.assertIn('오늘 · 사냥 기록 없음', record_history.period_text([], now))


class SpotSpellingTests(unittest.TestCase):
    def test_spacing_variants_share_one_average_under_the_common_spelling(self):
        rows = [row('a', None, '사자왕의 성 · 낮은 성벽2', rate=600),
                row('b', None, '사자왕의성 · 낮은 성벽2', rate=300),
                row('c', None, '사자왕의 성 · 낮은 성벽2', rate=300),
                row('d', None, '사자왕의 성 · 낮은 성벽3', rate=100)]
        ranked = record_history.by_memo(rows)
        self.assertEqual([(name, count) for name, _, _, count in ranked],
                         [('사자왕의 성 · 낮은 성벽2', 3), ('사자왕의 성 · 낮은 성벽3', 1)])

    def test_new_ocr_reading_reuses_existing_spelling(self):
        with tempfile.TemporaryDirectory() as base:
            record_history.save_memos({'1': '사자왕의 성 · 낮은 성벽2', '2': '커닝 스퀘어 · 7층 8층 A구역'}, base)
            self.assertEqual(map_name.resolve('사자왕의성 · 낮은 성벽2', base), '사자왕의 성 · 낮은 성벽2')
            self.assertEqual(map_name.resolve('커닝 스퀘어 · 7층8층A구역', base), '커닝 스퀘어 · 7층 8층 A구역')
            self.assertEqual(map_name.resolve('처음 보는 곳', base), '처음 보는 곳')
            # A confirmed correction still wins.
            map_name.learn_alias('사자왕의성 · 낮은 성벽2', '직접 고친 이름', base)
            self.assertEqual(map_name.resolve('사자왕의성 · 낮은 성벽2', base), '직접 고친 이름')


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home, self.other = Path(self.directory.name)/'home', Path(self.directory.name)/'other'
        self.first = write_record(self.home, character='곰곰스렁')
        record_history.save_memos({self.first: '사자왕의 성'}, self.home)
        (self.home/'records'/f'{self.first}-audit.jsonl').write_text('{}\n', encoding='utf-8')

    def test_export_holds_records_and_names_but_not_diagnostics(self):
        target = self.home/'out'/'backup.zip'
        self.assertEqual(records_backup.export_zip(target, self.home), 1)
        names = zipfile.ZipFile(target).namelist()
        self.assertIn(f'records/{self.first}.csv', names)
        self.assertIn('records/memos.json', names)
        self.assertFalse(any(name.endswith('.jsonl') for name in names))

    def test_import_adds_only_what_is_missing(self):
        second = write_record(self.other, gain=7000)
        record_history.save_memos({second: '미니던전'}, self.other)
        record_history.save_character_names({self.first: '다른컴퓨터이름'}, self.other)
        local_csv = (self.home/'records'/f'{self.first}.csv').read_bytes()
        # The other computer also has a different copy of the first record.
        (self.other/'records'/f'{self.first}.csv').write_text('changed', encoding='utf-8')
        backup = Path(self.directory.name)/'other.zip'
        records_backup.export_zip(backup, self.other)
        added, skipped, names = records_backup.import_zip(backup, self.home)
        self.assertEqual((added, skipped), (1, 1))
        self.assertEqual((self.home/'records'/f'{self.first}.csv').read_bytes(), local_csv)
        self.assertTrue((self.home/'records'/f'{second}.csv').exists())
        memos = record_history.load_memos(self.home)
        self.assertEqual(memos, {self.first: '사자왕의 성', second: '미니던전'})
        self.assertEqual(record_history.load_character_names(self.home), {self.first: '다른컴퓨터이름'})
        self.assertEqual(names, 2)
        self.assertEqual(records_backup.import_zip(backup, self.home), (0, 2, 0))

    def test_import_rejects_foreign_zips_and_ignores_unsafe_paths(self):
        foreign = Path(self.directory.name)/'foreign.zip'
        with zipfile.ZipFile(foreign, 'w') as archive:
            archive.writestr('records/20261118-200000-1.csv', 'x')
        with self.assertRaises(ValueError):
            records_backup.import_zip(foreign, self.home)
        unsafe = Path(self.directory.name)/'unsafe.zip'
        with zipfile.ZipFile(unsafe, 'w') as archive:
            archive.writestr('planet-helper-backup.json', '{}')
            archive.writestr('../evil.csv', 'x')
            archive.writestr('records/../../20261118-200000-2.csv', 'x')
            archive.writestr('records/settings.json', '{}')
        self.assertEqual(records_backup.import_zip(unsafe, self.home), (0, 0, 0))
        self.assertFalse((Path(self.directory.name)/'evil.csv').exists())
        self.assertFalse((self.home/'records'/'settings.json').exists())

    def test_auto_backup_runs_once_a_day_and_keeps_the_newest(self):
        for day in range(1, 5):
            records_backup.auto_backup(self.home, keep=2, today=datetime(2026, 11, day))
        self.assertIsNone(records_backup.auto_backup(self.home, keep=2, today=datetime(2026, 11, 4)))
        kept = sorted(path.name for path in (self.home/'records-backups').iterdir())
        self.assertEqual(kept, ['records-20261103.zip', 'records-20261104.zip'])


if __name__ == '__main__':
    unittest.main()
