"""Read the hunting-spot name from the minimap title at the top-left of the game.

The minimap shows two Korean lines (region, map), e.g. "사자왕의 성 / 낮은 성벽3".
They are located with the text detector and read with the Korean recognizer.
OCR can misread a character consistently, so names the user corrects in the
record table are remembered: records/map-names.json
    {"auto": {session_id: raw OCR name}, "alias": {raw OCR name: corrected name}}
"""
import json
import re
import threading
from pathlib import Path

import tracker

KOREAN_MODEL = 'korean_PP-OCRv5_rec_mobile.onnx'
DETECT_MODEL = 'PP-OCRv6_det_tiny.onnx'
REGION = (.22, .13)       # minimap title lives in this top-left share of the game frame
MIN_SCORE = .6
HANGUL = re.compile('[가-힣]')

_engine = None
_engine_lock = threading.Lock()


def available(resources=None):
    models = Path(resources or tracker.RESOURCES)/'models'
    return (models/KOREAN_MODEL).is_file() and (models/DETECT_MODEL).is_file()


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
            models = Path(tracker.RESOURCES)/'models'
            _engine = RapidOCR(params={
                'Global.use_cls': False,
                'Det.model_path': str(models/DETECT_MODEL), 'Det.ocr_version': OCRVersion.PPOCRV6,
                'Rec.lang_type': LangRec.KOREAN, 'Rec.ocr_version': OCRVersion.PPOCRV5,
                'Rec.model_type': ModelType.MOBILE, 'Rec.model_path': str(models/KOREAN_MODEL),
                'EngineConfig.onnxruntime.intra_op_num_threads': 1,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1})
        return _engine


def lines_to_name(lines):
    """lines: [(y, text, score)] -> "region · map" from the first two Korean lines, or None."""
    korean = [(y, ' '.join(text.split()), score) for y, text, score in sorted(lines)
              if text and HANGUL.search(text)]
    picked = korean[:2]
    if not picked or min(score for _, _, score in picked) < MIN_SCORE:
        return None
    return ' · '.join(text for _, text, _ in picked)[:60]


def read(image):
    """Return the raw minimap name from a full game frame (PIL image), or None."""
    import numpy as np
    from PIL import Image
    width, height = image.size
    crop = image.convert('RGB').crop((0, 0, int(width*REGION[0]), int(height*REGION[1])))
    crop = crop.resize((crop.width*2, crop.height*2), Image.Resampling.LANCZOS)
    result = _get_engine()(np.asarray(crop))
    if result.txts is None:
        return None
    lines = [(min(point[1] for point in box), text, float(score))
             for box, text, score in zip(result.boxes, result.txts, result.scores)]
    return lines_to_name(lines)


# ---- learned corrections ----

def _path(base=None):
    return Path(base or tracker.BASE)/'records'/'map-names.json'


def load(base=None):
    try:
        data = json.loads(_path(base).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('auto', {}); data.setdefault('alias', {})
    return data


def _save(data, base=None):
    path = _path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def resolve(raw, base=None):
    """Apply a remembered correction to a raw OCR name."""
    return load(base)['alias'].get(raw, raw)


def remember_auto(session_id, raw, base=None):
    data = load(base); data['auto'][session_id] = raw; _save(data, base)


def learn_correction(session_id, corrected, base=None):
    """The user edited an auto-filled memo: map that OCR reading to their text from now on."""
    data = load(base)
    raw = data['auto'].get(session_id)
    if not raw or not corrected or corrected == raw:
        return False
    data['alias'][raw] = corrected
    _save(data, base)
    # Fix other measurements that still carry the same uncorrected reading.
    from record_history import load_memos, save_memo
    memos = load_memos(base)
    for other, other_raw in data['auto'].items():
        if other != session_id and other_raw == raw and memos.get(other) == raw:
            save_memo(other, corrected, base)
    return True
