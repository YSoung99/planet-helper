"""macOS app: only macOS-specific hooks live here.

Shared behaviour is in qt_controller.py so the Linux app gets the same changes.
"""
import ctypes
import subprocess
import sys

import tracker
from qt_controller import (SharedController, Value, LevelValue, TrackerWindow, Selection,  # noqa: F401
                           WindowImage, WindowSelection, level_mismatch, short_exp, level_eta_text, run_app)


def native_overlay(widget, level=3):
    """Qt owns the event loop; Cocoa only supplies Space membership."""
    import objc
    from AppKit import (NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary)
    view = objc.objc_object(c_void_p=ctypes.c_void_p(int(widget.winId())))
    window = view.window()
    window.setLevel_(level)
    window.setHidesOnDeactivate_(False)
    window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                  NSWindowCollectionBehaviorFullScreenAuxiliary)


def open_privacy_settings(pane):
    subprocess.Popen(['open', f'x-apple.systempreferences:com.apple.preference.security?{pane}'])


class Controller(SharedController):
    NATIVE_MENU_BAR = True
    CAPTURE_MODULE = 'window_capture'
    CAPTURE_OPTIONS = {}
    CAPTURE_BACKENDS = ('sck-window',)
    REPEAT_NEEDS_PERMISSION = True

    def __init__(self, application, start_worker=True, restore=True, start_repeat=None):
        super().__init__(application, start_worker, restore, start_repeat)
        if getattr(sys, 'frozen', False) and restore and start_worker:
            from update_panel import MacUpdateBackend, UpdatePanel
            from PySide6.QtWidgets import QPushButton
            self.update_button = QPushButton('업데이트')
            self.updates = UpdatePanel(self, MacUpdateBackend())
            self.update_button.clicked.connect(self.updates.show)
            self.window.layout().itemAt(0).layout().insertWidget(1, self.update_button)
            self.menu_bar.actions()[0].menu().addAction('업데이트 및 롤백', self.updates.show)

    def show(self):
        super().show()
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle

        path = NSBundle.mainBundle().pathForResource_ofType_('PlanetEXP', 'icns')
        if path:
            icon = NSImage.alloc().initWithContentsOfFile_(path)
            if icon is not None:
                NSApplication.sharedApplication().setApplicationIconImage_(icon)

    def native_overlay(self, widget, level=3):
        native_overlay(widget, level)

    def open_path(self, path):
        subprocess.Popen(['open', str(path)])

    def open_records(self):
        from macos_support import open_records
        open_records(tracker.BASE/'records')

    def extra_menu_actions(self, menu):
        menu.addAction('화면 기록 권한', self.show_capture_permission)
        if hasattr(self, 'updates'):
            menu.addAction('업데이트 및 롤백', self.updates.show)

    def has_background_capture_permission(self):
        from macos_support import has_capture_permission
        return has_capture_permission()

    def show_capture_permission(self):
        from PySide6.QtWidgets import QMessageBox
        from macos_support import has_capture_permission
        if has_capture_permission():
            QMessageBox.information(self.window, '화면 기록 권한', '화면 기록 권한이 이미 허용돼 있어요.')
        else:
            self.check_permission()

    def open_missing_repeat_permission(self, state):
        # macOS prompts only once per app; afterwards the request is silent.
        if not state.get('inputMonitoring'):
            open_privacy_settings('Privacy_ListenEvent')
        elif not state.get('accessibility'):
            open_privacy_settings('Privacy_Accessibility')
        else:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self.window, '연타 권한', '연타 권한이 이미 모두 허용돼 있어요.')

    def check_permission(self):
        from PySide6.QtWidgets import QMessageBox
        from macos_support import has_capture_permission
        if has_capture_permission(): return True
        core = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        core.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        if core.CGRequestScreenCaptureAccess(): return True
        self.status.set('화면 기록 권한 허용 후 앱 다시 실행')
        answer = QMessageBox.question(self.window, '화면 기록 권한',
            '경험치 숫자를 읽으려면 화면 기록 권한이 필요해요.\n\n'
            '시스템 설정 → 개인정보 보호 및 보안 → 화면 및 시스템 오디오 기록에서\n'
            f'{self.APP_TITLE}을 허용한 뒤 앱을 다시 실행해 주세요.\n\n설정 화면을 열까요?')
        if answer == QMessageBox.StandardButton.Yes:
            open_privacy_settings('Privacy_ScreenCapture')
        return False

    def add_repeat_permission_widgets(self, layout):
        from PySide6.QtWidgets import QLabel, QPushButton
        permissions = QLabel(); permissions.setWordWrap(True)
        def update_permissions(state):
            permissions.setText(state.get('postPermissionName', '손쉬운 사용')+': '+('허용' if state.get('accessibility') else '허용 필요')+
                                '\n입력 모니터링: '+('허용' if state.get('inputMonitoring') else '허용 필요'))
        update_permissions(self.key_repeat.state)
        self.key_repeat.changed.connect(update_permissions)
        layout.addWidget(permissions)
        connect = QPushButton('연타 권한 연결')
        def request_permissions():
            from PySide6.QtCore import QTimer
            self.key_repeat.request_permissions()
            # Give the helper a moment to publish the refreshed permission state.
            QTimer.singleShot(1000, lambda: self.open_missing_repeat_permission(self.key_repeat.state))
        connect.clicked.connect(request_permissions)
        layout.addWidget(connect)
        layout.addWidget(QLabel('시스템 설정에서 플래닛 도우미를 허용해 주세요.\n권한을 바꾼 뒤 앱을 다시 실행해야 할 수 있어요.'))
        return lambda: self.key_repeat.changed.disconnect(update_permissions)


def main():
    run_app(Controller)


if __name__ == '__main__': main()
