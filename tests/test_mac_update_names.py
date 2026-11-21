import plistlib
import subprocess
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

import mac_updates
import tracker

SWAP = Path(__file__).resolve().parent.parent / 'mac_update_swap.sh'


def make_app(folder, name, version='1.4.10'):
    app = Path(folder) / name
    (app / 'Contents').mkdir(parents=True)
    with (app / 'Contents' / 'Info.plist').open('wb') as stream:
        plistlib.dump({'CFBundleShortVersionString': version,
                       'CFBundleIdentifier': mac_updates.BUNDLE_ID}, stream)
    return app


class VersionedNameTests(unittest.TestCase):
    def test_bundle_and_title_carry_the_version(self):
        self.assertEqual(mac_updates.app_name('1.4.10'), '플래닛 도우미 1.4.10.app')
        import qt_controller
        self.assertEqual(qt_controller.SharedController.APP_TITLE, f'플래닛 도우미 {tracker.APP_VERSION}')

    def test_backup_name_does_not_follow_the_version(self):
        for name in ('플래닛 도우미 1.4.10.app', '플래닛 도우미 자동인식 시험용.app'):
            self.assertEqual(mac_updates.previous(Path('/Applications')/name).name, '.플래닛 도우미.previous.app')

    def test_download_accepts_the_single_app_in_the_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            def extract(*args, **kwargs):
                make_app(stage, '플래닛 도우미 1.4.10.app')
            with mock.patch.object(mac_updates, 'download_archive'), \
                 mock.patch.object(mac_updates.subprocess, 'run', side_effect=extract), \
                 mock.patch.object(mac_updates, 'verify') as verify, \
                 mock.patch.object(Path, 'unlink'):
                candidate = mac_updates.download(mac_updates.Release('v1.4.10', '', '', 1), stage)
            self.assertEqual(candidate.name, '플래닛 도우미 1.4.10.app')
            verify.assert_called_once_with(candidate, 'v1.4.10')

    def test_download_refuses_an_archive_with_several_apps(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            def extract(*args, **kwargs):
                make_app(stage, '플래닛 도우미 1.4.10.app')
                make_app(stage, '다른 앱.app')
            with mock.patch.object(mac_updates, 'download_archive'), \
                 mock.patch.object(mac_updates.subprocess, 'run', side_effect=extract):
                with self.assertRaises(ValueError):
                    mac_updates.download(mac_updates.Release('v1.4.10', '', '', 1), stage)


class SwapScriptTests(unittest.TestCase):
    def run_swap(self, root, target, candidate_ok=True, dock=None):
        app = make_app(root, '플래닛 도우미 자동인식 시험용.app', '1.4.9')
        stage = root / 'stage'
        stage.mkdir()
        candidate = make_app(stage, target.name) if candidate_ok else stage / 'missing.app'
        backup = mac_updates.previous(app)
        # "open" is replaced so the test never launches anything.
        log = root / 'opened.txt'
        script = SWAP.read_text().replace('/usr/bin/open -n --env "PLANET_EXP_DATA_DIR=$data"',
                                          f'echo >> "{log}"')
        # The real Dock is never touched: a plist file stands in for its preferences.
        dock = dock or root / 'dock.plist'
        script = script.replace('com.apple.dock', f'"{dock}"').replace('/usr/bin/killall Dock', 'true')
        swap = root / 'swap.sh'
        swap.write_text(script)
        result = subprocess.run(['/bin/sh', str(swap), '999999', str(app), str(candidate), str(backup),
                                 str(stage), str(root / 'data'), str(target)], capture_output=True)
        return app, backup, log, result

    def test_new_version_moves_to_its_own_name_and_keeps_a_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / '플래닛 도우미 1.4.10.app'
            app, backup, log, result = self.run_swap(root, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(app.exists())
            self.assertEqual(mac_updates.app_version(target), '1.4.10')
            self.assertEqual(mac_updates.app_version(backup), '1.4.9')
            self.assertEqual(log.read_text().split('\n')[0], str(target))

    def test_failed_move_restores_the_running_app(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / '플래닛 도우미 1.4.10.app'
            app, backup, log, result = self.run_swap(root, target, candidate_ok=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(mac_updates.app_version(app), '1.4.9')
            self.assertFalse(target.exists())
            self.assertEqual(log.read_text().split('\n')[0], str(app))

    def test_pinned_icon_moves_to_the_new_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dock = root / 'dock.plist'
            old = {'bundle-identifier': mac_updates.BUNDLE_ID, 'file-label': '플래닛 도우미 1.4.9',
                   'book': b'old', 'file-data': {'_CFURLString': 'file:///old/', '_CFURLStringType': 15}}
            other = {'bundle-identifier': 'com.example.other', 'file-label': '다른 앱',
                     'file-data': {'_CFURLString': 'file:///Applications/Other.app/', '_CFURLStringType': 15}}
            with dock.open('wb') as stream:
                plistlib.dump({'persistent-apps': [{'tile-data': other}, {'tile-data': old}]}, stream)
            target = root / '플래닛 도우미 1.4.10.app'
            result = self.run_swap(root, target, dock=dock)[3]
            self.assertEqual(result.returncode, 0, result.stderr)
            with dock.open('rb') as stream:
                tiles = [tile['tile-data'] for tile in plistlib.load(stream)['persistent-apps']]
            self.assertEqual(tiles[0], other)
            self.assertEqual(tiles[1]['file-label'], '플래닛 도우미 1.4.10')
            # macOS writes the decomposed (NFD) Hangul form, as the Dock itself does.
            url = unquote(tiles[1]['file-data']['_CFURLString'])
            self.assertEqual(unicodedata.normalize('NFC', url), unquote(target.as_uri()) + '/')
            self.assertNotIn('book', tiles[1])


if __name__ == '__main__':
    unittest.main()
