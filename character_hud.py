"""Read the two character lines next to the confirmed classic HUD level."""
import re
from collections import Counter

from PIL import Image, ImageOps

import map_name
from window_capture import pixel_rect

MIN_SCORE = .95
JOBS = frozenset('초보자 전사 검사 파이터 크루세이더 히어로 페이지 나이트 팔라딘 스피어맨 '
                 '용기사 다크나이트 마법사 매지션 클레릭 프리스트 비숍 위자드 메이지 아크메이지 '
                 '궁수 아처 헌터 레인저 보우마스터 사수 저격수 신궁 도적 로그 어쌔신 허밋 '
                 '나이트로드 시프 시프마스터 섀도어 해적 인파이터 버커니어 바이퍼 건슬링거 '
                 '발키리 캡틴 아란 에반'.split())
# The classic HUD font joins the strokes of 아 in dark-background captures.
JOB_OCR_ALIASES = {'마란': '아란'}


def _confirmed(readings, *, job):
    votes = Counter(text for text, score in readings if score >= MIN_SCORE
                    and re.fullmatch(r'[가-힣A-Za-z0-9]{2,16}', text))
    if job:
        corrected = Counter()
        for text, count in votes.items():
            corrected[JOB_OCR_ALIASES.get(text, text)] += count
        votes = corrected
    if not votes:
        return None
    text, count = votes.most_common(1)[0]
    if job and text not in JOBS:
        return None
    return text if count >= 2 and len(votes) == 1 else None


def read(frame, level_roi, ui_kind):
    if ui_kind != 'classic' or level_roi is None:
        return None, None
    width, height = frame.size
    x, y, w, h = pixel_rect(level_roi, width, height)
    left, right = x + w + round(h * .85), min(width, x + w + round(h * 9))
    middle = y + round(h * .43)
    top, bottom = max(0, middle - round(h * .88)), min(height, middle + round(h * 1.12))
    if right <= left or not top < middle < bottom:
        return None, None

    job = _read_line(frame.crop((left, top, right, middle)), job=True)
    name = _read_line(frame.crop((left, middle, right, bottom)), job=False)
    return job, name


def _read_line(line, *, job):
    readings = []
    if job:
        for resample in (Image.Resampling.BILINEAR, Image.Resampling.NEAREST):
            scaled = line.resize((line.width * 2, line.height * 2), resample)
            readings.extend(map_name.recognize_lines(scaled, detect=False))
        return _confirmed(readings, job=True)
    gray = ImageOps.grayscale(line)
    for variant, resample in (
            (line, Image.Resampling.LANCZOS),
            (gray.point(lambda pixel: 255 if pixel >= 155 else 0), Image.Resampling.NEAREST),
            (gray.point(lambda pixel: 255 if pixel >= 195 else 0), Image.Resampling.LANCZOS)):
        scaled = variant.resize((variant.width * 3, variant.height * 3), resample)
        lines = map_name.recognize_lines(ImageOps.expand(scaled, border=12, fill=0))
        if len(lines) == 1:
            readings.extend(lines)
    return _confirmed(readings, job=False)
