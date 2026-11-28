"""Read the meso total from the item inventory and keep confirmed values.

The inventory shows the amount in a white box at its bottom edge, wherever the window is: the
classic UI writes `[coin] [8,890,080] 메소`, the new UI `[coin 메소] [24,252,276]`. A white box
only counts when the word 메소 sits right next to it, which rules out the coin boxes of the trade
window (unless the user explicitly pinned a manual meso region). Nothing is read while the
inventory is closed.
"""
import csv
import hashlib
import re
from datetime import datetime
from pathlib import Path

AMOUNT = re.compile(r'\d{1,3}(?:,\d{3})*')
MIN_SCORE = .85
TRADE_THRESHOLD = 1_000_000
MIN_THRESHOLD = 1
MAX_THRESHOLD = 10**12
END_WINDOW = 12.0
CONFIRMATION_GAP = 8.0
START_WINDOW = 30.0
PRE_START_WINDOW = 180.0
CONTINUOUS_CONFIRM_SECONDS = 60.0
CHECKPOINT_INTERVAL = 15.0
MAX_CHECKPOINTS = 600
MIN_RATE_SECONDS = 60.0
RECENT_FLOW_WINDOW = 360.0
# A reading this many times larger or smaller than the last one is usually a dropped or extra digit.
JUMP_RATIO = 10
RECHECK = object()


def format_short(value, *, signed=True):
    """Compact Korean meso formatting: +1.23억, +148만, +8,500."""
    if value is None:
        return '—'
    sign = '+' if signed and value > 0 else '-' if value < 0 else ('+' if signed else '')
    mag = abs(value)
    if mag >= 1e8:
        return f'{sign}{mag / 1e8:.2f}억'
    if mag >= 1e4:
        return f'{sign}{mag / 1e4:,.1f}만' if mag < 1e6 else f'{sign}{mag / 1e4:,.0f}만'
    return f'{sign}{mag:,.0f}'


def is_suspicious_jump(previous, value):
    """True when a jump looks like a dropped/extra digit or sudden zero glitch."""
    if previous is None or value is None or previous == value:
        return False
    if value == 0 and previous > 0:
        return True
    if previous > 0 and value > 0:
        if max(value, previous) >= JUMP_RATIO * min(value, previous):
            return True
        if max(value, previous) >= 100_000:
            prev_s, curr_s = str(previous), str(value)
            if abs(len(prev_s) - len(curr_s)) == 1:
                shorter, longer = (prev_s, curr_s) if len(prev_s) < len(curr_s) else (curr_s, prev_s)
                if longer.endswith(shorter) or longer.startswith(shorter):
                    return True
    return False


def sources_compatible(first, second):
    """True when two meso sources refer to the same session, window, and compatible character.

    An empty character name '' means OCR had not resolved the nickname yet; transitioning from ''
    to a confirmed character on the same game window is character resolution, not a character switch.
    """
    if first is None or second is None:
        return False
    if first == second:
        return True
    if len(first) != 3 or len(second) != 3:
        return False
    if first[0] != second[0] or first[2] != second[2]:
        return False
    return (not first[1] or not second[1]) and first[1] is not None and second[1] is not None


def longest_run(flags, bridge=0):
    """(start, end) of the longest stretch of True values, joining gaps up to `bridge` long.

    Digits make some columns of a small box dark; the bridge keeps the box in one piece."""
    runs, start = [], None
    for index, flag in enumerate([*flags, False]):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if runs and start - runs[-1][1] <= bridge:
                runs[-1] = (runs[-1][0], index)
            else:
                runs.append((start, index))
            start = None
    return max(runs, key=lambda run: run[1] - run[0], default=(0, 0))


