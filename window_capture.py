"""Public ScreenCaptureKit snapshots of one app window, independent of occlusion."""
import io
import math
import threading
import time

BUNDLE_ID = 'com.nexon.mod.inhouse'


def _await_callback(start, timeout=5):
    ready = threading.Event()
    result = []
    def completed(value, error):
        result.extend((value, error))
        ready.set()
    start(completed)
    deadline = time.monotonic()+timeout
    while not ready.wait(.02):
        if time.monotonic() >= deadline:
            raise TimeoutError('게임 창 캡처 시간 초과 · 자동 재시도')
        if threading.current_thread() is threading.main_thread():
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app: app.processEvents()
    value, error = result
    if error is not None:
        raise RuntimeError('게임 창 캡처 실패: '+str(error))
    if value is None:
        raise RuntimeError('게임 창 캡처 결과 없음')
    return value


def valid_roi(region):
    if not isinstance(region, dict): return False
    keys = ('left','top','width','height')
    if not all(type(region.get(k)) in (int,float) and math.isfinite(region[k]) for k in keys):
        return False
    return (0 <= region['left'] < 1 and 0 <= region['top'] < 1
            and 0 < region['width'] <= 1 and 0 < region['height'] <= 1
            and region['left']+region['width'] <= 1.000001
            and region['top']+region['height'] <= 1.000001)


def pixel_rect(region,width,height):
    if not valid_roi(region): raise ValueError('게임 경험치 영역을 다시 지정해 주세요')
    left=max(0,min(width-1,math.floor(region['left']*width)))
    top=max(0,min(height-1,math.floor(region['top']*height)))
    right=max(left+1,min(width,math.ceil((region['left']+region['width'])*width)))
    bottom=max(top+1,min(height,math.ceil((region['top']+region['height'])*height)))
    return left,top,right-left,bottom-top


def region_from_desktop(region, frame):
    x,y,width,height = frame
    if not region or width <= 0 or height <= 0:
        raise ValueError('게임 경험치 영역을 지정해 주세요')
    # Allow a two-point selection border to extend past the window edge.
    left,top = region['left']-x,region['top']-y
    right,bottom = left+region['width'],top+region['height']
    if left < -2 or top < -2 or right > width+2 or bottom > height+2:
        raise ValueError('게임 창에서 경험치 영역을 다시 지정해 주세요')
    left,top,right,bottom = max(0,left),max(0,top),min(width,right),min(height,bottom)
    roi = dict(left=left/width, top=top/height,
               width=(right-left)/width,height=(bottom-top)/height,
               reference_aspect=width/height)
    if not valid_roi(roi):
        raise ValueError('게임 창에서 경험치 영역을 다시 지정해 주세요')
    return roi


class WindowCapture:
    def __init__(self, bundle_id=BUNDLE_ID, preferred_id=None, expected_pid=None):
        if bundle_id is None and expected_pid is None:
            raise ValueError('A capture owner must be specified')
        self.bundle_id = bundle_id
        self.expected_pid = expected_pid
        self.preferred_id = preferred_id
        self.content = None
        self.content_at = 0

    def find_window(self, force=False):
        if force or self.content is None or time.monotonic()-self.content_at > 3:
            from ScreenCaptureKit import SCShareableContent
            self.content = _await_callback(lambda done:
                SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True,False,done))
            self.content_at = time.monotonic()
        windows = []
        for window in self.content.windows():
            owner = window.owningApplication()
            frame = window.frame()
            if (owner and (str(owner.bundleIdentifier()) == self.bundle_id if self.bundle_id is not None
                           else int(owner.processID()) == self.expected_pid)
                    and frame.size.width >= 200 and frame.size.height >= 100):
                windows.append(window)
        if not windows:
            self.content = None
            raise RuntimeError('MapleStory Worlds 게임 창 대기 · 실행/최소화 상태 확인')
        preferred = next((w for w in windows if int(w.windowID()) == self.preferred_id),None)
        window = preferred or max(windows,key=lambda w:
            (str(w.title() or '') == 'MapleStory Worlds',w.frame().size.width*w.frame().size.height))
        self.preferred_id = int(window.windowID())
        return window

    def snapshot(self, roi=None, legacy_region=None, include_full=False):
        import objc
        import Quartz  # Register CGImageRef bridging before ScreenCaptureKit callbacks.
        from AppKit import NSBitmapImageRep, NSBitmapImageFileTypePNG
        from ScreenCaptureKit import SCContentFilter, SCStreamConfiguration, SCScreenshotManager
        from PIL import Image
        with objc.autorelease_pool():
            window = self.find_window()
            frame = window.frame()
            bounds = (float(frame.origin.x),float(frame.origin.y),float(frame.size.width),float(frame.size.height))
            content_filter = SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
            rect = content_filter.contentRect()
            width,height = float(rect.size.width),float(rect.size.height)
            scale = float(content_filter.pointPixelScale())
            if roi is None and legacy_region is not None:
                roi = region_from_desktop(legacy_region,bounds)
            configuration = SCStreamConfiguration.alloc().init()
            configuration.setShowsCursor_(False)
            configuration.setCapturesAudio_(False)
            configuration.setIgnoreShadowsSingleWindow_(True)
            configuration.setShouldBeOpaque_(True)
            if roi is not None:
                if not valid_roi(roi): raise ValueError('게임 경험치 영역을 다시 지정해 주세요')
                reference = roi.get('reference_aspect',width/height)
                if abs((width/height)/reference-1) > .02:
                    raise ValueError('게임 창 비율 변경 · 경험치 영역 다시 지정')
            # sourceRect is not a reliable crop for desktop-independent windows.
            # Capture at full native resolution, then crop the returned CGImage.
            # The crop uses the same pixel coordinates as the selection image.
            configuration.setWidth_(max(1,round(width*scale)))
            configuration.setHeight_(max(1,round(height*scale)))
            try:
                captured = _await_callback(lambda done:
                    SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(content_filter,configuration,done))
                crop = None
                if roi is not None:
                    left,top,crop_width,crop_height=pixel_rect(roi,Quartz.CGImageGetWidth(captured),Quartz.CGImageGetHeight(captured))
                    crop = (left,top,left+crop_width,top+crop_height)
                    if not include_full:
                        captured=Quartz.CGImageCreateWithImageInRect(captured,((left,top),(crop_width,crop_height)))
                        if captured is None: raise RuntimeError('게임 경험치 영역 잘라내기 실패')
                bitmap = NSBitmapImageRep.alloc().initWithCGImage_(captured)
                encoded = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG,{})
                image = Image.open(io.BytesIO(bytes(encoded))).convert('RGB')
            except Exception:
                self.content = None
                raise
            image.info.update(capture_backend='sck-window',window_id=int(window.windowID()),
                              window_title=str(window.title() or ''),window_bounds=bounds,
                              content_points=(width,height),window_region=roi)
            if include_full and crop is not None:
                full = image
                image = full.crop(crop)
                image.info['full_game_image'] = full
            return image
