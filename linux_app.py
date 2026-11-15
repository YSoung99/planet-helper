"""Linux (KDE/X11/XWayland) app: only Linux-specific hooks live here.

Shared behaviour is in qt_controller.py so the macOS app gets the same changes.
"""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

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

    def __init__(self, application, start_worker=True, restore=True, start_repeat=None):
        super().__init__(application, start_worker, restore, start_repeat)
        install_root = os.environ.get('PLANET_HELPER_INSTALL_ROOT')
        if install_root and restore and start_worker:
            from linux_updates import LinuxUpdateBackend
            from update_panel import UpdatePanel
            from PySide6.QtWidgets import QPushButton
            self.update_button = QPushButton('업데이트')
            self.updates = UpdatePanel(self, LinuxUpdateBackend(install_root))
            self.update_button.clicked.connect(self.updates.show)
            self.window.layout().itemAt(0).layout().insertWidget(1, self.update_button)

    def extra_menu_actions(self, menu):
        if hasattr(self, 'updates'):
            menu.addAction('업데이트 및 롤백', self.updates.show)

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


def refresh_desktop_icon(home, source):
    """Migrate the desktop shortcut from installs created before the custom icon."""
    desktop = home / '.local/share/applications/planet-helper.desktop'
    if not desktop.is_file():
        return
    lines = desktop.read_text(encoding='utf-8').splitlines(keepends=True)
    launcher = home / '.local/bin/planet-helper'
    if not any(line.rstrip() == f'Exec={launcher}' for line in lines):
        return
    icon = home / '.local/share/icons/planet-helper.png'
    updated_line = f'Icon={icon}'
    old_line = 'Icon=utilities-system-monitor'
    existing = next((line.rstrip() for line in lines if line.startswith('Icon=')), '')
    if existing not in (old_line, updated_line):
        return

    def write_atomic(path, data):
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            tmp = Path(file.name)
        try:
            tmp.write_bytes(data)
            tmp.chmod(0o644)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    payload = source.read_bytes()
    if not icon.is_file() or icon.read_bytes() != payload:
        icon.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(icon, payload)
    if existing == old_line:
        updated = ''.join(f'{updated_line}\n' if line.rstrip() == old_line else line for line in lines)
        write_atomic(desktop, updated.encode('utf-8'))


def main():
    # Qt's native Wayland window does not apply the keep-above hint on KWin.
    # XWayland exposes the stacking hint for this overlay.
    if os.environ.get('XDG_SESSION_TYPE') == 'wayland' and os.environ.get('DISPLAY'):
        os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
    if os.environ.get('PLANET_HELPER_INSTALL_ROOT'):
        try:
            refresh_desktop_icon(Path.home(), tracker.RESOURCES / 'planet-helper-icon.png')
        except OSError as exc:
            tracker.log_error('앱 메뉴 아이콘 변경', exc)
    run_app(Controller)


if __name__ == '__main__': main()
