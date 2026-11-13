import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tracker


class DisplaySettingsTests(unittest.TestCase):
    def test_display_choices_survive_relaunch_with_existing_tracking_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            expected = dict(level=170, position=[-300, 90], compact=True,
                            always_on_top=False, window_opacity=.90)
            path.write_text(json.dumps(expected))
            clean, warning = tracker.load_settings(path)
            self.assertIsNone(warning)
            self.assertEqual(clean, expected)

    def test_invalid_display_values_cannot_hide_window_or_destroy_other_settings(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tracker, 'log_error'):
            path = Path(folder) / 'settings.json'
            for bad_opacity in (0, 2, True, float('nan'), '0.9'):
                with self.subTest(opacity=bad_opacity):
                    path.write_text(json.dumps(dict(level=170, compact='false',
                        always_on_top=1, window_opacity=bad_opacity)))
                    clean, warning = tracker.load_settings(path)
                    self.assertEqual(clean, dict(level=170))
                    self.assertIsNotNone(warning)
