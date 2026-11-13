"""macOS Screen Recording permission and Finder helpers."""
import ctypes
import subprocess


def has_capture_permission():
    core = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
    core.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
    return bool(core.CGPreflightScreenCaptureAccess())


def open_records(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(['open', str(path)])
