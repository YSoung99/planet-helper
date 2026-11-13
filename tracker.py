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


DIAGNOSTIC_SUFFIXES = ('-audit.jsonl', '-last-accepted.png', '-last-rejected.png')


def prune_diagnostics(base=None, keep=20, log_limit=1_000_000, log_keep=200_000):
    """Keep diagnostics for the newest `keep` measurements and cap launch.log.

    Hunting records (CSV/JSON) are never touched. Returns the number of files removed.
    """
    base = Path(base or BASE)
    folder = base/'records'
    removed = 0
    if folder.is_dir():
        sessions = {}
        for path in folder.iterdir():
            for suffix in DIAGNOSTIC_SUFFIXES:
                if path.is_file() and path.name.endswith(suffix):
                    sessions.setdefault(path.name[:-len(suffix)], []).append(path)
        for session_id in sorted(sessions)[:-keep or None] if keep else sorted(sessions):
            for path in sessions[session_id]:
                path.unlink(missing_ok=True); removed += 1
    log = base/'launch.log'
    try:
        if log.stat().st_size > log_limit:
            # Rewrite in place: the launcher appends through an O_APPEND descriptor.
            with log.open('r+b') as stream:
                stream.seek(-log_keep, 2); tail = stream.read()
                tail = tail[tail.find(b'\n')+1:]
                stream.seek(0); stream.write(tail); stream.truncate()
    except OSError:
        pass
    return removed


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


class MeasurementCore:
    """Measurement, OCR worker and record persistence shared by the Qt apps.

    The UI lives in qt_controller.SharedController, which subclasses this.
    """
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
        except (ValueError, TypeError):
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


# Backwards-compatible name used by tests and diagnostics.
App = MeasurementCore
