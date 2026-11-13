import unittest
from unittest.mock import patch

import qt_controller


class MacPortTests(unittest.TestCase):
    def test_permission_denial_never_starts_or_resets_measurement(self):
        app = qt_controller.SharedController.__new__(qt_controller.SharedController)
        app.auto_hud = True
        app.region = app.window_region = None
        app.running = False
        with patch.object(app, 'check_permission', return_value=False), \
                patch.object(app, 'reset') as reset:
            app.toggle()
            reset.assert_not_called()
            self.assertFalse(app.running)


if __name__ == '__main__':
    unittest.main()
