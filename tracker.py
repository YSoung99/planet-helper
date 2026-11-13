"""Planet EXP tracker: local screen capture, OCR and conservative session accounting."""
import csv
import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import queue
import re
import threading
import time
import math
import statistics
import sys
import traceback
from datetime import datetime
from collections import deque, Counter
from pathlib import Path
if sys.platform == 'darwin' and not getattr(sys, 'frozen', False):
    # python-build-standalone keeps Tcl/Tk beside its Python runtime.
    import _tkinter
    runtime_lib = Path(sys.base_prefix)/'lib'
    for variable, filename in (('TCL_LIBRARY', f'tcl{_tkinter.TCL_VERSION}/init.tcl'),
                               ('TK_LIBRARY', f'tk{_tkinter.TK_VERSION}/tk.tcl')):
        library = runtime_lib/filename
        if library.exists(): os.environ.setdefault(variable, str(library.parent))
try:
    import tkinter as tk
    from tkinter import filedialog
    TK_ERROR = tk.TclError
except ImportError:
    # The native macOS package uses Qt and does not ship Tcl/Tk.
    tk = filedialog = None
    TK_ERROR = RuntimeError

RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
BASE = (Path(os.environ.get('PLANET_EXP_DATA_DIR',
        str(Path.home()/'Library'/'Application Support'/'Maple Planet Helper Auto Trial')))
        if sys.platform == 'darwin' else Path(__file__).resolve().parent)
APP_VERSION = '1.4.0-auto.3'
MAX_BOUNDARY_GAP = 10.0
IDLE_AFTER = 60.0  # no EXP gain for longer than this counts as a break
try:
    EXP_METADATA = json.loads((RESOURCES/'exp_table.json').read_text(encoding='utf-8'))
    EXP_TABLE = {int(level): int(value) for level, value in EXP_METADATA['required'].items()}
except (OSError, ValueError, KeyError, TypeError):
    EXP_METADATA = {'screenshot_verified': {'169': 405810702}}
    EXP_TABLE = {169: 405810702}  # Exact in-game denominator supplied by the user.

def log_error(context, error):
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        (BASE/'last-error.log').write_text(
            f'{time.strftime("%Y-%m-%d %H:%M:%S")} {context}: {type(error).__name__}: {error}\n',
            encoding='utf-8')
    except OSError:
        pass


