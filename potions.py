"""Track quickslot potion / consumable usage from bottom-right hotkey slots.

Each configured slot stores a window-relative ROI, a display name, and an optional unit cost
(meso per item). When the user drags a full square quickslot icon, the reader automatically
focuses on the lower strip where the white black-outlined count digits live.

Only decreases in confirmed count add to `used`; increases (buying/restocking potions) update
the baseline without subtracting from `used`. Pixel-identical crops skip OCR completely.
"""
import hashlib
import re
import threading

COUNT_RE = re.compile(r'\d{1,5}')
MIN_SCORE = 0.80
MAX_COUNT = 99_999
MAX_SLOTS = 8
CONFIRMATION_GAP = 12.0
LARGE_DROP_THRESHOLD = 50
SCAN_INTERVAL = 2.0

_rec_engine = None
_rec_checked = False
_rec_lock = threading.Lock()


def _get_rec_engine():
    """Lazy PP-OCRv6_rec_medium recognizer (far more accurate on outlined pixel digits than Korean mobile)."""
    global _rec_engine, _rec_checked
    if _rec_checked:
        return _rec_engine
    with _rec_lock:
        if not _rec_checked:
            _rec_checked = True
            import tracker
            model = tracker.RESOURCES / 'models' / 'PP-OCRv6_rec_medium.onnx'
            if model.is_file():
                try:
                    import logging
                    from rapidocr import RapidOCR, ModelType
                    _rec_engine = RapidOCR(params={
                        'Global.use_det': False, 'Global.use_cls': False, 'Global.log_level': 'error',
                        'Rec.model_type': ModelType.MEDIUM, 'Rec.model_path': str(model),
                        'EngineConfig.onnxruntime.intra_op_num_threads': 1,
                        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                    })
                    logging.getLogger('RapidOCR').setLevel(logging.ERROR)
                except Exception:
                    _rec_engine = None
        return _rec_engine


def valid_slot(entry):
    """Validate one persisted potion slot dict."""
    from window_capture import valid_roi
    import math

    if not isinstance(entry, dict):
        return False
    name = entry.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 24:
        return False
    cost = entry.get('unit_cost', 0)
    if isinstance(cost, bool) or not isinstance(cost, int) or not (0 <= cost <= 10**9):
        return False
    roi = entry.get('roi')
    aspect = roi.get('reference_aspect') if isinstance(roi, dict) else None
    if not (valid_roi(roi) and type(aspect) in (int, float) and math.isfinite(aspect) and aspect > 0):
        return False
    return True


def clean_slots(entries):
    """Return a sanitized list of valid potion slot dicts."""
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries[:MAX_SLOTS]:
        if valid_slot(entry):
            roi = {k: entry['roi'][k] for k in ('left', 'top', 'width', 'height', 'reference_aspect')}
            result.append({
                'name': ' '.join(entry['name'].split())[:24],
                'unit_cost': int(entry.get('unit_cost', 0)),
                'roi': roi,
            })
    return result


def crop_digit_strip(frame, roi):
    """Crop the digit region for one quickslot ROI (auto-trimming square icon crops to lower half)."""
    from window_capture import pixel_rect

    x, y, w, h = pixel_rect(roi, frame.width, frame.height)
    x = max(0, min(x, frame.width - 1))
    y = max(0, min(y, frame.height - 1))
    w = min(w, frame.width - x)
    h = min(h, frame.height - y)
    if w < 10 or h < 8:
        return None
    # If the user selected the whole square quickslot icon, focus on its lower 48% where digits sit.
    if h >= w * 0.65:
        top = y + round(h * 0.52)
        h = max(8, (y + h) - top)
        y = top
    return frame.crop((x, y, x + w, y + h)).convert('RGB')


def recognize_count(crop, ocr_fn=None):
    """Read an integer potion count (0..99999) from a quickslot digit strip, or None."""
    import numpy as np
    from PIL import Image, ImageOps

    def run_ocr(img):
        if ocr_fn is not None:
            return ocr_fn(img)
        engine = _get_rec_engine()
        if engine is not None:
            with _rec_lock:
                res = engine(np.asarray(img.convert('RGB')), use_det=False, use_rec=True)
            txts = getattr(res, 'txts', None)
            scores = getattr(res, 'scores', None)
            return list(zip(txts, scores)) if txts is not None and scores is not None else []
        import map_name
        return map_name.recognize_lines(img, detect=False, digits_only=True)

    scale = max(2, min(4, round(66 / max(1, crop.height))))
    enlarged = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
    padded_w = ImageOps.expand(enlarged, border=12, fill=(255, 255, 255))
    lines = run_ocr(padded_w)
    val = _parse_lines(lines)
    if val is not None:
        return val
    # Secondary pass with neutral gray padding if white padding missed.
    padded_g = ImageOps.expand(enlarged, border=12, fill=(128, 128, 128))
    return _parse_lines(run_ocr(padded_g))


def _parse_lines(lines):
    if not lines:
        return None
    text = ''.join(str(t) for t, _ in lines).strip().replace(',', '').replace(' ', '')
    scores = [float(s) for _, s in lines]
    if not scores or min(scores) < MIN_SCORE:
        return None
    if not COUNT_RE.fullmatch(text):
        return None
    val = int(text)
    return val if 0 <= val <= MAX_COUNT else None


