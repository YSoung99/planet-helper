"""Track quickslot potion / consumable usage by potion icon image (hotkey-agnostic) or ROI.

Each configured slot stores a display name, optional unit cost (meso per item), an icon template
filename saved under ``records/potion-icons/`` (matched across the bottom-right quickslot bar
with the top-left hotkey text and bottom digit strip masked out), and an optional fallback ROI.

Because matching uses only the potion artwork, moving the potion to a different hotkey or
switching characters still tracks the exact same potion automatically—and if the potion is not
on the quickslot bar, it is cleanly skipped instead of reading a different skill or item.
"""
from functools import lru_cache
import hashlib
import math
import re
import threading
import time
from pathlib import Path

COUNT_RE = re.compile(r'\d{1,5}')
MIN_SCORE = 0.80
ICON_MATCH_THRESHOLD = 0.78
MAX_COUNT = 99_999
MAX_SLOTS = 8
CONFIRMATION_GAP = 12.0
LARGE_DROP_THRESHOLD = 50
SCAN_INTERVAL = 2.0
# Bottom-right quickslot search region (normalized x0, y0, x1, y1).
QUICKSLOT_SEARCH = (0.68, 0.72, 1.0, 1.0)
ICON_FILE_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}\.png$')

_rec_engine = None
_rec_checked = False
_rec_lock = threading.Lock()


def _get_rec_engine():
    """Lazy PP-OCRv6_rec_medium recognizer (accurate on outlined pixel digits)."""
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


def icon_dir(base=None):
    import tracker
    return Path(base or tracker.BASE) / 'records' / 'potion-icons'


def icon_path(filename, base=None):
    return icon_dir(base) / filename


def save_icon(image, base=None):
    """Save a cropped potion slot RGB image as PNG and return its filename."""
    folder = icon_dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    name = f'potion-{time.time_ns()}.png'
    image.convert('RGB').save(folder / name)
    return name


def delete_icon(filename, base=None):
    if filename and ICON_FILE_RE.fullmatch(str(filename)):
        try:
            icon_path(filename, base).unlink(missing_ok=True)
        except OSError:
            pass


def valid_slot(entry):
    """Validate one persisted potion slot dict (supports icon-based and/or ROI-based slots)."""
    from window_capture import valid_roi

    if not isinstance(entry, dict):
        return False
    name = entry.get('name')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 24:
        return False
    cost = entry.get('unit_cost', 0)
    if isinstance(cost, bool) or not isinstance(cost, int) or not (0 <= cost <= 10**9):
        return False
    icon = entry.get('icon')
    has_icon = isinstance(icon, str) and bool(ICON_FILE_RE.fullmatch(icon))
    roi = entry.get('roi')
    has_roi = False
    if roi is not None:
        aspect = roi.get('reference_aspect') if isinstance(roi, dict) else None
        has_roi = bool(valid_roi(roi) and type(aspect) in (int, float) and math.isfinite(aspect) and aspect > 0)
    if not (has_icon or has_roi):
        return False
    ref_h = entry.get('reference_height')
    if ref_h is not None and (isinstance(ref_h, bool) or not isinstance(ref_h, (int, float)) or not (100 <= ref_h <= 10000)):
        return False
    return True


def clean_slots(entries):
    """Return a sanitized list of valid potion slot dicts."""
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries[:MAX_SLOTS]:
        if valid_slot(entry):
            cleaned = {
                'name': ' '.join(entry['name'].split())[:24],
                'unit_cost': int(entry.get('unit_cost', 0)),
            }
            if isinstance(entry.get('icon'), str) and ICON_FILE_RE.fullmatch(entry['icon']):
                cleaned['icon'] = entry['icon']
            if entry.get('reference_height') is not None:
                cleaned['reference_height'] = int(round(entry['reference_height']))
            if isinstance(entry.get('roi'), dict):
                cleaned['roi'] = {k: entry['roi'][k] for k in ('left', 'top', 'width', 'height', 'reference_aspect')}
            result.append(cleaned)
    return result


@lru_cache(maxsize=32)
def icon_mask(width, height):
    """3-channel mask keeping pure potion art while ignoring top-left hotkey text and bottom digits."""
    import cv2
    import numpy as np

    mask = np.full((height, width), 255, dtype=np.uint8)
    # Ignore top-left hotkey label (A, Del, Shift, Home, etc.)
    mask[:int(height * 0.38), :int(width * 0.65)] = 0
    # Ignore bottom count digits
    mask[int(height * 0.58):, :] = 0
    return cv2.merge([mask, mask, mask])


def _prepare_search(frame):
    """Crop and convert the bottom-right quickslot search area once per frame."""
    import numpy as np
    fw, fh = frame.size
    sx0 = int(fw * QUICKSLOT_SEARCH[0])
    sy0 = int(fh * QUICKSLOT_SEARCH[1])
    search_pil = frame.crop((sx0, sy0, fw, fh))
    if search_pil.mode != 'RGB':
        search_pil = search_pil.convert('RGB')
    return np.asarray(search_pil), sx0, sy0


