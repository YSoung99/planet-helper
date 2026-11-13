"""Linux (KDE/X11/XWayland) app: only Linux-specific hooks live here.

Shared behaviour is in qt_controller.py so the macOS app gets the same changes.
"""
import os

import tracker
from qt_controller import (SharedController, Value, LevelValue, TrackerWindow, Selection,  # noqa: F401
                           WindowImage, WindowSelection, level_mismatch, short_exp, level_eta_text, run_app)


def native_overlay(widget, level=3):
    """KWin manages Qt window flags on Linux."""
    return


class Controller(SharedController):
    CAPTURE_MODULE = 'linux_capture'
    CAPTURE_OPTIONS = {'require_active': True}
    CAPTURE_BACKENDS = ('kwin-game-region', 'xcomposite-game-window')

    def native_overlay(self, widget, level=3):
        native_overlay(widget, level)

    def repeat_bridge_class(self):
        from linux_key_repeat import KeyRepeatBridge
        return KeyRepeatBridge

    def apply_pin(self, enabled):
        if self.window.isVisible() and self.application.platformName() == 'xcb':
            return enabled if self.set_x11_pin(enabled) else self.always_on_top
        return super().apply_pin(enabled)

    def set_x11_pin(self, enabled):
        from Xlib import X, display, error, protocol
        try:
            connection = display.Display()
            try:
                message = protocol.event.ClientMessage(
                    window=int(self.window.winId()),
                    client_type=connection.intern_atom('_NET_WM_STATE'),
                    data=(32, [int(enabled),
                               connection.intern_atom('_NET_WM_STATE_ABOVE'),
                               connection.intern_atom('_NET_WM_STATE_STAYS_ON_TOP'), 1, 0]),
                )
                connection.screen().root.send_event(
                    message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                connection.sync()
            finally:
                connection.close()
            return True
        except (error.DisplayConnectionError, error.XError) as exc:
            tracker.log_error('맨 위 고정', exc)
            self.status.set('맨 위 고정 변경 실패 · 창 연결 확인')
            return False

    def add_repeat_permission_widgets(self, layout):
        from PySide6.QtWidgets import QLabel
        layout.addWidget(QLabel('Linux 입력 장치 권한 · 기존 반복 앱과 동시 사용 불가'))
        return lambda: None


def main():
    # Qt's native Wayland window does not apply the keep-above hint on KWin.
    # XWayland exposes the stacking hint for this overlay.
    if os.environ.get('XDG_SESSION_TYPE') == 'wayland' and os.environ.get('DISPLAY'):
        os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
    run_app(Controller)


if __name__ == '__main__': main()