class SlotConfirmation:
    """Require 2 consecutive sightings (3 for sudden large drops or 0) to confirm a slot count."""

    def __init__(self):
        self.current = None
        self.pending = None
        self.count = 0
        self.last_stamp = None

    def observe(self, value, stamp=None):
        if stamp is not None:
            if self.last_stamp is not None:
                if stamp <= self.last_stamp:
                    return None
                if stamp - self.last_stamp > CONFIRMATION_GAP:
                    self.pending, self.count = None, 0
            self.last_stamp = stamp
        if value is None:
            self.pending, self.count = None, 0
            return None
        if value == self.current:
            self.pending, self.count = None, 0
            return value
        if value == self.pending:
            self.count += 1
        else:
            self.count = 1
        self.pending = value
        needed = 2
        if self.current is not None and (value == 0 or (self.current - value) >= LARGE_DROP_THRESHOLD):
            needed = 3
        if self.count >= needed:
            self.current, self.pending, self.count = value, None, 0
            return value
        return None


class Reader:
    """Hash-cached quickslot reader; skips OCR whenever a slot's pixels are unchanged."""

    def __init__(self, ocr_fn=None):
        self.ocr_fn = ocr_fn
        self._cache = {}  # index -> (size, digest, value)

    def reset(self):
        self._cache.clear()

    def read_slots(self, frame, slots):
        """Return list of (name, value_or_None) for each configured slot."""
        results = []
        for idx, slot in enumerate(slots):
            try:
                crop = crop_digit_strip(frame, slot['roi'])
            except (ValueError, RuntimeError):
                results.append((slot['name'], None))
                continue
            if crop is None:
                results.append((slot['name'], None))
                continue
            digest = hashlib.blake2b(crop.tobytes(), digest_size=16).digest()
            cached = self._cache.get(idx)
            if cached is not None and cached[0] == crop.size and cached[1] == digest:
                results.append((slot['name'], cached[2]))
                continue
            val = recognize_count(crop, ocr_fn=self.ocr_fn)
            self._cache[idx] = (crop.size, digest, val)
            results.append((slot['name'], val))
        return results


class SessionPotions:
    """Track per-slot consumption and cost across a hunting session (restock-safe)."""

    def __init__(self, data=None):
        self.items = {}  # name -> dict(start, end, used, unit_cost, restocks)
        if isinstance(data, dict) and isinstance(data.get('items'), list):
            for entry in data['items']:
                if not isinstance(entry, dict):
                    continue
                name = entry.get('name')
                used = entry.get('used')
                cost = entry.get('unit_cost', 0)
                if (isinstance(name, str) and name and type(used) is int and used >= 0
                        and type(cost) is int and cost >= 0):
                    self.items[name] = {
                        'start': entry.get('start') if type(entry.get('start')) is int else None,
                        'end': entry.get('end') if type(entry.get('end')) is int else None,
                        'used': used,
                        'unit_cost': cost,
                        'restocks': entry.get('restocks', 0) if type(entry.get('restocks')) is int else 0,
                    }

    def observe(self, name, value, unit_cost=0):
        """Record a confirmed slot count; returns True if usage or state changed."""
        if value is None or value < 0:
            return False
        unit_cost = max(0, int(unit_cost or 0))
        entry = self.items.get(name)
        if entry is None:
            self.items[name] = {
                'start': value, 'end': value, 'used': 0,
                'unit_cost': unit_cost, 'restocks': 0,
            }
            return True
        changed = False
        if entry['unit_cost'] != unit_cost:
            entry['unit_cost'] = unit_cost
            changed = True
        prev = entry['end']
        if prev is None:
            entry['start'] = entry['end'] = value
            return True
        if value < prev:
            entry['used'] += (prev - value)
            entry['end'] = value
            return True
        if value > prev:
            entry['restocks'] += 1
            entry['end'] = value
            return True
        return changed

    def total_used(self):
        return sum(item['used'] for item in self.items.values())

    def total_cost(self):
        return sum(item['used'] * item['unit_cost'] for item in self.items.values())

    def summary_text(self, active_seconds=None):
        """One-line display summary for the main panel."""
        if not self.items:
            return ''
        parts = []
        for name, item in self.items.items():
            if item['used'] > 0:
                parts.append(f"{name} {item['used']:,}개")
            elif item['end'] is not None:
                parts.append(f"{name} 0개 (남음 {item['end']:,})")
        if not parts:
            return ''
        text = '포션 · ' + ' · '.join(parts)
        cost = self.total_cost()
        if cost > 0:
            import meso
            text += f' · 비용 -{meso.format_short(cost, signed=False)}'
            if active_seconds and active_seconds >= 60:
                hourly = cost * 3600.0 / active_seconds
                text += f' (시급 -{meso.format_short(hourly, signed=False)})'
        return text

    def as_dict(self):
        if not self.items:
            return None
        items = []
        for name, item in self.items.items():
            items.append({
                'name': name,
                'start': item['start'],
                'end': item['end'],
                'used': item['used'],
                'unit_cost': item['unit_cost'],
                'cost': item['used'] * item['unit_cost'],
                'restocks': item['restocks'],
            })
        return {
            'items': items,
            'total_used': self.total_used(),
            'total_cost': self.total_cost(),
        }
