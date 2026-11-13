import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unified_data import DataLock, prepare_data, file_hash


class UnifiedDataTests(unittest.TestCase):
    def test_import_preserves_records_settings_and_excludes_live_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy, target = Path(folder) / 'old', Path(folder) / 'new'
            (legacy / 'records' / 'start-captures').mkdir(parents=True)
            for name, data in [('settings.json', b'{"level":170}'),
                               ('records/one.csv', b'old csv\r\n'),
                               ('records/start-captures/one.png', b'png'),
                               ('pending-update.json', b'live')]:
                (legacy / name).write_bytes(data)
            before = {str(p.relative_to(legacy)): file_hash(p) for p in legacy.rglob('*') if p.is_file()}
            report = prepare_data(target, legacy)
            self.assertFalse((target / 'pending-update.json').exists())
            for relative, digest in report['sha256'].items():
                self.assertEqual(file_hash(target / relative), digest)
            self.assertEqual(before, {str(p.relative_to(legacy)): file_hash(p) for p in legacy.rglob('*') if p.is_file()})
            (target / 'settings.json').write_text('{"level":171}')
            prepare_data(target, legacy)
            self.assertEqual(json.loads((target / 'settings.json').read_text()), {'level': 171})

    def test_existing_data_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy, target = Path(folder) / 'old', Path(folder) / 'new'
            legacy.mkdir(); target.mkdir()
            (legacy / 'settings.json').write_text('old')
            (target / 'settings.json').write_text('new')
            report = prepare_data(target, legacy)
            self.assertEqual((target / 'settings.json').read_text(), 'new')
            self.assertIn('settings.json', report['existing'])

    def test_second_process_cannot_write_until_lock_owner_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = DataLock(folder)
            self.assertTrue(lock.acquire())
            script = 'from unified_data import DataLock; import sys; lock=DataLock(sys.argv[1]); sys.exit(0 if lock.acquire() else 7)'
            def run():
                return subprocess.run([sys.executable, '-c', script, folder], cwd=Path(__file__).resolve().parents[1]).returncode
            self.assertEqual(run(), 7)
            lock.close()
            self.assertEqual(run(), 0)
