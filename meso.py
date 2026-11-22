"""Read the meso total from the item inventory and keep confirmed values.

The inventory shows the amount in a white box at its bottom edge, wherever the window is: the
classic UI writes `[coin] [8,890,080] 메소`, the new UI `[coin 메소] [24,252,276]`. A white box
only counts when the word 메소 sits right next to it, which rules out the coin boxes of the trade
window. Nothing is read while the inventory is closed.
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
    mask = cv2.inRange(pixels, (225, 225, 225), (255, 255, 255))
    mask //= 255
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
    MAX_REJECTED = 128

    def __init__(self):
        self.box = None
        self.cached = None
        self.rejected = {}

    @staticmethod
    def signature(frame, box):
        x, y, w, h = box
        reach = round(h * 2.6)
        region = frame.crop((max(0, x - reach), max(0, y - 3),
                             min(frame.width, x + w + reach), min(frame.height, y + h + 3)))
        return frame.size, hashlib.blake2b(region.tobytes(), digest_size=16).digest()

    def read(self, frame, now=None):
        import time
        now = time.monotonic() if now is None else now
        if self.box is not None and self.cached is not None:
            signature = self.signature(frame, self.box)
            if signature == self.cached[0]:
                return self.cached[1]
        old_box = self.box
        self.box = self.cached = None
        self.rejected = {box: item for box, item in self.rejected.items()
                         if now - item[0] < self.RECHECK_SECONDS}

        if old_box is not None and self.check(frame, old_box, now) is not None:
            return self.cached[1]
        for box in white_boxes(frame):
            if box != old_box:
                value = self.check(frame, box, now)
                if value is not None:
                    return value
        return None

    def check(self, frame, box, now):
        x, y, w, h = box
        if x + w > frame.width or y + h > frame.height:
            return None
        signature = self.signature(frame, box)
        rejected = self.rejected.get(box)
        if rejected is not None and rejected[1] == signature:
            return None
        value = read_amount(frame, box)
        if value is not None and has_label(frame, box):
            self.box, self.cached = box, (signature, value)
            return value
        self.rejected.pop(box, None)
        if len(self.rejected) >= self.MAX_REJECTED:
            del self.rejected[next(iter(self.rejected))]
        self.rejected[box] = (now, signature)
        return None


class Confirmation:
    """Accept a value seen twice in a row; a tenfold jump needs a third sighting."""

    def __init__(self):
        self.current = None
        self.pending = None
        self.count = 0
        self.last_stamp = None

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
    """Store sampled balance changes without guessing their cause."""

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
        self.source = self.last_seen = self.opening = None
        self.source_changed = False

    def check_source(self, source):
        if self.source is not None and source != self.source:
            self.source_changed = True
        return not self.source_changed

    def observe(self, value, threshold=TRADE_THRESHOLD):
        if self.start is None:
            self.start = value
        elif abs(value - self.end) > threshold:
            self.large_changes.append(value - self.end)
        self.end = value
        self.readings += 1

    def sample(self, value, source, stamp, opening, started, threshold):
        if self.source is None:
            if not 0 <= stamp - started <= START_WINDOW:
                return False
            self.source = source
        if not self.check_source(source):
            return False
        changed = self.start is None or value != self.end or opening != self.opening
        if changed:
            self.observe(value, threshold)
        self.last_seen, self.opening = stamp, opening
        return changed

    def end_ready(self, source, now):
        return (self.readings >= 2 and not self.source_changed and self.source == source
                and self.last_seen is not None and 0 <= now - self.last_seen <= END_WINDOW)

    def finish(self, source, now):
        self.complete = self.end_ready(source, now)

    def toggle_trade(self, index):
        self.marked.symmetric_difference_update({index})

    def hunted(self):
        if not self.complete or self.readings < 2 or self.start is None or self.end is None:
            return None
        return self.end - self.start - self.excluded_trade()

    def excluded_trade(self):
        if self.legacy:
            return self.trade
        return sum(amount for index, amount in enumerate(self.large_changes) if index in self.marked)

    def as_dict(self):
        if self.start is None:
            return None
        data = {'start': self.start, 'end': self.end, 'trade': self.trade, 'readings': self.readings}
        if not self.legacy:
            data.update(complete=self.complete, large_changes=self.large_changes, marked=sorted(self.marked))
        return data
