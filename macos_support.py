"""macOS window placement and Screen Recording integration (main thread only)."""
import ctypes
import subprocess


def has_capture_permission():
    core = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    core.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    return bool(core.CGPreflightScreenCaptureAccess())


def ensure_capture_permission(root):
    from tkinter import messagebox
    if has_capture_permission():
        return True
    core = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    core.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    if core.CGRequestScreenCaptureAccess():
        return True
    if messagebox.askyesno(
        '화면 기록 권한',
        '경험치 숫자를 읽으려면 화면 기록 권한이 필요해요.\n\n'
        '시스템 설정 → 개인정보 보호 및 보안 → 화면 및 시스템 오디오 기록에서\n'
        '메이플 플래닛 EXP를 허용한 뒤 앱을 다시 실행해 주세요.\n\n'
        '설정 화면을 열까요?', parent=root):
        subprocess.Popen(['open', 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'])
    return False


def configure_window(root, rectangle=None):
    """Use Cocoa screen coordinates, including displays left of the main display."""
    from AppKit import (NSApplication, NSScreen, NSFloatingWindowLevel,
                        NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary)
    root.update_idletasks()
    window = next((w for w in NSApplication.sharedApplication().windows()
                   if str(w.title()) == root.title()), None)
    if window is None:
        return False
    if rectangle is None:
        rectangle = (root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height())
    # Tk 9 can leave a titled Cocoa window after overrideredirect(True).
    # Matching the native style also keeps mouse and content coordinates aligned.
    window.setStyleMask_(0)
    window.setHidesOnDeactivate_(False)
    window.setLevel_(NSFloatingWindowLevel)
    window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                  NSWindowCollectionBehaviorFullScreenAuxiliary)
    x, y, width, height = rectangle
    main_height = NSScreen.screens()[0].frame().size.height
    window.setFrame_display_(((x, main_height-y-height), (width, height)), True)
    return True


def open_records(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(['open', str(path)])