def white_boxes(frame):
    """Wide, nearly white bands that could hold a meso amount: [(x, y, w, h)]."""
    import cv2
    import numpy as np

    rgb = frame if frame.mode == 'RGB' else frame.convert('RGB')
    pixels = np.asarray(rgb)
    mask = cv2.inRange(pixels, (215, 215, 215), (255, 255, 255))
    if cv2.countNonZero(mask) < 200:
        return []
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    if count <= 1:
        return []
    body = stats[1:count]
    keep = ((body[:, cv2.CC_STAT_HEIGHT] >= 9)
            & (body[:, cv2.CC_STAT_WIDTH] >= 36)
            & (body[:, cv2.CC_STAT_AREA] >= 200))
    boxes = []
    for x, y, w, h, _ in body[keep]:
        # A box can touch other bright UI through a gap in its border; split at rows that are
        # mostly dark (the border lines) and keep each band that is shaped like a box.
        region = mask[y:y + h, x:x + w] > 0
        top = None
        for index, bright in enumerate([*(region.mean(axis=1) >= .35), False]):
            if bright and top is None:
                top = index
            elif not bright and top is not None:
                band = region[top:index]
                height = index - top
                left, right = longest_run(band.mean(axis=0) >= .25, bridge=height // 2)
                width = right - left
                if 9 <= height <= 80 and width >= 4 * height and band[:, left:right].mean() >= .55:
                    boxes.append((int(x) + left, int(y) + top, width, height))
                top = None
    return boxes


HANGUL = re.compile('[가-힣]')


def recognize_amount(crop, *, allow_hangul=False):
    import map_name

    lines = map_name.recognize_lines(crop, detect=False, digits_only=not allow_hangul)
    if len(lines) != 1:
        return None
    text, score = lines[0]
    if allow_hangul:
        text = HANGUL.sub('', text)
    text = ''.join(text.split()).replace('.', ',')
    if score < MIN_SCORE or not AMOUNT.fullmatch(text):
        return None
    return int(text.replace(',', ''))


def read_amount(frame, box):
    """Read the original color strip, which can include the edge of the new UI label."""
    from PIL import Image, ImageOps

    x, y, w, h = box
    crop = frame.crop((x, y, x + w, y + h)).convert('RGB')
    scale = max(1, round(40 / max(1, h)))
    crop = ImageOps.expand(crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS),
                           border=8, fill=(255, 255, 255))
    return recognize_amount(crop, allow_hangul=True)


