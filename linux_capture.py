"""Capture the game's X11/XWayland window, with a focused desktop fallback."""
import math
from PIL import Image
from Xlib import X, display, error
import mss
import tracker


def pixel_rect(roi,width,height):
    if not isinstance(roi,dict): raise ValueError('게임 영역을 다시 지정해 주세요')
    values = [float(roi[k]) for k in ('left','top','width','height')]
    if not all(math.isfinite(v) for v in values): raise ValueError('잘못된 게임 영역')
    x,y,w,h=values
    if x<0 or y<0 or w<=0 or h<=0 or x+w>1.001 or y+h>1.001: raise ValueError('잘못된 게임 영역')
    aspect=roi.get('reference_aspect',width/height)
    if abs(width/height/aspect-1)>.03: raise RuntimeError('게임 화면 비율 변경 · 영역 다시 지정')
    from window_capture import pixel_rect as normalized_pixel_rect
    return normalized_pixel_rect(roi, width, height)


class WindowCapture:
    def __init__(self, *, require_active=False):
        self.display=display.Display(); self.root=self.display.screen().root
        self.capture=mss.MSS()
        self.require_active = require_active
        info = self.display.display.info
        self.composite_depths = {
            fmt.depth for fmt in info.pixmap_formats
            if fmt.depth in (24, 32) and fmt.bits_per_pixel == 32 and fmt.scanline_pad == 32
        } if self.display.has_extension('Composite') and info.image_byte_order == X.LSBFirst else set()
        self.hidden_atom = self.display.intern_atom('_NET_WM_STATE_HIDDEN')
        self.state_atom = self.display.intern_atom('_NET_WM_STATE')

    def game(self):
        prop=self.root.get_full_property(self.display.intern_atom('_NET_CLIENT_LIST'),X.AnyPropertyType)
        for wid in ([] if prop is None else prop.value):
            window=self.display.create_resource_object('window',int(wid))
            classes=window.get_wm_class() or ()
            if not any(str(c).lower()=='msw.exe' for c in classes): continue
            state = window.get_full_property(self.state_atom, X.AnyPropertyType)
            if (window.get_attributes().map_state != X.IsViewable
                    or state is not None and self.hidden_atom in state.value):
                raise RuntimeError('게임 최소화 · 판독 대기')
            geo=window.get_geometry(); point=self.root.translate_coords(window,0,0)
            return window,dict(left=point.x,top=point.y,width=geo.width,height=geo.height)
        raise RuntimeError('MapleStory Worlds 게임 창을 찾을 수 없음')

    def check_active(self, window):
        active = self.root.get_full_property(self.display.intern_atom('_NET_ACTIVE_WINDOW'), X.AnyPropertyType)
        if active is None or len(active.value) == 0 or int(active.value[0]) != window.id:
            raise RuntimeError('게임 창 활성화 대기 · 다른 창에서는 판독 중지')

    def window_image(self, window, size, rect):
        if not self.composite_depths:
            return None
        catcher = error.CatchError(error.XError)
        pixmap = window.composite_name_window_pixmap(onerror=catcher)
        self.display.sync()
        if catcher.get_error() is not None:
            self.display.display.free_resource_id(pixmap.id)
            return None
        try:
            geometry = pixmap.get_geometry()
            if geometry.depth not in self.composite_depths or (geometry.width, geometry.height) != size:
                return None
            x, y, width, height = rect
            data = pixmap.get_image(x, y, width, height, X.ZPixmap, 0xffffffff).data
            return Image.frombytes('RGB', (width, height), data, 'raw', 'BGRX')
        except (error.XError, ValueError):
            return None
        finally:
            pixmap.free()

    def snapshot(self,roi=None,legacy_region=None,include_full=False):
        window,bounds=self.game()
        size = bounds['width'], bounds['height']
        rect = pixel_rect(roi, *size) if roi is not None else (0, 0, *size)
        full = None
        frame = None
        if not legacy_region:
            whole = include_full and roi is not None
            frame = self.window_image(window, size, (0, 0, *size) if whole else rect)
            if frame is not None and whole:
                full = frame
                x, y, width, height = rect
                frame = full.crop((x, y, x+width, y+height))
        if frame is None:
            frame, full = self.desktop_snapshot(window, bounds, roi, legacy_region, include_full)
            backend = 'kwin-game-region'
        else:
            backend = 'xcomposite-game-window'
        current, current_bounds = self.game()
        if current.id != window.id or current_bounds != bounds:
            raise ValueError('게임 창 위치 또는 크기 변경 · 다시 확인 중')
        frame.info.update(capture_backend=backend, content_points=size, window_id=window.id,
                          window_bounds=bounds, window_title='MapleStory Worlds')
        if full is not None: frame.info['full_game_image']=full
        if roi is not None: frame.info['window_region']=roi
        return frame

    def desktop_snapshot(self, window, bounds, roi, legacy_region, include_full):
        measure = roi is not None or bool(legacy_region) or self.require_active
        if measure:
            self.check_active(window)
        region=dict(bounds)
        if roi is not None:
            x,y,w,h=pixel_rect(roi,bounds['width'],bounds['height'])
            region.update(left=bounds['left']+x,top=bounds['top']+y,width=w,height=h)
        elif legacy_region:
            region=dict(legacy_region)
        full = None
        if include_full and roi is not None:
            full = tracker.capture_image(self.capture,bounds)
            x,y,w,h=pixel_rect(roi,full.width,full.height)
            frame = full.crop((x,y,x+w,y+h))
        else:
            frame=tracker.capture_image(self.capture,region)
        if measure:
            self.check_active(window)
        return frame, full

    def close(self):
        self.capture.close(); self.display.close()
