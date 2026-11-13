import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from start_captures import StartCaptures


class StartCaptureTests(unittest.TestCase):
    def test_stages_keep_exact_frame_and_metadata_for_each_session_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StartCaptures(folder)
            full = Image.new('RGB', (80, 40), '#123456')
            roi = full.crop((3, 4, 30, 20))
            for session_id in ('20261101-150000-123', '20261101-150001-456'):
                for stage in store.STAGES:
                    self.assertTrue(store.submit(session_id, stage, full, roi, {'baseline':[100, .01]}))
            store.close()
            for path in Path(folder).glob('*/*.json'):
                metadata = json.loads(path.read_text())
                self.assertEqual(metadata['baseline'], [100, .01])
                self.assertEqual(Image.open(path.with_suffix('.png')).tobytes(), full.tobytes())
                self.assertEqual(Image.open(path.parent/metadata['exp_image']).tobytes(), roi.tobytes())
            self.assertEqual(len(list(Path(folder).glob('*/*.json'))), 6)
            reopened = StartCaptures(folder)
            self.assertFalse(reopened.submit('20261101-150000-123', 'hunt-start', roi, roi, {}))
            reopened.close()
            self.assertEqual(Image.open(Path(folder)/'20261101-150000-123'/'hunt-start.png').size, full.size)

    def test_transient_failure_retries_and_persistent_failure_does_not_stop_later_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StartCaptures(folder)
            image = Image.new('RGB', (2, 2))
            original = store._write
            with patch.object(store, '_write', side_effect=[OSError('temporary'), None]) as write:
                store.submit('1-1', 'first-frame', image, image, {})
                store.flush()
                self.assertEqual(write.call_count, 2)
                self.assertIsNone(store.results.get_nowait()[1])
            with patch.object(store, '_write', side_effect=OSError('disk full')):
                store.submit('1-1', 'hunt-start', image, image, {})
                store.flush()
                self.assertEqual(store.results.get_nowait()[1], 'disk full')
            store.submit('2-2', 'first-frame', image, image, {})
            store.close()
            self.assertTrue((Path(folder)/'2-2'/'first-frame.json').exists())

    def test_repeated_stage_is_saved_once_even_while_writer_is_busy(self):
        with tempfile.TemporaryDirectory() as folder:
            store = StartCaptures(folder)
            image = Image.new('RGB', (2, 2))
            self.assertTrue(store.submit('1-1', 'baseline', image, image, {}))
            self.assertFalse(store.submit('1-1', 'baseline', image, image, {}))
            store.close()

