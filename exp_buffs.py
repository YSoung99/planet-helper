"""Match registered buff icons locally and record only confirmed observations."""
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image

LABELS = {'coupon': '경쿠', 'holy_symbol': '심볼'}
MIN_SCORE = .92
MAX_GAP = 8.0


def template_path(base, kind):
    return Path(base) / 'buff-icons' / f'{kind}.png'


def register(base, kind, source):
    with Image.open(source) as icon:
        w, h = icon.size
        if not (14 <= w <= 256 and 14 <= h <= 256 and .8 <= w / h <= 1.25):
            raise ValueError('우상단의 버프 아이콘 한 칸만 잘라 선택해 주세요.')
        icon = icon.convert('RGB')
        low, high = icon.convert('L').resize((24, 24)).getextrema()
        if high - low < 45:
            raise ValueError('구분할 수 있는 무늬가 있는 아이콘을 선택해 주세요.')
        target = template_path(base, kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temp = Path(stream.name)
                icon.save(stream, format='PNG')
            temp.replace(target)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


@lru_cache(maxsize=8)
def _patches(path, stamp):
    import numpy as np

    with Image.open(path) as source:
        icon = source.convert('RGB')
    patches = {}
    for size in range(25, 46):
        # The countdown covers the bottom of the icon.
        scaled = icon.resize((size, size), Image.Resampling.BILINEAR)
        patch = np.asarray(scaled.crop((3, 3, size - 3, round(size * .65))))
        if patch.std(axis=(0, 1)).max() >= 10:
            patches[size] = patch
    return patches


def buff_corner(frame):
    """The top-right area of the game frame where buff icons are drawn."""
    width, height = frame.size
    return frame.crop((round(width * .42), round(height * .055), width, round(height * .27)))


def available(base):
    return any(template_path(base, kind).is_file() for kind in LABELS)


class Detector:
    def __init__(self):
        self.key = None
        self.last_result = None
        self.scales = {}
        self.locked_scales = {}  # (kind, frame_size) -> matched scale

    def read(self, frame, base):
        matches = dict.fromkeys(LABELS, 'unregistered')
        templates = {}
        for kind in LABELS:
            path = template_path(base, kind)
            try:
                stamp = path.stat().st_mtime_ns
                patches = _patches(path, stamp)
            except (OSError, ValueError):
                continue
            if patches:
                templates[kind] = (path, stamp, patches)
                matches[kind] = 'unknown'
        if not templates:
            return matches

        import cv2
        import hashlib
        import numpy as np

        ratio = 750 / frame.height
        corner = buff_corner(frame)
        dimensions = (round(corner.width * ratio), round(corner.height * ratio))
        if min(dimensions) < 25:
            return matches
        corner = corner.resize(dimensions, Image.Resampling.BILINEAR)
        if corner.mode != 'RGB':
            corner = corner.convert('RGB')
        digest = hashlib.blake2b(corner.tobytes(), digest_size=16).digest()
        key = (dimensions, tuple((kind, path, stamp) for kind, (path, stamp, _) in templates.items()),
               digest)
        if key == self.key:
            return self.last_result.copy()

        pixels = np.asarray(corner)
        locations = {}
        for kind, (_, _, patches) in templates.items():
            locked = self.locked_scales.get((kind, frame.size))
            if locked is not None:
                sizes = [s for s in (locked, locked - 1, locked + 1) if s in patches]
            else:
                previous = self.scales.get(kind, 37)
                sizes = sorted(patches, key=lambda size: abs(size - previous))
            for size in sizes:
                patch = patches[size]
                if patch.shape[0] > pixels.shape[0] or patch.shape[1] > pixels.shape[1]:
                    continue
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(pixels, patch, cv2.TM_CCOEFF_NORMED))
                if score >= MIN_SCORE:
                    self.scales[kind] = size
                    self.locked_scales[(kind, frame.size)] = size
                    matches[kind] = 'active'
                    locations[kind] = point
                    break
        if len(locations) == 2:
            first, second = locations.values()
            if abs(first[0] - second[0]) < 16 and abs(first[1] - second[1]) < 16:
                matches = dict.fromkeys(matches, 'unknown')
        self.key, self.last_result = key, matches.copy()
        return matches


class Confirmation:
    def __init__(self):
        self.previous = {}
        self.stamp = None
        self.source = None
        self.current = {}

    def observe(self, source, stamp, readings):
        consecutive = (source == self.source and self.stamp is not None
                       and 0 < stamp - self.stamp <= MAX_GAP)
        self.current = {
            kind: ('active' if consecutive and self.previous.get(kind) == value == 'active'
                   else value if value in ('unregistered', 'inactive') else 'unknown')
            for kind, value in readings.items()
        }
        confirmed_since = self.stamp if consecutive else None
        self.source, self.stamp, self.previous = source, stamp, readings
        return confirmed_since

    def text(self, generation, now):
        if self.source is None or self.source[0] != generation or now - self.stamp > MAX_GAP:
            return '경쿠 · 심볼 확인 불가'
        words = {'active': '사용 중', 'inactive': '없음', 'unknown': '확인 불가', 'unregistered': '미등록'}
        return ' · '.join(f'{label} {words[self.current[key]]}' for key, label in LABELS.items())


def record(intervals, readings, start, end):
    """Store consecutive positive samples as offsets in seconds from hunt start."""
    if start is None or not start < end or end <= 0:
        return False
    changed = False
    for kind, value in readings.items():
        if value != 'active':
            continue
        start_sec, end_sec = round(max(0, start), 2), round(end, 2)
        if end_sec <= start_sec:
            continue
        spans = intervals.setdefault(kind, [])
        if spans and spans[-1][1] >= start_sec:
            if end_sec <= spans[-1][1]:
                continue
            spans[-1][1] = end_sec
        else:
            spans.append([start_sec, end_sec])
        changed = True
    return changed