def current_boot_id():
    if sys.platform == 'darwin':
        return subprocess.check_output(['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],text=True,timeout=2).strip()
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def load_settings(path):
    """Keep valid fields when settings are damaged; never pass invalid ROIs to the worker."""
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeError, ValueError) as exc:
        log_error('설정 읽기', exc)
        return {}, '설정 복구 · 영역 다시 지정'
    if not isinstance(payload, dict):
        log_error('설정 읽기', ValueError('Settings must be an object'))
        return {}, '설정 복구 · 영역 다시 지정'
    clean, invalid = {}, []
    region = payload.get('region')
    if region is not None:
        keys = ('left', 'top', 'width', 'height')
        if (isinstance(region, dict) and all(type(region.get(key)) is int for key in keys)
                and region['width'] >= 40 and region['height'] >= 8):
            clean['region'] = {key: region[key] for key in keys}
        else:
            invalid.append('region')
    if payload.get('window_region') is not None:
        from window_capture import valid_roi
        window_region = payload['window_region']
        aspect = window_region.get('reference_aspect') if isinstance(window_region,dict) else None
        if valid_roi(window_region) and type(aspect) in (int,float) and math.isfinite(aspect) and aspect > 0:
            clean['window_region'] = {key:window_region[key] for key in ('left','top','width','height','reference_aspect')}
        else:
            invalid.append('window_region')
    position = payload.get('position')
    if position is not None:
        if isinstance(position, list) and len(position) == 2 and all(type(v) is int for v in position):
            clean['position'] = position
        else:
            invalid.append('position')
    level = payload.get('level', 169)
    if type(level) is int and level in EXP_TABLE:
        clean['level'] = level
    else:
        invalid.append('level')
    for key in ('compact', 'always_on_top', 'auto_hud'):
        if key in payload:
            if type(payload[key]) is bool:
                clean[key] = payload[key]
            else:
                invalid.append(key)
    if 'window_opacity' in payload:
        value = payload['window_opacity']
        if type(value) in (int, float) and math.isfinite(value) and .65 <= value <= 1.0:
            clean['window_opacity'] = float(value)
        else:
            invalid.append('window_opacity')
    if invalid:
        log_error('설정 읽기', ValueError('Invalid fields: ' + ', '.join(invalid)))
    return clean, '설정 오류 · 일부 항목 초기화' if invalid else None


def parse(text):
    # Spaces are allowed between fields, never inside EXP or percentage digits.
    match=re.fullmatch(r'\s*(?:(EXP[.:]?)\s*)?((?:\d{1,3}(?:,\d{3})+)|\d{1,12})\s*'
                      r'([\[({])\s*(\d{1,2}[.,]\d{2})\s*([%％xX×]?)\s*([\])}]{0,3})\s*',text,re.IGNORECASE)
    if not match:return None
    label,number,opening,pct,percent,closing=match.groups()
    if not closing and not (label and percent in ('%','％')):return None
    if percent in ('x','X','×') and not closing:return None
    digits=number.replace(',','')
    if len(digits)>12:return None
    exp=int(digits);pct=float(pct.replace(',','.'))
    return (exp,pct) if 0<=pct<100 else None


class KWinRegionCapture:
    """KDE's public region API, with no desktop identity spoofing or permission changes."""
    def __init__(self):
        import asyncio
        from dbus_fast.aio import MessageBus
        from dbus_fast import BusType
        self.loop = asyncio.new_event_loop()
        async def connect():
            return await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
        try:
            self.bus = self.loop.run_until_complete(__import__('asyncio').wait_for(connect(),2))
        except Exception:
            self.loop.close()
            raise

    def grab(self, region):
        return self.loop.run_until_complete(self._grab(region))

    async def _grab(self, region):
        import asyncio
        from dbus_fast import Message, MessageType
        from PIL import Image
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        try:
            reply = await asyncio.wait_for(self.bus.call(Message(destination='org.kde.KWin',
                path='/org/kde/KWin/ScreenShot2', interface='org.kde.KWin.ScreenShot2', member='CaptureArea',
                signature='iiuua{sv}h', body=[region['left'],region['top'],region['width'],region['height'],{},0],
                unix_fds=[writer])), 2)
            if reply.message_type == MessageType.ERROR:
                raise RuntimeError(f'KDE capture unavailable: {reply.error_name}')
            meta = {key:value.value for key,value in reply.body[0].items()}
            if meta.get('type') != 'raw' or abs(meta.get('scale',1)-1)>0.001:
                raise RuntimeError('Unsupported KDE capture scale or type')
            width,height,stride = int(meta['width']),int(meta['height']),int(meta['stride'])
            if width<=0 or height<=0 or stride*height>16000000:
                raise RuntimeError('Invalid KDE capture dimensions')
            expected = stride*height
            deadline = self.loop.time()+2
            data = bytearray()
            while len(data)<expected:
                try:
                    chunk = os.read(reader, expected-len(data))
                    if chunk: data.extend(chunk)
                except BlockingIOError:
                    pass
                if len(data)>=expected: break
                if self.loop.time()>deadline: raise TimeoutError('KDE image transfer timed out')
                await asyncio.sleep(.001)
            modes = {16:'RGBX',17:'RGBA',18:'RGBA'}
            if os.sys.byteorder=='little': modes.update({4:'BGRX',5:'BGRA',6:'BGRA'})
            mode = modes.get(int(meta['format']))
            if mode is None: raise RuntimeError('Unsupported KDE image format')
            if mode in ('RGBA','BGRA'):
                return Image.frombytes('RGBA',(width,height),bytes(data),'raw',mode,stride,1).convert('RGB')
            return Image.frombytes('RGB',(width,height),bytes(data),'raw',mode,stride,1)
        finally:
            os.close(reader);os.close(writer)

    def close(self):
        import asyncio
        if self.loop.is_closed(): return
        self.bus.disconnect()
        self.loop.run_until_complete(asyncio.sleep(0))
        self.loop.close()


_capture_state = threading.local()

def create_ocr_engine(*, prefer_native=True):
    if prefer_native and sys.platform.startswith('linux'):
        try:
            from native_ocr import open_recognizer
            return open_recognizer(RESOURCES)
        except ModuleNotFoundError as exc:
            if exc.name not in ('native_ocr', '_planet_ocr'):
                log_error('Rust OCR 초기화 · 기존 엔진 사용', exc)
        except (ImportError, OSError, RuntimeError) as exc:
            log_error('Rust OCR 초기화 · 기존 엔진 사용', exc)

    from rapidocr import RapidOCR, ModelType
    params = {'Rec.model_type': ModelType.MEDIUM,
              'EngineConfig.onnxruntime.intra_op_num_threads': 2,
              'EngineConfig.onnxruntime.inter_op_num_threads': 1,
              'Global.use_det': False, 'Global.use_cls': False}
    model = RESOURCES/'models'/'PP-OCRv6_rec_medium.onnx'
    if model.exists():
        params['Rec.model_path'] = str(model)
    return RapidOCR(params=params)

def capture_image(capture, region):
    from PIL import Image
    if os.environ.get('XDG_SESSION_TYPE') == 'wayland':
        if not getattr(_capture_state,'disabled',False):
            try:
                if not hasattr(_capture_state,'backend'): _capture_state.backend = KWinRegionCapture()
                frame = _capture_state.backend.grab(region)
                frame.info['capture_backend'] = 'kwin-region'
                return frame
            except Exception as exc:
                _capture_state.disabled = True
                _capture_state.reason = str(exc)
                if hasattr(_capture_state,'backend'):
                    _capture_state.backend.close()
        if not shutil.which('spectacle'):
            raise RuntimeError('Wayland 캡처에는 Spectacle이 필요함')
        with tempfile.TemporaryDirectory(prefix='planet-exp-') as folder:
            target = str(Path(folder) / 'screen.png')
            subprocess.run(['spectacle', '-i', '-b', '-n', '-f', '-o', target],
                           check=True, timeout=4, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            with Image.open(target) as image:
                # Spectacle full desktop coordinates are relative to its origin.
                desktop = capture.monitors[0]
                x = region['left'] - desktop['left']
                y = region['top'] - desktop['top']
                frame = image.crop((x,y,x+region['width'],y+region['height'])).convert('RGB')
                frame.info['capture_backend'] = 'spectacle-fallback'
                return frame
    if sys.platform == 'darwin':
        import mss.darwin
        # Coordinates stay in desktop points; retain native Retina pixels for OCR.
        mss.darwin.IMAGE_OPTIONS &= ~mss.darwin.kCGWindowImageNominalResolution
    shot = capture.grab(region)
    frame = Image.frombytes('RGB', shot.size, shot.rgb)
    frame.info['capture_backend'] = 'mss-region'
    return frame

class LoadController:
    """Bound the scheduling target to 1..5 seconds; this is not a hard CPU cap."""
    def __init__(self):
        self.base = 1.0

    def period(self, system_load, cpu_seconds):
        self.base = min(5.0, max(1.0, self.base))
        if system_load >= 85:
            self.base = min(5.0, self.base + 1.0)
        elif system_load >= 65:
            self.base = min(5.0, self.base + .5)
        elif system_load < 50:
            self.base = max(1.0, self.base - .25)
        return min(5.0, max(self.base, max(0, cpu_seconds) / .20))


class LineReader:
    """Reuse a verified result only for byte-identical pixels, at most 5 seconds."""
    def __init__(self, engine):
        self.engine = engine
        self.native = getattr(engine, 'read_exp', None)
        self.key = self.result = None
        self.cached_at = 0
        self.last_readings = []
        self.cached_readings = []

    def read(self, image, observed_at, force=False, *, level=None):
        import numpy as np
        from PIL import Image, ImageOps
        key = (image.mode, image.size, image.tobytes(), level)
        if not force and key == self.key and self.result is not None and observed_at-self.cached_at < 5:
            self.last_readings = list(self.cached_readings)
            return (*self.result, True)
        if self.native is not None:
            value, score, raw, self.last_readings = self.native(image.convert('RGB'), EXP_TABLE.get(level))
            result = (value, score, raw)
            if value is not None:
                self.key,self.result,self.cached_at = key,result,observed_at
                self.cached_readings = list(self.last_readings)
            return (*result, False)
        readings = []
        def recognize(prepared):
            prepared = ImageOps.expand(prepared.resize((image.width*4,image.height*4)),20,fill=0)
            output = self.engine(np.asarray(prepared.convert('RGB')))
            texts = output.txts if output.txts is not None else ()
            scores = output.scores if output.scores is not None else ()
            raw = ''.join(texts)
            readings.append((parse(raw),min(scores) if len(scores) else 0,raw))
        recognize(image)
        # Neutral RGB keeps white glyphs while suppressing the original green bar.
        # The EXP-labelled HUD has green brackets: grayscale preserves those.
        neutral = Image.fromarray(np.asarray(image.convert('RGB')).min(axis=2))
        grayscale = ImageOps.grayscale(image)
        labelled_hud = bool(re.match(r'^EXP[.:]?(?=\d)', re.sub(r'\s+', '', readings[0][2]), re.IGNORECASE))
        recognize(ImageOps.autocontrast(grayscale if labelled_hud else neutral))
        initially_agreed = readings[0][0] is not None and readings[0][0]==readings[1][0] and min(readings[0][1],readings[1][1])>=.85
        if not initially_agreed:
            recognize(ImageOps.autocontrast(neutral if labelled_hud else grayscale))
        self.last_readings = [entry[2] for entry in readings]
        votes = Counter(value for value,score,raw in readings if value is not None and score>=.85)
        winner = votes.most_common(1)
        if winner and winner[0][1]>=2:
            value = winner[0][0]
            matching = [entry for entry in readings if entry[0]==value and entry[1]>=.85]
            result = (value,min(entry[1] for entry in matching),matching[0][2])
            self.key,self.result,self.cached_at = key,result,observed_at
            self.cached_readings = list(self.last_readings)
        else:
            result = (None,readings[0][1],readings[0][2])
        return (*result,False)


class Session:
    def __init__(self, level=None):
        self.level = level
        self.reset()

    def reset(self):
        self.created_wall = time.time()
        self.created_monotonic = time.monotonic()
        self.first = self.last = None
        self.baseline_candidate = None
        self.last_observed = None
        self.restored = False
        self.session_id = time.strftime('%Y%m%d-%H%M%S') + f'-{time.time_ns()%1000000000:09d}'
        self.started = None
        self.rate_anchor = None
        self.rate_anchor_kind = None
        self.ended = None
        self.samples = deque()
        self.rows = []
        self.persisted_rows = 0
        self.ledger_dirty = False
        self.total = EXP_TABLE.get(self.level)
        self.pending = None
        self.confirmations = 0
        self.blocked = False
        self.stop_reason = ''
        self.mismatch_count = 0
        self.decrease_candidate = None
        self.decrease_count = 0
        self.small_decrease_count = 0
        self.small_decrease_at = None
        self.pending_jump = None
        self.positive_rates = deque(maxlen=8)
        self.minute_snapshots = []
        self.valid_minute = {'one': None, 'six': None}
        self.level_ups = []
        self.first_gain = 0  # EXP of the packet that started the hunt timer
        self.idle_closed = 0.0  # finished breaks (seconds beyond IDLE_AFTER)
        self.last_gain_at = None

    def accept(self, value, now, corroborated=False, baseline_verified=False):
        if self.blocked:
            return self.stop_reason
        if self.last_observed is not None and now <= self.last_observed:
            return '이전 관측 무시'
        previous_observed = self.last_observed
        self.last_observed = now
        exp, pct = value
        # Validate BOTH fields before treating a lower OCR result as a real loss.
        from temporal_ocr import consistent_value
        if not consistent_value(value, self.total, self.level):
            self.baseline_candidate = None
            self.decrease_count = 0
            self.small_decrease_count = 0
            self.small_decrease_at = None
            if self.last:
                self.mismatch_count = self.mismatch_count+1 if previous_observed is not None and now-previous_observed <= 10 else 1
                if self.mismatch_count >= 3:
                    return f'연속 불일치 {self.mismatch_count}회 · 마지막 정상값 유지 · 재인식 중'
            return '레벨/숫자/퍼센트 불일치 · 반영 보류'
        self.mismatch_count = 0
        if self.first is None and not baseline_verified:
            if self.baseline_candidate is None or self.baseline_candidate[0] != value or now-self.baseline_candidate[1] > 10:
                self.baseline_candidate = (value, now)
                return '시작값 재확인 중 · 잠시 경험치 획득 대기'
            if now-self.baseline_candidate[1] < .5:
                return '다음 실제 캡처에서 시작값 재확인'
            self.baseline_candidate = None
        if self.last and exp >= self.last[0] and pct < self.last[1]:
            self.small_decrease_count = 0
            self.small_decrease_at = None
            return '퍼센트 오인식 의심 · 반영 보류'
        if self.last and exp < self.last[0]:
            self.pending_jump = None
            loss = self.last[0] - exp
            if pct > self.last[1]-.019999 or loss <= max(100, (self.total or 0)*.0002):
                # OCR correction and a genuine small loss are indistinguishable.
                # Stop on repeated corroborated conflicts; never rewrite the ledger.
                if corroborated:
                    self.small_decrease_count = (self.small_decrease_count+1
                        if self.small_decrease_at is not None and 0 < now-self.small_decrease_at <= 10 else 1)
                    self.small_decrease_at = now
                    if self.small_decrease_count >= 3:
                        self.blocked = True
                        self.stop_reason = '작은 감소 충돌 3회 · 오인식/실제 감소 확인 후 새 측정'
                        return self.stop_reason
                else:
                    self.small_decrease_count = 0
                    self.small_decrease_at = None
                return '작은 감소값 재확인 · 기존 값 유지'
            self.small_decrease_count = 0
            self.small_decrease_at = None
            if self.decrease_count and previous_observed is not None and now-previous_observed <= 10:
                self.decrease_count += 1
            else:
                self.decrease_candidate, self.decrease_count = value, 1
            if self.decrease_count >= 3:
                self.blocked = True
                self.stop_reason = '유효한 감소 3회 · 사망/레벨 확인 후 새 측정'
                return self.stop_reason
            return '감소값 재확인 · 기존 값 유지'
        self.decrease_candidate, self.decrease_count = None, 0
        self.small_decrease_count = 0
        self.small_decrease_at = None
        confirmed_start = now
        if self.last and exp > self.last[0]:
            gap = max(.001, now-self.samples[-1][0])
            rate = statistics.median(self.positive_rates) if self.positive_rates else 0
            threshold = max(1000000, (self.total or 0)*.005, rate*gap*6)
            if exp-self.last[0] > threshold:
                if self.pending_jump and now-self.pending_jump[1] <= 10 and exp >= self.pending_jump[0][0]:
                    confirmed_start = self.pending_jump[1]
                else:
                    self.pending_jump = (value, now)
                    return '큰 증가값 다음 화면 재검증 중'
        self.pending_jump = None
        if self.last and exp > self.last[0]:
            self.positive_rates.append((exp-self.last[0])/max(.001,now-self.samples[-1][0]))
        self.pending = value
        self.confirmations += 1
        if self.first is None:
            self.first = value
            if self.total is None:
                self.total = exp/(pct/100) if pct > 0 else None
        elif self.started is None and exp > self.first[0]:
            self.started = confirmed_start
            self.first_gain = exp-self.first[0]
            self.establish_rate_anchor(now, exp)
        if self.total is None and pct > 0: self.total = exp/(pct/100)
        self._note_gain(now, exp, self.last[0] if self.last else None)
        self.last = value
        self.samples.append((now, exp))
        self._advance_minute_snapshots()
        while len(self.samples) > 1 and self.samples[1][0] < now-360:
            self.samples.popleft()
        wall = time.time()+now-time.monotonic()
        stamp = time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(wall))+f'.{int(wall%1*1000):03d}'
        self.rows.append((stamp, exp, pct, round(self.elapsed(now),3), exp-self.first[0], 'waiting' if self.started is None else 'hunting'))
        return '기준 확보 · 첫 경험치 증가 대기' if self.started is None else '측정 중 · 숫자/퍼센트 검증'

    def _note_gain(self, stamp, exp, previous_exp):
        """Close a break when EXP rises again after more than IDLE_AFTER seconds."""
        if self.started is None:
            return
        if self.last_gain_at is None:
            self.last_gain_at = stamp
        elif previous_exp is not None and exp > previous_exp:
            gap = stamp-self.last_gain_at
            if gap > IDLE_AFTER:
                self.idle_closed += gap-IDLE_AFTER
            self.last_gain_at = stamp

    def idle_until(self, stamp):
        """Break seconds up to `stamp`, including a break still in progress."""
        if self.started is None or self.last_gain_at is None:
            return 0.0
        return self.idle_closed+max(0.0, stamp-self.last_gain_at-IDLE_AFTER)

    def idle(self, now):
        return self.idle_until(self.ended if self.ended is not None else now)

    def resting(self, now):
        return (self.ended is None and self.last_gain_at is not None
                and now-self.last_gain_at > IDLE_AFTER)

    def active_elapsed(self, now):
        return max(0.0, self.elapsed(now)-self.idle(now))

    def level_up(self, new_level):
        """Continue across a level-up by moving history into the new level's EXP scale.

        On level-up the in-game EXP drops by the old level's requirement, so every
        stored EXP point shifts down by that amount; differences (gained, rates,
        minute totals) stay unchanged and new raw readings continue seamlessly.
        """
        old = self.level
        if self.first is None or self.blocked or old is None or new_level is None or new_level <= old:
            return False
        steps = range(old, new_level)
        if any(EXP_TABLE.get(level) is None for level in steps) or EXP_TABLE.get(new_level) is None:
            return False
        shift = sum(EXP_TABLE[level] for level in steps)
        self.first = (self.first[0]-shift, self.first[1])
        # Percent is not comparable across levels; 0.0 never trips the percent checks.
        self.last = (self.last[0]-shift, 0.0)
        self.samples = deque((stamp, exp-shift) for stamp, exp in self.samples)
        if self.rate_anchor is not None:
            self.rate_anchor = (self.rate_anchor[0], self.rate_anchor[1]-shift)
        self.pending = self.pending_jump = self.baseline_candidate = self.decrease_candidate = None
        self.decrease_count = self.small_decrease_count = self.mismatch_count = 0
        self.small_decrease_at = None
        self.level, self.total = new_level, EXP_TABLE[new_level]
        self.level_ups.append((old, new_level, shift))
        return True

    def _boundary_exp(self, stamp):
        if stamp <= self.started: return float(self.first[0]), 0.0
        points = list(self.samples)
        for (left_t,left_exp),(right_t,right_exp) in zip(points,points[1:]):
            if left_t <= stamp <= right_t:
                if stamp == left_t: return float(left_exp), 0.0
                if stamp == right_t: return float(right_exp), 0.0
                ratio = (stamp-left_t)/(right_t-left_t)
                return left_exp+(right_exp-left_exp)*ratio, right_t-left_t
        return None, None

    def establish_rate_anchor(self, stamp, exp):
        if self.rate_anchor is not None:
            return
        if self.samples and 0 < stamp-self.samples[-1][0] <= 10:
            self.rate_anchor = self.samples[-1]
            self.rate_anchor_kind = 'previous_observation'
        else:
            # Without a recent baseline, exclude the first packet from the rate
            # instead of assigning it zero acquisition time. Total EXP is intact.
            self.rate_anchor = (stamp, exp)
            self.rate_anchor_kind = 'first_increase'

    def rate_estimate(self, now, window=180):
        from rates import estimate
        # Rates are measured to the last observation; count breaks only up to it.
        idle = self.idle_until(self.samples[-1][0]) if self.samples else 0.0
        return estimate(self.samples, self.rate_anchor, now, self.ended, window, idle=idle)

    def _advance_minute_snapshots(self):
        if self.started is None: return
        completed = max(0, int((self.samples[-1][0]-self.started)//60))
        while len(self.minute_snapshots) < completed:
            index = len(self.minute_snapshots)+1
            exp, gap = self._boundary_exp(self.started+index*60)
            cumulative = max(0.0,exp-self.first[0]) if exp is not None and gap <= MAX_BOUNDARY_GAP else None
            previous = self.minute_snapshots[-1] if self.minute_snapshots else {'cumulative':0,'gap':0}
            oldest = self.minute_snapshots[index-7] if index > 6 else {'cumulative':0,'gap':0}
            snapshot = {'index':index,'cumulative':cumulative,
                'one':max(0.0,cumulative-previous['cumulative']) if cumulative is not None and previous['cumulative'] is not None else None,
                'six':max(0.0,cumulative-oldest['cumulative']) if cumulative is not None and oldest['cumulative'] is not None else None,
                'gap':gap, 'estimated':max(gap or 0,previous['gap'] or 0,oldest['gap'] or 0)>5}
            self.minute_snapshots.append(snapshot)
            for key in self.valid_minute:
                if snapshot[key] is not None:
                    self.valid_minute[key] = snapshot

    def minute_result(self):
        return self.minute_snapshots[-1] if self.minute_snapshots else None

    def elapsed(self, now):
        if self.started is None: return 0
        return max(0, (self.ended if self.ended is not None else now) - self.started)

    def finish(self, now, reason):
        if self.ended is None:
            self.ended = now
            self.stop_reason = reason

    def recent_gained(self, seconds, now):
        """Interpolate only the left boundary; hold latest value, never extrapolate.

        Return None when freshness or boundary coverage is insufficient.
        """
        if self.ended is not None: now = self.ended
        if not self.samples: return 0.0
        points = list(self.samples)
        if now - points[-1][0] > 6.0: return None
        cutoff = now - seconds
        if cutoff <= points[0][0]:
            baseline = points[0][1]
        else:
            baseline = points[-1][1]
            for (left_t, left_exp), (right_t, right_exp) in zip(points, points[1:]):
                if left_t <= cutoff <= right_t:
                    if cutoff == left_t:
                        baseline = left_exp
                    elif cutoff == right_t:
                        baseline = right_exp
                    elif right_t - left_t > 10.0:
                        return None
                    else:
                        weight = (cutoff - left_t) / (right_t - left_t)
                        baseline = left_exp + (right_exp - left_exp) * weight
                    break
        return max(0.0, points[-1][1] - baseline)

    def stats(self, now):
        if self.ended is not None: now = self.ended
        if self.first is None:
            return 0, 0, 0, None
        elapsed = self.active_elapsed(now)
        gained = self.last[0] - self.first[0]
        hourly = gained / elapsed * 3600 if elapsed >= 5 else 0
        recent = 0
        if self.samples:
            stamp, exp = self.samples[0]
            recent = (self.last[0] - exp) / max(now-stamp, .001) * 3600
        eta = (self.total-self.last[0]) / hourly * 3600 if hourly > 0 and self.total else None
        return gained, hourly, recent, eta

def load_saved_session(path, level, now):
    """Restore accepted observations as a frozen record; never re-run OCR or filters."""
    path = Path(path)
    with path.open(encoding='utf-8-sig',newline='') as stream:
        rows = list(csv.DictReader(stream))
    if not rows: return None
    last_elapsed = float(rows[-1]['사냥 초'])
    final_elapsed = last_elapsed
    state = {}
    try:
        candidate = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        if (candidate.get('version', 0) >= 4 and candidate.get('samples') == len(rows)
                and candidate.get('baseline') == [int(rows[0]['현재 경험치']), float(rows[0]['퍼센트'])]
                and candidate.get('last') == [int(rows[-1]['현재 경험치']), float(rows[-1]['퍼센트'])]):
            saved_elapsed = float(candidate['elapsed_seconds'])
            if math.isfinite(saved_elapsed) and saved_elapsed >= last_elapsed:
                final_elapsed, state = saved_elapsed, candidate
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    session = Session(level=level)
    last_wall = datetime.fromisoformat(rows[-1]['관측 시각']).timestamp()
    session.session_id = path.stem
    last_observed_at = now-(final_elapsed-last_elapsed)
    hunting_rows = [row for row in rows if row.get('상태') == 'hunting']
    if hunting_rows:
        session.started = now-final_elapsed
        session.first_gain = int(hunting_rows[0]['총 획득 경험치'])
        first_hunt_wall = datetime.fromisoformat(hunting_rows[0]['관측 시각']).timestamp()
        start_wall = first_hunt_wall-float(hunting_rows[0]['사냥 초'])
    # Rebuild EXP from the gained column so records that crossed a level-up
    # (raw EXP drops) restore on the final level's scale; offset is 0 otherwise.
    first_raw = int(rows[0]['현재 경험치'])
    offset = int(rows[-1]['현재 경험치'])-first_raw-int(rows[-1]['총 획득 경험치'])
    for row in rows:
        exp,pct = first_raw+int(row['총 획득 경험치'])+offset,float(row['퍼센트'])
        elapsed = float(row['사냥 초'])
        if session.started is not None:
            stamp = session.started+elapsed if row.get('상태') == 'hunting' else min(session.started-.001, session.started+(datetime.fromisoformat(row['관측 시각']).timestamp()-start_wall))
        else:
            stamp = last_observed_at+(datetime.fromisoformat(row['관측 시각']).timestamp()-last_wall)
        if session.first is None: session.first = (exp,pct)
        if row.get('상태') == 'hunting' and session.rate_anchor is None:
            session.establish_rate_anchor(stamp, exp)
        if row.get('상태') == 'hunting':
            session._note_gain(stamp, exp, session.last[0] if session.last else None)
        session.last = (exp,pct)
        session.samples.append((stamp,exp))
        if row.get('상태') == 'hunting':
            session._advance_minute_snapshots()
        while len(session.samples)>1 and session.samples[1][0]<stamp-360:
            session.samples.popleft()
        session.rows.append((row['관측 시각'],exp,pct,elapsed,int(row['총 획득 경험치']),row.get('상태','hunting')))
    if session.total is None and session.last[1]>0:
        session.total = session.last[0]/(session.last[1]/100)
    session.ended = now
    session.last_observed = session.samples[-1][0]
    session.blocked = bool(state.get('blocked', False))
    session.stop_reason = state.get('stop_reason', '')
    session.restored = True
    session.persisted_rows = len(session.rows)
    return session


class App:
    def __init__(self, root, handoff=None):
        self.root = root
        root.report_callback_exception = self.report_callback_error
        self.region = None
        self.running = False
        self.generation = 0
        self.events = queue.Queue()
        self.session = Session(level=169)
        self.engine = None
        self.last_update = None
        self.scan_seconds = 0
        self.target_seconds = 1.0
        self.preview_frame = None
        self.preview_stamp = None
        self.preview_photo = None
        self.save_warning = ''
        self.audit_warning = ''
        self.worker_ready = False
        self.stop = threading.Event()
        root.title('Planet EXP')
        BASE.mkdir(parents=True, exist_ok=True)
        root.geometry(f'280x492+{max(20, root.winfo_screenwidth()-300)}+90')
        root.configure(bg='#151b24')
        root.overrideredirect(True)
        root.attributes('-topmost', True)
        root.after(100, lambda: root.attributes('-alpha', 0.97))
        root.resizable(False, False)
        self.status = tk.StringVar(value='영역을 지정하고 측정을 시작하세요')
        self.value = tk.StringVar(value='경험치 대기 중')
        self.metrics = tk.StringVar()
        self.canvas = tk.Canvas(root, width=280, height=492, bg='#151b24', highlightthickness=0)
        c = self.canvas
        c.pack(fill='both', expand=True)
        c.create_rectangle(0, 0, 279, 491, outline='#35414d')
        c.create_rectangle(1, 1, 279, 3, fill='#a8edbb', outline='')
        c.create_text(15, 23, text='PLANET', fill='#f0f4f8', anchor='w', font=('Helvetica Neue', 10, 'bold'))
        c.create_text(74, 23, text=f'/ EXP {APP_VERSION}', fill='#84919f', anchor='w', font=('Helvetica Neue', 8))
        c.create_text(185, 23, text='Lv.', anchor='w', fill='#98a7b7', font=('Helvetica Neue', 8))
        self.level_var = tk.IntVar(value=169)
        self.level_picker = tk.Spinbox(root, from_=1, to=219, textvariable=self.level_var, command=self.change_level, width=4, bg='#25303d', fg='#b4e9c6', buttonbackground='#25303d', insertbackground='white', relief='flat', highlightthickness=0, font=('Helvetica Neue', 9))
        self.level_picker.place(x=203, y=14, width=46, height=19)
        self.level_picker.bind('<Return>', lambda e: self.change_level())
        self.level_picker.bind('<FocusOut>', lambda e: self.change_level())
        c.create_text(263, 23, text='×', fill='#84919f', font=('Helvetica Neue', 13), tags='close')
        c.create_text(15, 50, text='총 획득 경험치', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 9))
        self.total_item = c.create_text(14, 78, text='0', fill='#f4f8fc', anchor='w', font=('Helvetica Neue', 25, 'bold'))
        c.create_line(15, 106, 265, 106, fill='#2b3542')
        c.create_text(15, 122, text='1분당 평균', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 9))
        c.create_text(151, 122, text='6분당 평균', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 9))
        self.minute_item = c.create_text(15, 145, text='—', fill='#b4e9c6', anchor='w', font=('Helvetica Neue', 13, 'bold'))
        self.six_item = c.create_text(151, 145, text='—', fill='#b4e9c6', anchor='w', font=('Helvetica Neue', 13, 'bold'))
        self.timer_item = c.create_text(265, 50, text='사냥 00:00:00', fill='#b4e9c6', anchor='e', font=('Helvetica Neue', 9, 'bold'))
        c.create_text(15, 175, text='직전 1분 획득 ≈', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 9))
        c.create_text(151, 175, text='최근 6분 획득 ≈', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 9))
        self.recent_minute_item = c.create_text(15, 198, text='—', fill='#e9eef4', anchor='w', font=('Helvetica Neue', 13, 'bold'))
        self.recent_six_item = c.create_text(151, 198, text='—', fill='#e9eef4', anchor='w', font=('Helvetica Neue', 13, 'bold'))
        self.minute_info_item = c.create_text(15, 218, text='1분마다 집계 · 첫 결과 대기', fill='#84919f', anchor='w', font=('Helvetica Neue', 8))
        c.create_oval(15, 230, 21, 236, fill='#7c8c9d', outline='', tags='indicator')
        self.footer_item = c.create_text(28, 233, text='측정 대기', fill='#84919f', anchor='w', font=('Helvetica Neue', 8))
        c.create_line(15, 247, 265, 247, fill='#2b3542')
        for tag, label, x1, x2, command in (
            ('region', '영역 지정', 15, 80, self.select),
            ('reset', '새 측정', 86, 145, self.reset),
            ('export', 'CSV 저장', 151, 211, self.export),
        ):
            c.create_rectangle(x1, 254, x2, 274, fill='#25303d', outline='', tags=tag)
            c.create_text((x1+x2)/2, 264, text=label, fill='#c4cfda', font=('Helvetica Neue', 8), tags=tag)
            c.tag_bind(tag, '<Button-1>', lambda e, callback=command: callback())
        c.create_rectangle(217, 254, 265, 274, fill='#304e3d', outline='', tags='toggle')
        self.toggle_item = c.create_text(241, 264, text='시작', fill='#c6f1d3', font=('Helvetica Neue', 9, 'bold'), tags='toggle')
        c.tag_bind('close', '<Button-1>', lambda e: self.close())
        c.tag_bind('toggle', '<Button-1>', lambda e: self.toggle())
        c.create_line(15, 284, 265, 284, fill='#2b3542')
        self.current_item = c.create_text(15, 300, text=self.value.get(), fill='#b4e9c6', anchor='w', font=('Helvetica Neue', 9))
        self.baseline_item = c.create_text(15, 316, text='시작 기준 —', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 8))
        self.table_item = c.create_text(15, 332, text='169레벨 통 405,810,702 · 사진 대조', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 8))
        self.detail_item = c.create_text(15, 348, text=self.status.get(), width=250, fill='#a8b5c3', anchor='nw', font=('Helvetica Neue', 8))
        c.create_text(15, 384, text='Medium · 2스레드 · 자동 기록 저장\n목표 1~5초 · CPU 부하 따라 조절\n첫 경험치 증가 확인부터 사냥 시간 측정', fill='#84919f', anchor='nw', font=('Helvetica Neue', 8))
        c.create_text(15, 441, text='실제 인식 영역 · 가려지면 판독 보류', fill='#98a7b7', anchor='w', font=('Helvetica Neue', 8))
        c.create_rectangle(15,453,265,484,fill='#101721',outline='#2b3542')
        self.preview_item = c.create_image(15,453,anchor='nw')
        self.preview_hint = c.create_text(140,468,text='측정 시작 후 표시',fill='#84919f',font=('Helvetica Neue',8))
        def drag_start(event):
            self.drag_origin = (event.x_root-root.winfo_x(), event.y_root-root.winfo_y())
        def drag_move(event):
            if hasattr(self, 'drag_origin'):
                x, y = event.x_root-self.drag_origin[0], event.y_root-self.drag_origin[1]
                if sys.platform == 'darwin':
                    from macos_support import configure_window
                    configure_window(root, (x, y, 280, 492))
                else:
                    root.geometry(f'{x:+d}{y:+d}')
        c.bind('<Button-1>', drag_start)
        c.bind('<B1-Motion>', drag_move)
        c.bind('<ButtonRelease-1>', lambda event: self.save_config())
        config, settings_warning = load_settings(BASE/'settings.json')
        self.region = config.get('region')
        self.session = Session(level=config.get('level', 169))
        self.level_var.set(self.session.level)
        position = config.get('position')
        if position is not None:
            self.place_window(position)
        if sys.platform == 'darwin':
            root.after(150, self.configure_mac_window)
            root.bind('<Command-q>', lambda event: (self.close(), 'break')[1])
            root.createcommand('tk::mac::Quit', self.close)
            menu = tk.Menu(root, tearoff=False)
            menu.add_command(label='화면 기록 권한', command=self.check_permission)
            from macos_support import open_records
            menu.add_command(label='기록 폴더 열기', command=lambda: open_records(BASE/'records'))
            menu.add_separator()
            menu.add_command(label='종료', command=self.close)
            c.bind('<Button-2>', lambda event: menu.tk_popup(event.x_root, event.y_root))
            c.bind('<Control-Button-1>', lambda event: menu.tk_popup(event.x_root, event.y_root))
        self.restore_latest_record()
        if handoff is not None:
            self.resume_handoff(handoff)
        if settings_warning:
            self.status.set(settings_warning)
        threading.Thread(target=self.worker, daemon=True).start()
        root.after(200, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)

    def configure_mac_window(self):
        try:
            from macos_support import configure_window
            configure_window(self.root)
        except Exception as exc:
            log_error('macOS 창 표시', exc)

    def report_callback_error(self, kind, value, tb):
        try:
            (BASE/'callback-error.log').write_text(''.join(traceback.format_exception(kind, value, tb)), encoding='utf-8')
        except OSError:
            pass
        log_error('화면 동작', value)
        self.status.set('화면 동작 오류 · callback-error.log 확인')

    def place_window(self, position):
        import mss
        x, y = position
        with mss.MSS() as capture:
            monitors = capture.monitors[1:]
        if not any(m['left'] <= x < m['left']+m['width']-40 and
                   m['top'] <= y < m['top']+m['height']-40 for m in monitors):
            x, y = max(20, self.root.winfo_screenwidth()-300), 90
        if sys.platform == 'darwin':
            from macos_support import configure_window
            configure_window(self.root, (x, y, 280, 492))
        else:
            self.root.geometry(f'280x492{x:+d}{y:+d}')

    def check_permission(self):
        if sys.platform != 'darwin':
            return True
        from macos_support import ensure_capture_permission
        allowed = ensure_capture_permission(self.root)
        if not allowed:
            self.status.set('화면 기록 권한 허용 후 앱 다시 실행')
        return allowed

    def restore_latest_record(self):
        folder = BASE/'records'
        if not folder.exists(): return
        candidates = sorted(folder.glob('*.csv'),key=lambda path:path.stat().st_mtime,reverse=True)
        for path in candidates:
            try:
                state_path = path.with_suffix('.json')
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if not isinstance(state, dict):
                    continue
                level = state.get('level',self.session.level)
                if level != self.session.level: continue
                restored = load_saved_session(path,level,time.monotonic())
                if restored is None: continue
                self.session = restored
                exp,pct = restored.last
                self.value.set(f'{exp:,} [{pct:.2f}%] · 기록')
                self.status.set('이전 기록 복구 · 시작 시 새 측정')
                return
            except (OSError,ValueError,KeyError,TypeError):
                continue

    def resume_handoff(self, path):
        """Resume only an explicitly supplied, recent update checkpoint in this boot."""
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            stamp = float(data['snapshot_monotonic'])
            if (not 0 <= time.monotonic()-stamp <= 120 or data['running_before_update'] is not True
                    or data['boot_id'] != current_boot_id()
                    or data['session_id'] != self.session.session_id
                    or data['baseline'] != list(self.session.first or ()) or self.session.blocked):
                raise ValueError('Handoff does not match the current saved session')
            restored = load_saved_session(BASE/'records'/(self.session.session_id+'.csv'), self.session.level, stamp)
            if restored is None or (restored.started is None and data.get('phase') != 'waiting'):
                raise ValueError('No hunting session to resume')
            restored.ended = None
            restored.restored = False
            restored.stop_reason = ''
            self.session = restored
            self.running = True
            self.status.set('업데이트 완료 · 기존 측정 이어서 진행')
            return True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log_error('업데이트 기록 이어받기', exc)
            self.status.set('업데이트 기록 확인 필요 · 정지 상태 유지')
            return False

    def change_level(self):
        try:
            level = self.level_var.get()
        except (ValueError, TK_ERROR):
            self.level_var.set(self.session.level or 169)
            return
        if level == self.session.level: return
        if level not in EXP_TABLE:
            self.level_var.set(self.session.level or 169)
            self.status.set('경험치표가 없는 레벨')
            return
        if not self.finish_measurement('레벨 변경'):
            self.level_var.set(self.session.level or 169)
            return
        self.session = Session(level=level)
        self.value.set('경험치 대기 중')
        self.status.set('레벨 변경 · 시작으로 기준 재인식')
        self.save_config()

    def select(self):
        if not self.finish_measurement('영역 재지정'):
            return
        import mss
        try:
            with mss.MSS() as capture:
                monitors = [dict(m) for m in capture.monitors[1:]]
        except Exception as exc:
            log_error('영역 선택', exc)
            self.status.set('화면 정보 확인 실패 · 앱 다시 실행')
            return
        self.selection_windows = []
        def cancel(event=None):
            for win in self.selection_windows:
                if win.winfo_exists(): win.destroy()
            self.selection_windows = []
        for index, monitor in enumerate(monitors):
            self.select_monitor(monitor, index, cancel)

    def select_monitor(self, monitor, index, cancel):
        win = tk.Toplevel(self.root)
        self.selection_windows.append(win)
        win.title(f'Planet EXP Region {index}')
        win.overrideredirect(True)
        win.geometry(f'{monitor["width"]}x{monitor["height"]}{monitor["left"]:+d}{monitor["top"]:+d}')
        win.attributes('-topmost', True)
        # Select directly over the live desktop; never cover it with a capture.
        win.configure(bg='#142536')
        win.attributes('-alpha', 0.25)
        canvas = tk.Canvas(win, bg='#142536', highlightthickness=0, cursor='crosshair')
        canvas.pack(fill='both', expand=True)
        canvas.create_text(20, 30, anchor='w', text='경험치 숫자와 [퍼센트]만 드래그 · ESC 취소', fill='#ffda70', font=('Helvetica Neue', 18, 'bold'))
        selection = {}
        def down(event):
            selection['start'] = (event.x, event.y)
            if 'rect' in selection: canvas.delete(selection['rect'])
            selection['rect'] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='#ff5555', width=2)
        def move(event):
            if 'start' in selection: canvas.coords(selection['rect'], *selection['start'], event.x, event.y)
        def up(event):
            if 'start' not in selection: return
            x, y = selection['start']; left, top = min(x,event.x), min(y,event.y)
            width, height = abs(event.x-x), abs(event.y-y)
            if width < 40 or height < 8: return
            self.region = dict(left=left+monitor['left'], top=top+monitor['top'], width=width, height=height)
            self.save_config()
            self.reset(); self.status.set('영역 저장됨 · 시작 버튼 클릭'); cancel()
        canvas.bind('<Button-1>', down); canvas.bind('<B1-Motion>', move); canvas.bind('<ButtonRelease-1>', up)
        win.bind('<Escape>', cancel)
        win.update_idletasks()
        if sys.platform == 'darwin':
            from macos_support import configure_window
            configure_window(win, (monitor['left'], monitor['top'], monitor['width'], monitor['height']))
        win.attributes('-alpha', 0.25)
        win.focus_force()

    def toggle(self):
        if not self.region:
            self.select(); return
        if self.running:
            self.finish_measurement('사용자 정지')
            self.status.set('정지 · 시작 시 새 측정'); return
        if not self.check_permission():
            return
        if not self.reset():
            return
        self.running = True
        self.status.set('인식기 준비 중')

    def reset(self):
        was_running = self.running
        if not self.finish_measurement('새 측정'):
            self.status.set('저장 실패 · 기존 기록 유지 중')
            return False
        self.generation += 1
        self.session.reset()
        self.running = was_running
        self.value.set('경험치 대기 중')
        self.status.set('기준 인식 대기 · 첫 증가부터 타이머 시작')
        return True

    def finish_measurement(self, reason):
        self.running = False
        self.generation += 1
        if self.session.restored or self.session.first is None:
            return True
        self.session.finish(time.monotonic(), reason)
        try:
            self.save_observation()
            self.save_warning = ''
            return True
        except OSError as exc:
            log_error('최종 기록 저장', exc)
            self.save_warning = '최종 저장 실패 · CSV로 별도 보관'
            self.status.set('저장 실패 · 기록 유지 중 · 저장 위치 확인')
            return False

    def worker(self):
        while not self.stop.is_set():
            try:
                App._worker_run(self)
            except Exception as exc:
                self.worker_ready = False
                log_error('인식 스레드 초기화', exc)
                self.events.put((self.generation, None, '', 0, str(exc), time.monotonic(),
                                 {'initialization_error': True}))
                if self.stop.wait(2):
                    return
            else:
                return

    def _worker_run(self):
        try:
            import numpy as np
            import psutil
            process = psutil.Process()
            try:
                process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
            except (psutil.AccessDenied, OSError):
                pass
            load_control = LoadController()
            reader = None
            previous_capture = None
            psutil.cpu_percent()  # Prime sampling; the first result is not meaningful.
            import mss
            from PIL import Image, ImageOps
            with mss.MSS() as capture:
                self.worker_ready = True
                while not self.stop.is_set():
                    if not self.running:
                        self.stop.wait(.2); continue
                    generation, region = self.generation, dict(self.region or {})
                    try:
                        cycle_started = time.monotonic()
                        cpu_started = sum(process.cpu_times()[:2])
                        system_load = psutil.cpu_percent()
                        if self.engine is None: self.engine = create_ocr_engine()
                        if reader is None: reader = LineReader(self.engine)
                        from temporal_ocr import verify, save_rejected, save_conflict, usable_image
                        def take_frame(force=False):
                            capture_started = time.monotonic()
                            capture_wall = time.time()
                            custom_capture = getattr(self, 'capture_frame', None)
                            image = custom_capture(region) if callable(custom_capture) else capture_image(capture, region)
                            capture_finished = time.monotonic()
                            observed = (capture_started+capture_finished)/2
                            pixels = np.asarray(image)
                            orange = ((pixels[:,:,0]>130)&(pixels[:,:,1]>60)&(pixels[:,:,0]>pixels[:,:,1]*1.1)&(pixels[:,:,2]<80)).mean()
                            blank = not usable_image(image)
                            if blank or orange>.02:
                                value,score,raw,reused = None,0,'화면/설명창 가림 · 판독 보류',False
                                variants=[]
                            else:
                                level = image.info.get('detected_level', self.session.level)
                                value,score,raw,reused=reader.read(image,observed,force=force,level=level)
                                variants=list(reader.last_readings)
                            return dict(image=image,value=value,score=score,raw=raw,reused=reused,
                                        rejection_reason='blank' if blank else 'covered' if orange>.02 else None,
                                        observed_at=observed,covered=bool(orange>.02),variants=variants,
                                        capture_started=capture_started,capture_finished=capture_finished,
                                        capture_wall=capture_wall)
                        force=(self.session.first is None or self.session.pending_jump is not None
                               or self.session.decrease_count>0 or self.session.small_decrease_count>0 or self.session.mismatch_count>0)
                        initial=take_frame(force)
                        observed_level=initial['image'].info.get('detected_level',self.session.level)
                        observed_total=EXP_TABLE.get(observed_level,self.session.total)
                        selected,evidence,frames=verify(initial,take_frame,observed_total,self.session.last,
                            lambda:generation!=self.generation or not self.running or self.stop.is_set(),self.stop.wait,
                            level=observed_level)
                        if evidence['reason']=='cancelled':continue
                        frame=selected or frames[-1]
                        image=frame['image'];observed_at=frame['observed_at']
                        self.preview_frame=(image.copy(),time.monotonic())
                        if previous_capture is not None:self.scan_seconds=observed_at-previous_capture
                        previous_capture=observed_at
                        details=dict(cached=frame['reused'],covered=frame['covered'],image=image.copy(),
                            ocr_engine='rust' if reader.native is not None else 'python',
                            full_game_image=image.info.get('full_game_image'),
                            capture_started_wall=frame['capture_wall'],
                            capture_finished_wall=frame['capture_wall']+frame['capture_finished']-frame['capture_started'],
                            capture_started_monotonic=frame['capture_started'],capture_finished_monotonic=frame['capture_finished'],
                            backend=image.info.get('capture_backend','unknown'),window_region=image.info.get('window_region'),
                            detected_level=image.info.get('detected_level'),auto_detected=image.info.get('auto_detected',False),
                            auto_confirmed=image.info.get('auto_confirmed',False),
                            ui_kind=image.info.get('ui_kind'),game_image_size=image.info.get('game_image_size'),
                            ui_verified_at=image.info.get('ui_verified_at'),
                            window_id=image.info.get('window_id'),window_title=image.info.get('window_title'),
                            window_bounds=image.info.get('window_bounds'),variants=frame['variants'],temporal=evidence,
                            verification_frames=frames or [initial])
                        if selected is None:
                            try:save_rejected(BASE/'records'/'verification-frames',frames,evidence)
                            except OSError as exc:
                                details['verification_save_error']=str(exc);log_error('확인 이미지 저장',exc)
                        elif self.session.last and selected['value'][0] < self.session.last[0]:
                            try:
                                save_conflict(BASE/'records'/'verification-frames',
                                    getattr(self,'last_accepted_evidence',None),frames,evidence,self.session.last,
                                    self.session.session_id,generation)
                            except OSError as exc:
                                details['verification_save_error']=str(exc);log_error('감소 충돌 이미지 저장',exc)
                        self.target_seconds=load_control.period(system_load,sum(process.cpu_times()[:2])-cpu_started)
                        self.events.put((generation,frame['value'] if selected else None,frame['raw'],frame['score'],None,observed_at,details))
                    except Exception as exc:
                        if isinstance(exc, subprocess.TimeoutExpired):
                            reason = '화면 캡처 시간 초과'
                        elif isinstance(exc, subprocess.CalledProcessError):
                            reason = f'화면 캡처 실패 (코드 {exc.returncode})'
                        else:
                            reason = str(exc)
                        log_error('캡처/인식', exc)
                        from auto_hud import HudLevelChanged
                        self.events.put((generation, None, '', 0, reason, time.monotonic(),
                                         {'level_changed':isinstance(exc,HudLevelChanged),
                                          'level_from':getattr(exc,'old',None),'level_to':getattr(exc,'level',None)}))
                    period = self.scan_period() if hasattr(self, 'scan_period') else self.target_seconds
                    self.stop.wait(max(0, period - (time.monotonic() - cycle_started)))
        finally:
            backend = getattr(_capture_state,'backend',None)
            if backend is not None:
                try: backend.close()
                except Exception: pass
                del _capture_state.backend
            _capture_state.disabled = False

    def poll(self):
        if self.worker_ready and self.status.get().startswith('인식기 준비 실패'):
            self.status.set('인식기 준비 완료 · 시작으로 새 측정' if not self.running else '인식기 복구 · 화면 확인 중')
        try:
            while True:
                event = self.events.get_nowait()
                generation, value, raw, score, error = event[:5]
                observed_at = event[5] if len(event) > 5 else time.monotonic()
                details = event[6] if len(event)>6 else {}
                before = len(self.session.rows)
                if generation != self.generation: continue
                if details.get('initialization_error'):
                    self.status.set('인식기 준비 실패 · 2초 후 재시도')
                    continue
                if not self.running: continue
                if error:
                    self.status.set(error[:55]+' · 자동 재시도 중')
                elif value:
                    old_count = len(self.session.rows)
                    previous_value = self.session.last
                    state = self.session.accept(value, observed_at, corroborated=details.get('temporal',{}).get('selected') is not None)
                    if len(self.session.rows) > old_count:
                        self.remember_accepted_capture(previous_value, details)
                        try:
                            self.save_observation()
                            self.save_warning = ''
                        except OSError as exc:
                            log_error('자동 기록 저장', exc)
                            self.save_warning = '자동 저장 실패 · CSV로 별도 보관'
                            state += ' · 자동 저장 실패'
                    self.status.set(f'{state} · 갱신 {self.scan_seconds:.1f}초')
                    if self.session.last:
                        exp,pct = self.session.last
                        self.value.set(f'{exp:,}  [{pct:.2f}%]')
                    if self.session.blocked:
                        self.finish_measurement(state)
                else:
                    self.status.set('설명창 가림 · 경험치 바에서 마우스 이동' if details.get('covered') else '숫자 판독 보류 · 아래 영역/창 가림 확인')
                try:
                    self.save_audit(observed_at,value,raw,score,len(self.session.rows)>before,details)
                    self.audit_warning = ''
                except OSError as exc:
                    log_error('진단 기록 저장', exc)
                    self.audit_warning = '진단 저장 실패 · last-error.log 확인'
        except queue.Empty:
            pass
        gained, hourly, _, _ = self.session.stats(time.monotonic())
        per_minute = hourly / 60
        per_six_minutes = per_minute * 6
        self.metrics.set(f'총 획득 경험치  {gained:,.0f}\n1분당 평균       {per_minute:,.0f}\n6분당 평균       {per_six_minutes:,.0f}')
        c = self.canvas
        now = time.monotonic()
        elapsed = int(self.session.elapsed(now))
        timer = ('기준 확인 중' if self.session.first is None else '첫 증가 대기') if self.session.started is None else f'사냥 {elapsed//3600:02d}:{elapsed%3600//60:02d}:{elapsed%60:02d}'
        c.itemconfigure(self.timer_item, text=timer)
        snapshot = self.session.minute_result()
        if snapshot:
            one, six = self.session.valid_minute['one'], self.session.valid_minute['six']
            c.itemconfigure(self.recent_minute_item,text=f'{one["one"]:,.0f}' if one else '미확정')
            c.itemconfigure(self.recent_six_item,text=f'{six["six"]:,.0f}' if six else '미확정')
            remaining = max(0, math.ceil((snapshot['index']+1)*60-self.session.elapsed(now)))
            quality = '관측 공백 추정' if snapshot['estimated'] else '분별 집계'
            if snapshot['one'] is None or snapshot['six'] is None:
                info = f'공백 미확정 · 1분 {one["index"] if one else "—"}:00 / 6분 {six["index"] if six else "—"}:00'
            else:
                info = f'{quality} {snapshot["index"]}:00 · 다음 {remaining}초' if self.running else f'{snapshot["index"]}:00 집계 · 측정 정지'
            if (self.running and snapshot['one'] is not None and snapshot['six'] is not None
                    and self.session.elapsed(now) >= (snapshot['index']+1)*60):
                info = f'집계 {snapshot["index"]}:00 유지 · 새 관측 대기'
        else:
            text = '첫 1분 대기' if self.session.started is not None else '집계 대기'
            c.itemconfigure(self.recent_minute_item,text=text)
            c.itemconfigure(self.recent_six_item,text=text)
            remaining = max(0,60-int(self.session.elapsed(now)))
            info = f'첫 집계까지 {remaining}초 · 1분마다 갱신' if self.session.started is not None else '첫 증가 후 1분마다 집계'
        c.itemconfigure(self.minute_info_item,text=info)
        c.itemconfigure(self.total_item, text=f'{gained:,.0f}')
        c.itemconfigure(self.minute_item, text=f'{per_minute:,.0f}' if self.session.started is not None and elapsed >= 5 else '계산 중' if self.session.started is not None else '—')
        c.itemconfigure(self.six_item, text=f'{per_six_minutes:,.0f}' if self.session.started is not None and elapsed >= 5 else '계산 중' if self.session.started is not None else '—')
        c.itemconfigure(self.toggle_item, text='정지' if self.running else '새 시작' if self.session.first else '시작', font=('Helvetica Neue', 8, 'bold'))
        warning = self.status.get()
        if not self.running:
            footer = '측정 대기' if self.session.first is None else '이전 기록 · 측정 정지' if self.session.restored else '측정 정지'
        elif self.session.started is None and self.session.first:
            footer = '준비 완료 · 첫 경험치 증가 대기'
        elif '재시도' in warning or '오류' in warning:
            footer = '캡처 재시도 중'
        else:
            footer = f'갱신 {self.scan_seconds:.1f}초 · 목표 {self.target_seconds:.1f}초' if self.session.first else '숫자 확인 중'
        if self.running and self.session.samples and now-self.session.samples[-1][0] > 5:
            footer = f'인식 지연 · {int(now-self.session.samples[-1][0])}초 전 값'
        if self.save_warning or self.audit_warning:
            footer = self.save_warning or self.audit_warning
        c.itemconfigure(self.footer_item, text=footer)
        c.itemconfigure(self.current_item, text=self.value.get())
        baseline = f'시작 기준 {self.session.first[0]:,}' if self.session.first else '시작 기준 —'
        c.itemconfigure(self.baseline_item, text=baseline)
        known = self.session.level is not None and self.session.level in EXP_TABLE
        checked = str(self.session.level) in EXP_METADATA.get('screenshot_verified', {})
        if known:
            suffix = '사진 대조' if checked else '참고표'
            if self.session.last:
                table_text = f'남음 {self.session.total-self.session.last[0]:,} · 통 {self.session.total:,}'
            else:
                table_text = f'{self.session.level}레벨 통 {self.session.total:,} · {suffix}'
        else:
            table_text = '경험치통 추정치 사용'
        c.itemconfigure(self.table_item, text=table_text)
        c.itemconfigure(self.detail_item, text=self.status.get())
        delayed = self.session.samples and now-self.session.samples[-1][0]>5
        c.itemconfigure('indicator', fill='#e7b86e' if self.running and delayed else '#a8edbb' if self.running else '#7c8c9d')
        if self.preview_frame is not None and self.preview_frame[1] != self.preview_stamp:
            from PIL import ImageTk
            frame,self.preview_stamp = self.preview_frame
            self.preview_photo = ImageTk.PhotoImage(frame.resize((250,31)))
            c.itemconfigure(self.preview_item,image=self.preview_photo)
            c.itemconfigure(self.preview_hint,text='')
        self.root.after(200, self.poll)

    def save_config(self):
        try:
            BASE.mkdir(parents=True, exist_ok=True)
            temporary = BASE/'settings.json.tmp'
            temporary.write_text(json.dumps({'region': self.region, 'position': [self.root.winfo_x(), self.root.winfo_y()], 'level': self.session.level}), encoding='utf-8')
            temporary.replace(BASE/'settings.json')
        except OSError as exc:
            log_error('설정 저장', exc)
            self.status.set('창 위치/영역 저장 실패')

    def save_observation(self):
        folder = BASE/'records'
        folder.mkdir(exist_ok=True)
        path = folder/(self.session.session_id+'.csv')
        header = ['관측 시각','현재 경험치','퍼센트','사냥 초','총 획득 경험치','상태']
        temporary = path.with_suffix('.csv.tmp')
        try:
            if self.session.ledger_dirty:
                with temporary.open('w', newline='', encoding='utf-8-sig') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(header)
                    writer.writerows(self.session.rows)
                temporary.replace(path)
            elif self.session.persisted_rows < len(self.session.rows):
                fresh = not path.exists()
                with path.open('a', newline='', encoding='utf-8-sig') as stream:
                    writer = csv.writer(stream)
                    if fresh: writer.writerow(header)
                    writer.writerows(self.session.rows[self.session.persisted_rows:])
            self.session.persisted_rows = len(self.session.rows)
            self.session.ledger_dirty = False
        except OSError:
            self.session.ledger_dirty = True
            raise
        self.save_summary()

    def save_summary(self):
        folder = BASE/'records'
        folder.mkdir(exist_ok=True)
        rows = self.session.rows
        # Raw endpoints match the CSV even after a level-up shifted the session scale.
        summary = {'version':4, 'app_version':APP_VERSION, 'timing':'first_increase',
                   'baseline': [rows[0][1], rows[0][2]] if rows else self.session.first,
                   'last': [rows[-1][1], rows[-1][2]] if rows else self.session.last,
                   'gained': self.session.last[0]-self.session.first[0],
                   'level_ups': [list(step) for step in self.session.level_ups],
                   'elapsed_seconds': self.session.elapsed(time.monotonic()),
                   'active_seconds': self.session.active_elapsed(time.monotonic()),
                   'idle_seconds': self.session.idle(time.monotonic()),
                   'samples': len(self.session.rows), 'blocked': self.session.blocked,
                   'ended': self.session.ended is not None, 'stop_reason':self.session.stop_reason,
                   'level': self.session.level, 'required_exp': self.session.total,
                   'required_exp_screenshot_verified': str(self.session.level) in EXP_METADATA.get('screenshot_verified', {}),
                   'minute_snapshot':self.session.minute_result()}
        temp = folder/(self.session.session_id+'.json.tmp')
        temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(folder/(self.session.session_id+'.json'))

    def remember_accepted_capture(self, previous_value, details):
        if previous_value != self.session.last:
            from temporal_ocr import remember_change
            self.last_accepted_evidence = remember_change(details.get('verification_frames', []),
                details.get('temporal', {}), self.session.session_id, self.generation, self.session.last)

    def save_audit(self, observed_at, value, raw, score, accepted, details):
        folder = BASE/'records'
        folder.mkdir(exist_ok=True)
        record = {'app_version':APP_VERSION,'time':time.time(),'capture_time_estimate':time.time()+observed_at-time.monotonic(),
                  'raw':raw[:180],'value':value,'ocr_score':float(score),'accepted':accepted,
                  'ocr_engine':details.get('ocr_engine'),
                  'cached':details.get('cached',False),'covered':details.get('covered',False),'backend':details.get('backend','unknown'),
                  'reason':self.status.get(),'capture_interval':self.scan_seconds,'target':self.target_seconds,
                  'variants':details.get('variants',[]),'ui_kind':details.get('ui_kind'),
                  'game_image_size':details.get('game_image_size'),'temporal':details.get('temporal'),'verification_save_error':details.get('verification_save_error')}
        record.update(baseline=self.session.first, last=self.session.last,
                      baseline_candidate=self.session.baseline_candidate,
                      hunt_started_monotonic=self.session.started,
                      capture_started_wall=details.get('capture_started_wall'),
                      capture_finished_wall=details.get('capture_finished_wall'))
        with (folder/(self.session.session_id+'-audit.jsonl')).open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(record,ensure_ascii=False)+'\n')
        frame = details.get('image')
        if frame is not None:
            group = 'accepted' if accepted else 'rejected'
            key = (self.session.session_id, frame.size, frame.tobytes())
            if not hasattr(self,'saved_frame_keys'): self.saved_frame_keys = {}
            if self.saved_frame_keys.get(group) != key:
                name = self.session.session_id+('-last-accepted.png' if accepted else '-last-rejected.png')
                frame.save(folder/name)
                self.saved_frame_keys[group] = key

    def export(self):
        target = filedialog.asksaveasfilename(defaultextension='.csv', initialfile='planet-exp.csv', filetypes=[('CSV','*.csv')])
        if not target: return
        temporary = None
        try:
            destination = Path(target)
            with tempfile.NamedTemporaryFile('w', newline='', encoding='utf-8-sig',
                    dir=destination.parent, prefix='.planet-exp-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                writer=csv.writer(stream); writer.writerow(['관측 시각','현재 경험치','퍼센트','사냥 초','총 획득 경험치','상태']); writer.writerows(self.session.rows)
            temporary.replace(destination)
        except OSError as exc:
            log_error('CSV 저장', exc)
            self.status.set('CSV 저장 실패 · 저장 위치/권한 확인')
        else:
            self.status.set('CSV 저장 완료')
        finally:
            if temporary is not None:
                try: temporary.unlink(missing_ok=True)
                except OSError: pass

    def close(self):
        if not self.finish_measurement('앱 종료'):
            return
        self.save_config()
        self.stop.set(); self.root.destroy()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Planet EXP tracker')
    parser.add_argument('--resume-handoff', type=Path, default=None)
    parser.add_argument('--self-test-report', type=Path, default=None)
    options = parser.parse_args()
    if options.self_test_report is not None:
        from diagnostics import run_diagnostics
        run_diagnostics(options.self_test_report)
        sys.exit(0)
    try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError): pass
    try:
        App(tk.Tk(), handoff=options.resume_handoff).root.mainloop()
    except Exception as exc:
        log_error('앱 실행', exc)
        from tkinter import messagebox
        messagebox.showerror('메이플 플래닛 EXP',
                             f'앱을 실행하지 못했어요.\n{exc}\n\n오류 기록: {BASE / "last-error.log"}')
        raise
