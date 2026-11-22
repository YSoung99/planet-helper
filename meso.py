"""Read the meso total from the item inventory and keep confirmed values.

The inventory shows the amount in a white box at its bottom edge, wherever the window is: the
classic UI writes `[coin] [8,890,080] 메소`, the new UI `[coin 메소] [24,252,276]`. A white box
only counts when the word 메소 sits right next to it, which rules out the coin boxes of the trade
window. Nothing is read while the inventory is closed.
"""
import csv
import re
from datetime import datetime
from pathlib import Path

AMOUNT = re.compile(r'\d{1,3}(?:,\d{3})*')
MIN_SCORE = .85
TRADE_THRESHOLD = 1_000_000
# A reading this many times larger or smaller than the last one is usually a dropped or extra digit.
JUMP_RATIO = 10


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

    pixels = np.asarray(frame.convert('RGB'))
    mask = (pixels.min(axis=2) >= 225).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
    boxes = []
    for x, y, w, h, area in stats[1:count]:
        if h < 9 or w < 36 or area < 200:
            continue
        # A box can touch other bright UI through a gap in its border; split at rows that are
        # mostly dark (the border lines) and keep each band that is shaped like a box.
        region = mask[y:y + h, x:x + w]
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


def read_amount(frame, box):
    """The amount inside a white box, or None when it is not a clean number.

    In the new UI the band also catches the edge of the 메소 button, so Hangul is dropped."""
    from PIL import Image, ImageOps
    import map_name

    x, y, w, h = box
    crop = frame.crop((x, y, x + w, y + h)).convert('RGB')
    scale = max(1, round(40 / max(1, h)))
    crop = ImageOps.expand(crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS),
                           border=8, fill=(255, 255, 255))
    lines = map_name.recognize_lines(crop, detect=False)
    if len(lines) != 1:
        return None
    text, score = lines[0]
    text = HANGUL.sub('', text).replace(' ', '').replace('.', ',')
    if score < MIN_SCORE or not AMOUNT.fullmatch(text):
        return None
    return int(text.replace(',', ''))


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
    return any('메소' in text for text, _ in map_name.recognize_lines(crop, detect=True))


class Reader:
    """Find the inventory meso box, remembering where it was last seen."""

    RECHECK_SECONDS = 30.0

    def __init__(self):
        self.box = None
        self.rejected = {}

    def read(self, frame, now=None):
        import time
        now = time.monotonic() if now is None else now
        if self.box is not None:
            value = read_amount(frame, self.box)
            if value is not None:
                return value
            self.box = None
        # The quick number read filters the candidates before the slower label check, and a
        # number box without the label (trade window, NPC dialog) is not checked again for a while.
        self.rejected = {box: when for box, when in self.rejected.items() if now - when < self.RECHECK_SECONDS}
        for box in white_boxes(frame):
            if box in self.rejected:
                continue
            value = read_amount(frame, box)
            if value is None:
                continue
            if has_label(frame, box):
                self.box = box
                return value
            self.rejected[box] = now
        return None


class Confirmation:
    """Accept a value seen twice in a row; a tenfold jump needs a third sighting."""

    def __init__(self):
        self.current = None
        self.pending = None
        self.count = 0

    def observe(self, value):
        """Return the value once it is trusted (again on every later sighting), or None."""
        if value is None:
            self.pending, self.count = None, 0
            return None
        if value == self.current:
            self.pending, self.count = None, 0
            return value
        self.count = self.count + 1 if value == self.pending else 1
        self.pending = value
        needed = 2
        if self.current and value and max(value, self.current) >= JUMP_RATIO * min(value, self.current):
            needed = 3
        if self.count >= needed:
            self.current, self.pending, self.count = value, None, 0
            return value
        return None


def log_path(base):
    return Path(base) / 'records' / 'meso.csv'


def append(base, character, value, when=None):
    path = log_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open('a', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        if new:
            writer.writerow(['시각', '캐릭터', '메소'])
        writer.writerow([(when or datetime.now()).isoformat(timespec='seconds'), character or '', value])


def load(base):
    """[(datetime, character, meso)] in file order; unreadable lines are skipped."""
    entries = []
    try:
        with log_path(base).open(newline='', encoding='utf-8') as stream:
            for row in csv.DictReader(stream):
                try:
                    entries.append((datetime.fromisoformat(row['시각']), row['캐릭터'], int(row['메소'])))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return entries


def day_change(entries, character, day):
    """Last minus first reading of that character on that date, or None with fewer than two."""
    values = [value for when, name, value in entries if name == (character or '') and when.date() == day]
    return values[-1] - values[0] if len(values) >= 2 else None


class SessionMeso:
    """Meso at the start and end of one measurement, with large single changes kept apart."""

    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        self.start = data.get('start')
        self.end = data.get('end')
        self.trade = data.get('trade', 0)
        self.readings = data.get('readings', 1 if self.start is not None else 0)

    def observe(self, value, threshold=TRADE_THRESHOLD):
        if self.start is None:
            self.start = self.end = value
        else:
            if abs(value - self.end) > threshold:
                self.trade += value - self.end
            self.end = value
        self.readings += 1

    def hunted(self):
        """Meso earned while hunting, or None until the inventory was seen twice."""
        if self.readings < 2:
            return None
        return self.end - self.start - self.trade

    def as_dict(self):
        if self.start is None:
            return None
        return {'start': self.start, 'end': self.end, 'trade': self.trade, 'readings': self.readings}
