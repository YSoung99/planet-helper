import unittest
from types import SimpleNamespace
from unittest.mock import patch
import tracker


class MacPortTests(unittest.TestCase):
    def test_saved_position_survives_negative_origin_second_display(self):
        app = tracker.App.__new__(tracker.App)
        app.root = SimpleNamespace(winfo_screenwidth=lambda: 1440)
        monitors = [{'left': 0, 'top': 0, 'width': 1440, 'height': 900},
                    {'left': -1920, 'top': -100, 'width': 1920, 'height': 1080}]
        fake = SimpleNamespace(monitors=[{}]+monitors)
        with patch('mss.MSS') as factory, patch.object(tracker.sys, 'platform', 'darwin'), \
                patch('macos_support.configure_window') as place:
            factory.return_value.__enter__.return_value = fake
            app.place_window([-1800, 50])
            place.assert_called_once_with(app.root, (-1800, 50, 280, 492))

    def test_disconnected_display_position_is_recovered(self):
        app = tracker.App.__new__(tracker.App)
        app.root = SimpleNamespace(winfo_screenwidth=lambda: 1440)
        fake = SimpleNamespace(monitors=[{}, {'left': 0, 'top': 0, 'width': 1440, 'height': 900}])
        with patch('mss.MSS') as factory, patch.object(tracker.sys, 'platform', 'darwin'), \
                patch('macos_support.configure_window') as place:
            factory.return_value.__enter__.return_value = fake
            app.place_window([2800, 50])
            place.assert_called_once_with(app.root, (1140, 90, 280, 492))

    def test_permission_denial_never_starts_or_resets_measurement(self):
        app = tracker.App.__new__(tracker.App)
        app.region = {'left': 20, 'top': 20, 'width': 180, 'height': 22}
        app.running = False
        with patch.object(app, 'check_permission', return_value=False), \
                patch.object(app, 'reset') as reset:
            app.toggle()
            reset.assert_not_called()
            self.assertFalse(app.running)


if __name__ == '__main__':
    unittest.main()
