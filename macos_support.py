"""macOS Screen Recording permission and Finder helpers."""
import ctypes
import subprocess

_CORE_GRAPHICS = None


def _core_graphics():
    global _CORE_GRAPHICS
    if _CORE_GRAPHICS is None:
        core = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        core.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        core.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
        _CORE_GRAPHICS = core
    return _CORE_GRAPHICS


def has_capture_permission():
    return bool(_core_graphics().CGPreflightScreenCaptureAccess())


def request_capture_permission():
    return bool(_core_graphics().CGRequestScreenCaptureAccess())


def open_records(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(['open', str(path)])