def amount_glyphs(frame, box):
    """Isolate the right-aligned dark glyphs inside the light amount field."""
    import numpy as np

    x, y, w, h = box
    gray = frame.crop((x, y, x + w, y + h)).convert('L')
    pixels = np.asarray(gray)
    left, right = longest_run((pixels >= 215).mean(axis=0) >= .32, bridge=max(2, round(h * .3)))
    margin = max(1, h // 24)
    if right - left < h or h <= margin * 2:
        return None
    interior = pixels[margin:h - margin, left + margin:right - margin]
    ink = interior < 135
    # A broad opaque strip inside the light field can hide leading or trailing
    # digits. Do not read the surviving suffix as a different total.
    blocked_left, blocked_right = longest_run(ink.mean(axis=0) >= .45)
    if blocked_right - blocked_left >= .85 * h:
        return None
    # Separate the glyph run from the label and any isolated specks to its left.
    columns = np.flatnonzero(ink.any(axis=0))
    if not len(columns):
        return None
    gaps = np.flatnonzero(np.diff(columns) > max(3, round(h * .3)))
    glyph_left = columns[gaps[-1] + 1] if len(gaps) else columns[0]
    glyph_right = columns[-1] + 1
    rows = np.flatnonzero(ink[:, glyph_left:glyph_right].any(axis=1))
    if rows[-1] - rows[0] + 1 < max(4, h // 3):
        return None
    return gray.crop((left + margin + glyph_left, margin + rows[0],
                      left + margin + glyph_right, margin + rows[-1] + 1))


def read_gray_amount(glyphs):
    """Retry the original shading without the empty area or the label beside the digits."""
    from PIL import Image, ImageOps

    scale = max(2, round(40 / glyphs.height))
    scaled = glyphs.resize((glyphs.width * scale, glyphs.height * scale), Image.Resampling.LANCZOS)
    return recognize_amount(ImageOps.expand(scaled, border=(10, 5), fill=255))


def read_binary_amount(glyphs):
    from PIL import Image, ImageOps
    import numpy as np

    binary = glyphs.point(lambda pixel: 0 if pixel < 135 else 255)
    scale = max(2, round(40 / glyphs.height))
    scaled = binary.resize((binary.width * scale, binary.height * scale), Image.Resampling.NEAREST)
    value = recognize_amount(ImageOps.expand(scaled, border=8, fill=255))
    if value is not None:
        return value
    # A narrow glyph such as 0 can become ambiguous after enlargement.
    value = recognize_amount(ImageOps.expand(binary, border=(10, 5), fill=255))
    if value is not None:
        return value
    # Adaptive threshold fallback for slightly lighter/darker font rendering.
    arr = np.asarray(glyphs)
    low, high = int(arr.min()), int(arr.max())
    if high - low >= 40:
        midpoint = (low + high) // 2
        if abs(midpoint - 135) >= 15:
            adaptive = glyphs.point(lambda pixel, t=midpoint: 0 if pixel < t else 255)
            scaled_ad = adaptive.resize((adaptive.width * scale, adaptive.height * scale), Image.Resampling.NEAREST)
            return recognize_amount(ImageOps.expand(scaled_ad, border=8, fill=255))
    return None


def has_label(frame, box):
    """True when the word 메소 is written next to the box, as it is only in the item inventory."""
    from PIL import Image, ImageOps
    import map_name

    x, y, w, h = box
    width, height = frame.size
    reach = round(h * 2.6)
    crop = frame.crop((max(0, x - reach), max(0, y - 3), min(width, x + w + reach), min(height, y + h + 3)))
    scale = max(1, round(40 / max(1, h)))
    crop = ImageOps.expand(crop.convert('RGB').resize((crop.width * scale, crop.height * scale),
                                                      Image.Resampling.LANCZOS), border=12, fill=(0, 0, 0))
    if any('메소' in text for text, _ in map_name.recognize_lines(crop, detect=True)):
        return True

    # In small new UI windows the bright strip starts after the label.
    # In larger windows it can include part of the grey label button.
    if scale > 1:
        left, right = max(0, x - round(h * 2.45)), x
        if right - left < h:
            return False
        detect = False
    else:
        left, right = x, min(width, x + round(h * 1.55))
        detect = True
    label = frame.crop((left, y, right, y + h)).convert('RGB')
    enlarged = label.resize((label.width * 2, label.height * 2), Image.Resampling.LANCZOS)
    enlarged = ImageOps.expand(enlarged, border=12, fill=(0, 0, 0))
    return any(''.join(text.split()) == '메소' and score >= MIN_SCORE
               for text, score in map_name.recognize_lines(enlarged, detect=detect))


class Reader:
    """Find the inventory meso box, remembering where it was last seen or using a manual ROI."""

    RECHECK_SECONDS = 30.0
    MAX_REJECTED = 128

    def __init__(self, arbiter=None, manual_roi=None):
        self.box = None
        self.cached = None
        self.last_glyphs = None
        self.label_verified = None
        self.needs_recheck = False
        self.rejected = {}
        self.arbiter = arbiter
        self.manual_roi = manual_roi
        self.hint = None      # normalized region JEV pointed at, searched when the full frame finds nothing
        self.hinted = None
        self.last_crop = None
        self.last_diag = None

    def set_manual_roi(self, manual_roi):
        self.manual_roi = manual_roi
        self.box = None
        self.cached = None
        self.last_glyphs = None
        self.label_verified = None
        self.rejected.clear()
        self.hinted = None

    @staticmethod
    def signature(frame, box):
        x, y, w, h = box
        reach = round(h * 2.6)
        region = frame.crop((max(0, x - reach), max(0, y - 3),
                             min(frame.width, x + w + reach), min(frame.height, y + h + 3)))
        return frame.size, hashlib.blake2b(region.tobytes(), digest_size=16).digest()

    @staticmethod
    def label_signature(frame, box):
        """Hash only the left and right label strips outside the changing numeric field."""
        x, y, w, h = box
        reach = round(h * 2.6)
        top, bottom = max(0, y - 3), min(frame.height, y + h + 3)
        left_strip = frame.crop((max(0, x - reach), top, x, bottom))
        right_strip = frame.crop((x + w, top, min(frame.width, x + w + reach), bottom))
        digest = hashlib.blake2b(digest_size=16)
        digest.update(left_strip.tobytes())
        digest.update(b'|')
        digest.update(right_strip.tobytes())
        return frame.size, box, digest.digest()

    @staticmethod
    def preview_crop(frame, box):
        x, y, w, h = box
        pad_x = max(4, round(h * 0.4))
        pad_y = max(2, round(h * 0.15))
        return frame.crop((max(0, x - pad_x), max(0, y - pad_y),
                           min(frame.width, x + w + pad_x), min(frame.height, y + h + pad_y))).convert('RGB')

    def read(self, frame, now=None):
        if self.manual_roi is not None:
            return self.read_manual(frame, now)
        value = self.read_frame(frame, now)
        if value is not None or self.needs_recheck or self.hint is None:
            return value
        # Search only where JEV saw the inventory, enlarged when the game window is small.
        from PIL import Image
        left, top, right, bottom = self.hint
        width, height = frame.size
        crop = frame.crop((round(left*width), round(top*height), round(right*width), round(bottom*height)))
        scale = 2 if height < 900 else 1
        if scale > 1:
            crop = crop.resize((crop.width*scale, crop.height*scale), Image.Resampling.LANCZOS)
        if self.hinted is None or self.hinted[0] != self.hint:
            self.hinted = self.hint, Reader(self.arbiter)
        reader = self.hinted[1]
        value = reader.read_frame(crop, now)
        self.needs_recheck = reader.needs_recheck
        if reader.last_crop is not None:
            self.last_crop = reader.last_crop
        if reader.last_diag is not None and (value is not None or self.needs_recheck):
            diag = dict(reader.last_diag)
            diag['mode'] = 'jev_hint'
            self.last_diag = diag
        return value

    def read_manual(self, frame, now=None):
        """Read directly from a user-pinned window-relative ROI while rejecting closed inventory."""
        import time
        import numpy as np
        from window_capture import pixel_rect

        now = time.monotonic() if now is None else now
        self.needs_recheck = False
        rx, ry, rw, rh = pixel_rect(self.manual_roi, frame.width, frame.height)
        rx = max(0, min(rx, frame.width - 1))
        ry = max(0, min(ry, frame.height - 1))
        rw = min(rw, frame.width - rx)
        rh = min(rh, frame.height - ry)
        if rw < 16 or rh < 6:
            self.box = self.cached = None
            self.last_diag = {'mode': 'manual', 'status': 'missing', 'reason': '수동 영역이 너무 작아요',
                              'frame_size': frame.size, 'box': None, 'value': None}
            return None
        roi_box = (rx, ry, rw, rh)
        self.last_crop = frame.crop((rx, ry, rx + rw, ry + rh)).convert('RGB')
        # Fast path: reuse cached reading if the manual ROI pixels did not change.
        if self.box is not None and self.cached is not None:
            signature = self.signature(frame, self.box)
            if signature == self.cached[0]:
                self.last_diag = {'mode': 'manual', 'status': 'ok',
                                  'reason': f'수동 영역 인식 유지 ({self.cached[1]:,})',
                                  'frame_size': frame.size, 'box': self.box, 'value': self.cached[1]}
                return self.cached[1]
        self.box = self.cached = None
        # Look for a white box inside a slightly expanded region around the manual ROI first.
        pad = max(6, rh // 2)
        sx0, sy0 = max(0, rx - pad), max(0, ry - pad)
        sx1, sy1 = min(frame.width, rx + rw + pad), min(frame.height, ry + rh + pad)
        sub = frame.crop((sx0, sy0, sx1, sy1))
        candidates = [(sx0 + bx, sy0 + by, bw, bh) for bx, by, bw, bh in white_boxes(sub)]
        for box in candidates:
            value = self.check(frame, box, now, require_label=False, mode='manual')
            if value is not None or self.needs_recheck:
                return value
        # Fallback: test the exact manual ROI if it has a light background (inventory open).
        gray = np.asarray(self.last_crop.convert('L'))
        if (gray >= 215).mean() >= .40:
            value = self.check(frame, roi_box, now, require_label=False, mode='manual')
            if value is not None or self.needs_recheck:
                return value
            return None
        self.last_diag = {'mode': 'manual', 'status': 'closed',
                          'reason': '수동 영역에 밝은 메소 칸 없음 · 인벤토리가 닫혀 있거나 위치가 바뀜',
                          'frame_size': frame.size, 'box': roi_box, 'value': None}
        return None

    @property
    def located(self):
        """True when the meso box was found on the last read, in the full frame or the JEV hint."""
        return self.box is not None or (self.hinted is not None and self.hinted[1].box is not None)

    def read_frame(self, frame, now=None):
        import time
        now = time.monotonic() if now is None else now
        self.needs_recheck = False
        if self.box is not None and self.cached is not None:
            signature = self.signature(frame, self.box)
            if signature == self.cached[0]:
                self.last_crop = self.preview_crop(frame, self.box)
                self.last_diag = {'mode': 'auto', 'status': 'ok',
                                  'reason': f'인벤토리 메소 칸 인식 유지 ({self.cached[1]:,})',
                                  'frame_size': frame.size, 'box': self.box, 'value': self.cached[1]}
                return self.cached[1]
        old_box = self.box
        self.box = self.cached = None
        self.rejected = {box: item for box, item in self.rejected.items()
                         if now - item[0] < self.RECHECK_SECONDS}

        if old_box is not None:
            value = self.check(frame, old_box, now)
            if value is not None or self.needs_recheck:
                return value
        boxes = white_boxes(frame)
        checked = 0
        for box in boxes:
            if box != old_box:
                checked += 1
                value = self.check(frame, box, now)
                if value is not None or self.needs_recheck:
                    return value
        if not boxes and old_box is None:
            reason = '흰색 칸 없음 · 인벤토리가 닫혀 있거나 가려짐'
        elif checked or old_box is not None:
            reason = f'후보 칸 {len(boxes)}개 확인 · "메소" 라벨 또는 숫자 미일치'
        else:
            reason = '인벤토리 메소 칸 대기 중'
        self.last_diag = {'mode': 'auto', 'status': 'missing', 'reason': reason,
                          'frame_size': frame.size, 'box': None, 'value': None, 'candidates': len(boxes)}
        return None

    def check(self, frame, box, now, *, require_label=True, mode='auto'):
        x, y, w, h = box
        if x + w > frame.width or y + h > frame.height:
            return None
        signature = self.signature(frame, box)
        if require_label:
            rejected = self.rejected.get(box)
            if rejected is not None and rejected[1] == signature:
                return None
        glyphs = amount_glyphs(frame, box)
        if glyphs is None:
            if not require_label:
                self.last_diag = {'mode': mode, 'status': 'occluded',
                                  'reason': '글자 분리 실패 또는 가림 감지 · 판독 보류',
                                  'frame_size': frame.size, 'box': box, 'value': None}
            return None
        key = glyphs.size, glyphs.tobytes()
        reuse = self.last_glyphs is not None and self.last_glyphs[0] == key
        value = self.last_glyphs[1] if reuse else read_amount(frame, box)
        if value is None:
            value = read_gray_amount(glyphs)
        if value is not None:
            label_ok = not require_label
            if require_label:
                label_sig = self.label_signature(frame, box)
                if self.label_verified == label_sig:
                    label_ok = True
                elif has_label(frame, box):
                    self.label_verified = label_sig
                    label_ok = True
            if label_ok:
                self.box = box
                self.last_crop = self.preview_crop(frame, box)
                if not reuse:
                    binary = read_binary_amount(glyphs)
                    if binary != value:
                        judged = self.judge(glyphs, key, value, binary)
                        if judged is None:
                            self.needs_recheck = True
                            b_text = '없음' if binary is None else f'{binary:,}'
                            self.last_diag = {
                                'mode': mode, 'status': 'recheck',
                                'reason': f'원본({value:,}) / 흑백({b_text}) 불일치 · 재확인 중',
                                'frame_size': frame.size, 'box': box, 'value': None,
                                'candidates': (value, binary),
                            }
                            return None
                        value = judged
                self.last_glyphs = key, value
                self.cached = signature, value
                self.last_diag = {'mode': mode, 'status': 'ok',
                                  'reason': f'{"수동 영역" if mode == "manual" else "인벤토리"} 메소 인식 ({value:,})',
                                  'frame_size': frame.size, 'box': box, 'value': value}
                return value
        if require_label:
            self.rejected.pop(box, None)
            if len(self.rejected) >= self.MAX_REJECTED:
                del self.rejected[next(iter(self.rejected))]
            self.rejected[box] = (now, signature)
        else:
            self.last_diag = {'mode': mode, 'status': 'unreadable',
                              'reason': '수동 영역 숫자 판독 보류 · 영역이 정확한지 확인해 주세요',
                              'frame_size': frame.size, 'box': box, 'value': None}
        return None

    def judge(self, glyphs, key, color, binary):
        """When the two readings differ, let JEV pick one of them for these exact glyphs.

        The question runs in the background; until its answer arrives (and when
        JEV is unsure) the balance stays unconfirmed as before.
        """
        if self.arbiter is None or color is None or binary is None:
            return None
        readings = (color, binary)
        question = ('meso', hashlib.blake2b(str(key[0]).encode() + key[1], digest_size=16).digest(), readings)
        found, picked = self.arbiter.answer(question)
        if found:
            return None if picked is None else readings[picked]
        import jev
        self.arbiter.submit(question,
                            lambda: jev.pick_reading(glyphs, [f'{v:,}' for v in readings], 'meso amount'),
                            label='메소 판독', priority=2)
        return None


class Confirmation:
    """Accept a value seen twice in a row; suspicious jumps/digit-drops need a third sighting."""

    def __init__(self):
        self.current = None
        self.pending = None
        self.count = 0
        self.last_stamp = None
        self.pending_since = self.first_seen = None

    def observe(self, value, stamp=None):
        """Return the value once it is trusted (again on every later sighting), or None."""
        if stamp is not None:
            if self.last_stamp is not None:
                if stamp <= self.last_stamp:
                    return None
                if stamp - self.last_stamp > CONFIRMATION_GAP:
                    self.current, self.pending, self.count = None, None, 0
            self.last_stamp = stamp
        if value is None:
            self.pending, self.count = None, 0
            return None
        if value == self.current:
            self.pending, self.count = None, 0
            self.first_seen = stamp
            return value
        if value == self.pending:
            self.count += 1
        else:
            self.count = 1
            self.pending_since = stamp
        self.pending = value
        needed = 3 if is_suspicious_jump(self.current, value) else 2
        if self.count >= needed:
            self.current, self.pending, self.count = value, None, 0
            self.first_seen = self.pending_since
            return value
        return None


def log_path(base):
    return Path(base) / 'records' / 'meso.csv'


def append(base, character, value, when=None):
    path = log_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        if new:
            writer.writerow(['시각', '캐릭터', '메소'])
        writer.writerow([(when or datetime.now()).isoformat(timespec='seconds'), character or '', value])


def iter_entries(base):
    """Read valid balances in file order."""
    try:
        with log_path(base).open(newline='', encoding='utf-8', errors='replace') as stream:
            for row in csv.DictReader(stream):
                try:
                    value = int(row['메소'])
                    when = datetime.fromisoformat(row['시각'])
                    character = row['캐릭터']
                    if value >= 0 and character is not None:
                        yield when, character, value
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        return


def load(base):
    return list(iter_entries(base))


def day_change(entries, character, day):
    """Last minus first reading of that character on that date, or None with fewer than two."""
    values = [value for when, name, value in entries if name == (character or '') and when.date() == day]
    return values[-1] - values[0] if len(values) >= 2 else None


class DailyLog:
    """Keep today's first and last balance per character after the initial file read."""

    def __init__(self):
        self.day = None
        self.balances = {}

    def refresh(self, base, day):
        if self.day == day:
            return
        self.day = day
        self.balances = {}
        for when, character, value in iter_entries(base):
            if when.date() == day:
                self.remember(character, value)

    def remember(self, character, value):
        first, _ = self.balances.get(character, (value, value))
        self.balances[character] = first, value

    def record(self, base, character, value, when):
        self.refresh(base, when.date())
        previous = self.balances.get(character)
        if previous is None or previous[1] != value:
            append(base, character, value, when)
            self.remember(character, value)

    def change(self, character):
        pair = self.balances.get(character)
        return pair[1] - pair[0] if pair else None


class SessionMeso:
    """Store sampled balance changes with exact active-time spans and flow checkpoints."""

    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        self.start = data.get('start')
        self.end = data.get('end')
        self.readings = data.get('readings', 1 if self.start is not None else 0)
        if (type(self.start) is not int or self.start < 0 or type(self.end) is not int
                or self.end < 0 or type(self.readings) is not int or self.readings < 1):
            self.start = self.end = None
            self.readings = 0
        trade = data.get('trade')
        self.trade = trade if type(trade) is int else 0
        changes = data.get('large_changes')
        self.large_changes = changes.copy() if isinstance(changes, list) and all(
            type(change) is int for change in changes) else []
        marks = data.get('marked')
        self.marked = {index for index in marks if type(index) is int and 0 <= index < len(self.large_changes)
                       } if isinstance(marks, list) else set()
        self.complete = data.get('complete', self.readings >= 2) is True
        self.legacy = bool(data) and 'large_changes' not in data
        start_active = data.get('start_active')
        end_active = data.get('end_active')
        if (isinstance(start_active, (int, float)) and isinstance(end_active, (int, float))
                and 0 <= start_active <= end_active):
            self.start_active = float(start_active)
            self.end_active = float(end_active)
        else:
            measured = data.get('measured_seconds')
            if isinstance(measured, (int, float)) and measured > 0:
                self.start_active = 0.0
                self.end_active = float(measured)
            else:
                self.start_active = self.end_active = None
        self.checkpoints = []
        raw_timeline = data.get('timeline')
        if isinstance(raw_timeline, list):
            for pt in raw_timeline:
                if (isinstance(pt, (list, tuple)) and len(pt) == 2
                        and isinstance(pt[0], (int, float)) and type(pt[1]) is int and pt[0] >= 0 and pt[1] >= 0):
                    self.checkpoints.append((float(pt[0]), pt[1]))
        raw_details = data.get('large_change_details')
        self.large_change_details = []
        if isinstance(raw_details, list) and len(raw_details) == len(self.large_changes):
            for entry in raw_details:
                if isinstance(entry, dict) and type(entry.get('amount')) is int:
                    self.large_change_details.append(dict(entry))
        self.large_change_times = [
            entry.get('active') for entry in self.large_change_details
        ] if len(self.large_change_details) == len(self.large_changes) else []
        self.seeded = False
        self.source = self.last_seen = self.opening = None
        self.source_changed = False

    def check_source(self, source):
        if self.source is None:
            return not self.source_changed
        if source is None:
            self.source_changed = True
            return False
        if source == self.source:
            return not self.source_changed
        if sources_compatible(self.source, source):
            if not self.source[1] and source[1]:
                self.source = source
            return not self.source_changed
        self.source_changed = True
        return False

    def _record_checkpoint(self, active_seconds, value):
        if active_seconds is None:
            return
        active = max(0.0, float(active_seconds))
        if self.start_active is None:
            self.start_active = active
        if self.end_active is None or active >= self.end_active:
            self.end_active = active
        if not self.checkpoints:
            self.checkpoints.append((active, value))
            return
        last_t, last_v = self.checkpoints[-1]
        if active < last_t:
            return
        if value != last_v or active - last_t >= CHECKPOINT_INTERVAL:
            self.checkpoints.append((active, value))
            if len(self.checkpoints) > MAX_CHECKPOINTS:
                # Keep the first anchor and full resolution inside RECENT_FLOW_WINDOW; thin older points.
                cutoff = active - RECENT_FLOW_WINDOW
                middle = self.checkpoints[1:-1]
                old = [pt for pt in middle if pt[0] < cutoff]
                recent = [pt for pt in middle if pt[0] >= cutoff]
                if len(old) >= 4:
                    thinned = old[::2]
                else:
                    thinned = old
                    if len(recent) > MAX_CHECKPOINTS - 2:
                        recent = recent[::2]
                self.checkpoints = [self.checkpoints[0]] + thinned + recent + [self.checkpoints[-1]]

    def seed(self, value, source, stamp, opening=0, *, active_seconds=0.0):
        """Seed a pre-hunt confirmed balance from idle scanning before measurement started."""
        if self.start is not None or self.source_changed or source is None:
            return False
        self.source = source
        self.start = self.end = value
        self.readings = 1
        self.last_seen = stamp
        self.opening = opening
        self.seeded = True
        self._record_checkpoint(active_seconds, value)
        return True

    def observe(self, value, threshold=TRADE_THRESHOLD, *, active_seconds=None, when=None):
        if self.start is None:
            self.start = value
        elif abs(value - self.end) > threshold:
            delta = value - self.end
            active_val = max(0.0, float(active_seconds)) if active_seconds is not None else self.end_active
            self.large_changes.append(delta)
            self.large_change_times.append(active_val)
            if active_seconds is not None or when is not None:
                while len(self.large_change_details) < len(self.large_changes) - 1:
                    prev_idx = len(self.large_change_details)
                    self.large_change_details.append({'amount': self.large_changes[prev_idx]})
                detail = {'amount': delta, 'before': self.end, 'after': value}
                if active_val is not None:
                    detail['active'] = round(active_val, 1)
                if when is not None:
                    detail['at'] = when.strftime('%H:%M:%S') if hasattr(when, 'strftime') else str(when)
                self.large_change_details.append(detail)
        self.end = value
        self.readings += 1
        self._record_checkpoint(active_seconds, value)

    def describe_change(self, index):
        """Human-readable label for a large change, using stored metadata when available."""
        amount = self.large_changes[index]
        detail = self.large_change_details[index] if index < len(self.large_change_details) else None
        if isinstance(detail, dict) and 'before' in detail and 'after' in detail:
            prefix_parts = []
            if detail.get('at'):
                prefix_parts.append(str(detail['at'])[:5])
            if isinstance(detail.get('active'), (int, float)):
                mins = int(detail['active'] // 60)
                prefix_parts.append(f'{mins}분')
            prefix = f"{' · '.join(prefix_parts)} | " if prefix_parts else ''
            return f"{prefix}{amount:+,} ({format_short(detail['before'], signed=False)} → {format_short(detail['after'], signed=False)})"
        return f'{amount:+,}'

    def sample(self, value, source, stamp, opening, started, threshold, *,
               first_seen=None, active_seconds=None, when=None):
        if self.source is None:
            if started is not None and stamp < started:
                return False
            self.source = source
        if not self.check_source(source):
            return False
        continuous_ready = (
            self.readings == 1 and self.start_active is not None and active_seconds is not None
            and active_seconds - self.start_active >= CONTINUOUS_CONFIRM_SECONDS
        )
        changed = (self.start is None or value != self.end or opening != self.opening
                   or continuous_ready)
        if changed:
            self.observe(value, threshold, active_seconds=active_seconds, when=when)
        elif active_seconds is not None:
            self._record_checkpoint(active_seconds, value)
        self.last_seen, self.opening = stamp, opening
        return changed

    def end_ready(self, source, now=None):
        return (self.readings >= 2 and not self.source_changed and self.source is not None
                and sources_compatible(self.source, source))

    def finish(self, source, now=None):
        self.complete = self.end_ready(source, now)

    def toggle_trade(self, index):
        self.marked.symmetric_difference_update({index})

    def observed_delta(self):
        if self.readings < 2 or self.start is None or self.end is None:
            return None
        return self.end - self.start - self.excluded_trade()

    def hunted(self):
        return self.observed_delta() if self.complete else None

    def excluded_trade(self):
        if self.legacy:
            return self.trade
        return sum(amount for index, amount in enumerate(self.large_changes) if index in self.marked)

    def _excluded_trade_between(self, start_active, end_active):
        if self.legacy or not self.marked:
            return 0
        total = 0
        for index in self.marked:
            if index < len(self.large_changes) and index < len(self.large_change_times):
                when = self.large_change_times[index]
                if when is not None and start_active < when <= end_active:
                    total += self.large_changes[index]
        return total

    def measured_seconds(self, fallback_active=None):
        if self.start_active is not None and self.end_active is not None and self.end_active > self.start_active:
            return self.end_active - self.start_active
        if fallback_active is not None and fallback_active > 0:
            return float(fallback_active)
        return None

    def rate(self, fallback_active=None, *, min_seconds=MIN_RATE_SECONDS):
        """Return cumulative and recent-window meso rates based on measured active hunting time."""
        delta = self.observed_delta()
        seconds = self.measured_seconds(fallback_active)
        result = {
            'delta': delta,
            'seconds': seconds,
            'per_minute': None,
            'per_hour': None,
            'ready': False,
            'recent_delta': None,
            'recent_seconds': None,
            'recent_per_hour': None,
            'trend_ratio': None,
        }
        if delta is None or seconds is None or seconds <= 0:
            return result
        if seconds >= min_seconds:
            per_minute = delta * 60.0 / seconds
            per_hour = delta * 3600.0 / seconds
            result.update(per_minute=per_minute, per_hour=per_hour, ready=True)
        # Recent flow (e.g., last ~6 minutes of active hunting) when intermediate checkpoints exist.
        if len(self.checkpoints) >= 3:
            t_end, v_end = self.checkpoints[-1]
            target = t_end - RECENT_FLOW_WINDOW
            # Pick the checkpoint closest to target with at least min_seconds of span, excluding the very first
            # anchor if it covers almost the entire measurement (no distinct recent sub-window).
            candidates = [
                (t, v) for t, v in self.checkpoints[:-1]
                if t_end - t >= min_seconds and (seconds <= 0 or (t_end - t) < seconds * 0.85)
            ]
            if candidates:
                t_anchor, v_anchor = min(candidates, key=lambda pt: abs(pt[0] - target))
                dt = t_end - t_anchor
                recent_delta = (v_end - v_anchor) - self._excluded_trade_between(t_anchor, t_end)
                recent_hour = recent_delta * 3600.0 / dt
                trend = None
                if result['per_hour'] and abs(result['per_hour']) >= 1000:
                    trend = (recent_hour - result['per_hour']) / abs(result['per_hour'])
                result.update(
                    recent_delta=recent_delta,
                    recent_seconds=dt,
                    recent_per_hour=recent_hour,
                    trend_ratio=trend,
                )
        return result

    def as_dict(self):
        if self.start is None:
            return None
        data = {'start': self.start, 'end': self.end, 'trade': self.trade, 'readings': self.readings}
        if not self.legacy:
            data.update(complete=self.complete, large_changes=self.large_changes, marked=sorted(self.marked))
            if self.large_change_details and len(self.large_change_details) == len(self.large_changes):
                data['large_change_details'] = [dict(d) for d in self.large_change_details]
            if self.start_active is not None and self.end_active is not None:
                data['start_active'] = round(self.start_active, 2)
                data['end_active'] = round(self.end_active, 2)
                data['measured_seconds'] = round(max(0.0, self.end_active - self.start_active), 2)
                if len(self.checkpoints) >= 2:
                    pts = self.checkpoints
                    if len(pts) > 120:
                        step = max(1, (len(pts) - 2) // 118)
                        pts = [pts[0]] + pts[1:-1:step] + [pts[-1]]
                    data['timeline'] = [[round(t, 1), v] for t, v in pts]
        return data
