"""A damaged latest record must not prevent recovery of an older valid one."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tracker


class RecordRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.patch = patch.object(tracker, 'BASE', self.base)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        saved = tracker.Session(level=170)
        for stamp, value in ((0, (219671043, 51.32)),
                             (1, (219671043, 51.32)),
                             (2, (219676043, 51.32))):
            saved.accept(value, stamp)
        writer = tracker.App.__new__(tracker.App)
        writer.session = saved
        with patch.object(tracker.time, 'monotonic', return_value=2):
            writer.save_observation()
        self.good = self.base/'records'/(saved.session_id+'.csv')
        os.utime(self.good, (100, 100))
        self.saved_id = saved.session_id
        self.app = tracker.App.__new__(tracker.App)
        self.app.session = tracker.Session(level=170)
        self.messages = []
        self.app.value = self.app.status = SimpleNamespace(set=self.messages.append)

    def damaged_latest(self, payload):
        latest = self.good.with_name('newest-damaged.csv')
        latest.write_bytes(self.good.read_bytes())
        latest.with_suffix('.json').write_text(json.dumps(payload), encoding='utf-8')
        os.utime(latest, (200, 200))
        return latest

    def snapshot(self):
        return {p.name:p.read_bytes() for p in self.good.parent.iterdir() if p.is_file()}

    def assert_good_recovery(self):
        self.assertTrue(self.app.session.restored)
        self.assertEqual(self.app.session.session_id, self.saved_id)
        self.assertEqual(self.app.session.first, (219671043, 51.32))
        self.assertEqual(self.app.session.last, (219676043, 51.32))
        self.assertEqual(self.app.session.stats(100)[0], 5000)

    def test_valid_record_restores_without_modifying_files(self):
        before = self.snapshot()
        self.app.restore_latest_record()
        self.assert_good_recovery()
        self.assertEqual(self.snapshot(), before)

    def test_wrong_json_root_skips_to_older_valid_record(self):
        for payload in ([], None, 1, 'broken metadata', True):
            with self.subTest(payload=payload):
                self.damaged_latest(payload)
                self.app.session = tracker.Session(level=170)
                before = self.snapshot()
                self.app.restore_latest_record()
                self.assert_good_recovery()
                self.assertEqual(self.snapshot(), before)

    def test_all_records_with_wrong_root_leave_new_session_usable(self):
        latest = self.damaged_latest([])
        self.good.unlink()
        self.good.with_suffix('.json').unlink()
        original = self.app.session
        before = self.snapshot()
        self.app.restore_latest_record()
        self.assertIs(self.app.session, original)
        self.assertFalse(self.app.session.restored)
        self.assertIsNone(self.app.session.first)
        self.assertEqual(latest.with_suffix('.json').read_text(), '[]')
        self.assertEqual(self.snapshot(), before)

    def test_level_selection_still_filters_valid_record_metadata(self):
        latest = self.damaged_latest({'level':171})
        before = self.snapshot()
        self.app.restore_latest_record()
        self.assert_good_recovery()
        self.assertTrue(latest.exists())
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