def find_icon(frame, template_rgb, *, reference_height=None, threshold=ICON_MATCH_THRESHOLD, _search=None):
    """Locate `template_rgb` inside the bottom-right quickslot area, ignoring hotkey text & digits.

    Returns (x, y, w, h, score) in full-frame pixel coordinates, or None when not found.
    """
    import cv2
    import numpy as np
    from PIL import Image

    fw, fh = frame.size
    search, sx0, sy0 = _prepare_search(frame) if _search is None else _search
    sh_s, sw_s = search.shape[:2]

    tw, th = template_rgb.size
    if reference_height and reference_height > 0:
        ratio = fh / float(reference_height)
        if abs(ratio - 1.0) >= 0.04:
            tw = max(12, round(tw * ratio))
            th = max(12, round(th * ratio))
            template_rgb = template_rgb.resize((tw, th), Image.Resampling.LANCZOS)
    if tw >= sw_s or th >= sh_s or tw < 10 or th < 10:
        return None
    tmpl = np.asarray(template_rgb if template_rgb.mode == 'RGB' else template_rgb.convert('RGB'))
    mask3 = icon_mask(tw, th)
    res = cv2.matchTemplate(search, tmpl, cv2.TM_CCOEFF_NORMED, mask=mask3)
    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, best_val, _, best_loc = cv2.minMaxLoc(res)
    if best_val < threshold:
        return None
    bx, by = best_loc
    return (sx0 + bx, sy0 + by, tw, th, float(best_val))


def _box_digit_strip(frame, box):
    """Crop the digit strip from a pixel box (x, y, w, h); trims square icon boxes to lower 48%."""
    x, y, w, h = box
    x = max(0, min(x, frame.width - 1))
    y = max(0, min(y, frame.height - 1))
    w = min(w, frame.width - x)
    h = min(h, frame.height - y)
    if w < 10 or h < 8:
        return None
    if h >= w * 0.65:
        top = y + round(h * 0.52)
        h = max(8, (y + h) - top)
        y = top
    return frame.crop((x, y, x + w, y + h)).convert('RGB')


def crop_digit_strip(frame, roi):
    """Crop the digit region for one quickslot ROI (auto-trimming square icon crops to lower half)."""
    from window_capture import pixel_rect

    return _box_digit_strip(frame, pixel_rect(roi, frame.width, frame.height))


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
    val = _parse_lines(run_ocr(padded_w))
    if val is not None:
        return val
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
    """Icon-matching + hash-cached quickslot reader; skips matching & OCR when pixels are unchanged."""

    def __init__(self, ocr_fn=None, base=None):
        self.ocr_fn = ocr_fn
        self.base = base
        self._cache = {}       # index -> (size, digest, value)
        self._templates = {}   # filename -> PIL RGB Image or None
        self._last_boxes = {}  # index -> (frame_size, box, full_slot_digest)

    def reset(self):
        self._cache.clear()
        self._templates.clear()
        self._last_boxes.clear()

    def _load_template(self, filename):
        if filename in self._templates:
            return self._templates[filename]
        from PIL import Image
        path = icon_path(filename, self.base)
        try:
            with Image.open(path) as img:
                loaded = img.convert('RGB').copy()
        except OSError:
            loaded = None
        self._templates[filename] = loaded
        return loaded

    def _locate_slot_box(self, idx, frame, slot, search_cache):
        """Return pixel box (x, y, w, h) for a slot via icon matching (with fast cache) or ROI."""
        icon_file = slot.get('icon')
        if icon_file:
            template = self._load_template(icon_file)
            if template is None:
                return None
            # Fast path: if the previously matched box still has identical pixels, reuse it directly.
            prev = self._last_boxes.get(idx)
            if prev is not None and prev[0] == frame.size:
                bx, by, bw, bh = prev[1]
                if bx + bw <= frame.width and by + bh <= frame.height:
                    patch = frame.crop((bx, by, bx + bw, by + bh))
                    if patch.mode != 'RGB':
                        patch = patch.convert('RGB')
                    digest = hashlib.blake2b(patch.tobytes(), digest_size=16).digest()
                    if digest == prev[2]:
                        return prev[1]
                    # Check if the icon art at the same position still matches (only digits changed!).
                    import cv2
                    import numpy as np
                    tmpl = template
                    if tmpl.size != (bw, bh):
                        from PIL import Image
                        tmpl = tmpl.resize((bw, bh), Image.Resampling.LANCZOS)
                    res = cv2.matchTemplate(np.asarray(patch), np.asarray(tmpl),
                                            cv2.TM_CCOEFF_NORMED, mask=icon_mask(bw, bh))
                    score = float(np.nan_to_num(res[0, 0], nan=-1.0))
                    if score >= ICON_MATCH_THRESHOLD:
                        self._last_boxes[idx] = (frame.size, prev[1], digest)
                        return prev[1]
            if not search_cache:
                search_cache.append(_prepare_search(frame))
            found = find_icon(frame, template, reference_height=slot.get('reference_height'),
                              _search=search_cache[0])
            if found is None:
                self._last_boxes.pop(idx, None)
                return None
            box = found[:4]
            patch = frame.crop((box[0], box[1], box[0] + box[2], box[1] + box[3])).convert('RGB')
            digest = hashlib.blake2b(patch.tobytes(), digest_size=16).digest()
            self._last_boxes[idx] = (frame.size, box, digest)
            return box
        roi = slot.get('roi')
        if roi is not None:
            from window_capture import pixel_rect
            try:
                return pixel_rect(roi, frame.width, frame.height)
            except (ValueError, RuntimeError):
                return None
        return None

    def read_slots(self, frame, slots):
        """Return list of (name, value_or_None) for each configured slot."""
        results = []
        search_cache = []
        for idx, slot in enumerate(slots):
            box = self._locate_slot_box(idx, frame, slot, search_cache)
            if box is None:
                results.append((slot['name'], None))
                continue
            crop = _box_digit_strip(frame, box)
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
