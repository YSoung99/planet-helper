"""Cloud sync through rclone, using a local folder as the 'cloud'."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_sync
import record_history
import tracker
from test_records_features import write_record


class RemoteValidationTests(unittest.TestCase):
    def test_remote_shapes(self):
        for good in ('gdrive:planet-helper', 'gdrive:', 'my drive:폴더/planet-helper'):
            self.assertTrue(cloud_sync.valid_remote(good), good)
        for bad in (None, 12, 'no-colon', ':local:/tmp', 'gdrive:../escape', 'gdrive:a\nb', '-gdrive:x'):
            self.assertFalse(cloud_sync.valid_remote(bad), bad)

    def test_settings_keep_only_valid_remotes(self):
        with tempfile.TemporaryDirectory() as base, patch.object(tracker, 'log_error'):
            path = Path(base)/'settings.json'
            path.write_text(json.dumps({'cloud_remote': 'gdrive:planet-helper'}), encoding='utf-8')
            self.assertEqual(tracker.load_settings(path)[0]['cloud_remote'], 'gdrive:planet-helper')
            path.write_text(json.dumps({'cloud_remote': 'gdrive:../x'}), encoding='utf-8')
            self.assertNotIn('cloud_remote', tracker.load_settings(path)[0])

    def test_missing_rclone_is_reported_not_crashed(self):
        with patch.object(cloud_sync, 'rclone', return_value=None):
            self.assertEqual(cloud_sync.remotes(), [])
            with self.assertRaises(RuntimeError):
                cloud_sync.push('gdrive:planet-helper', tempfile.gettempdir())


@unittest.skipUnless(cloud_sync.rclone(), 'rclone not installed')
class LocalCloudTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.cloud = root/'cloud'
        self.env = patch.dict(os.environ, {'RCLONE_CONFIG_TESTCLOUD_TYPE': 'local',
                                           'RCLONE_CONFIG': str(root/'none.conf')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.remote = f'testcloud:{self.cloud}/planet-helper'
        self.desk, self.laptop = root/'desk', root/'laptop'
        self.desk_record = write_record(self.desk, character='곰곰스렁')
        self.laptop_record = write_record(self.laptop, gain=9000)
        record_history.save_memos({self.laptop_record: '미니던전 · 부활하는 기억'}, self.laptop)

    def as_computer(self, name):
        return patch.object(cloud_sync, 'computer_name', return_value=name)

    def test_two_computers_end_up_with_both_records(self):
        self.assertIn('testcloud:', cloud_sync.remotes())
        with self.as_computer('desk'):
            first = cloud_sync.sync(self.remote, self.desk)
        self.assertEqual((first['files'], first['added']), (0, 0))
        self.assertTrue((self.cloud/'planet-helper'/'records-desk.zip').exists())
        with self.as_computer('laptop'):
            second = cloud_sync.sync(self.remote, self.laptop)
        self.assertEqual((second['files'], second['added']), (1, 1))
        with self.as_computer('desk'):
            third = cloud_sync.sync(self.remote, self.desk)
        self.assertEqual((third['files'], third['added']), (1, 1))
        for base in (self.desk, self.laptop):
            ids = {row['session_id'] for row in record_history.collect(base)}
            self.assertEqual(ids, {self.desk_record, self.laptop_record})
        self.assertEqual(record_history.load_memos(self.desk)[self.laptop_record], '미니던전 · 부활하는 기억')
        # Nothing new the next time.
        with self.as_computer('desk'):
            self.assertEqual(cloud_sync.sync(self.remote, self.desk)['added'], 0)
        self.assertIn('새 기록 0개', cloud_sync.describe(cloud_sync.load_state(self.desk)))

    def test_unrelated_files_in_the_cloud_folder_are_ignored(self):
        folder = self.cloud/'planet-helper'
        folder.mkdir(parents=True)
        (folder/'notes.txt').write_text('x', encoding='utf-8')
        (folder/'records-../evil.zip'.replace('/', '_')).write_text('x', encoding='utf-8')
        with self.as_computer('desk'):
            result = cloud_sync.sync(self.remote, self.desk)
        self.assertEqual(result['files'], 0)


if __name__ == '__main__':
    unittest.main()
