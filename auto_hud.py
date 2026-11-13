"""Locate a Maple HUD using local pixels; never infer a level from EXP alone."""
import math
import re
import time
from collections import Counter
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps


class HudNotFound(ValueError):
    pass


class HudLevelChanged(ValueError):
    def __init__(self, old, new):
        self.old, self.level = old, new
        super().__init__(f'레벨 {old} → {new} 확인 · 현재 기록 보존 · 새 측정 필요')


@dataclass
class Hud:
    level: int
    value: tuple
    exp_roi: dict
    level_roi: dict
    window_id: object
    size: tuple
    ui_kind: str = 'unknown'


def normalized(box, size):
    x, y, right, bottom = box
    w, h = size
    return dict(left=x/w, top=y/h, width=(right-x)/w, height=(bottom-y)/h,
                reference_aspect=w/h)


def overlap(a, b):
    left, top = max(a['left'], b['left']), max(a['top'], b['top'])
    right = min(a['left']+a['width'], b['left']+b['width'])
    bottom = min(a['top']+a['height'], b['top']+b['height'])
    shared = max(0, right-left)*max(0, bottom-top)
    union = a['width']*a['height']+b['width']*b['height']-shared
    return shared/union if union else 0


class Confirmation:
    """Require three agreeing observations of this window, layout and level."""
    def __init__(self):
        self.previous = None
        self.count = 0
        self.at = 0

    def clear(self):
        self.previous = None
        self.count = 0

    def accept(self, hud, now):
        old = self.previous
        agrees = (old is not None and 0 < now-self.at <= 8
                  and old.window_id == hud.window_id and old.size == hud.size
                  and old.level == hud.level and overlap(old.exp_roi, hud.exp_roi) >= .65
                  and old.ui_kind == hud.ui_kind
                  and overlap(old.level_roi, hud.level_roi) >= .65
                  and old.value[0] <= hud.value[0] and old.value[1] <= hud.value[1])
        self.count = self.count+1 if agrees else 1
        self.previous, self.at = hud, now
        return self.count >= 3


def recognize(engine, image, scale=3):
    # Keep the same local recognition model as the production app.
    image = ImageOps.expand(image.resize((image.width*scale, image.height*scale),
                                        Image.Resampling.NEAREST), 16, fill=0)
    result = engine(np.asarray(image.convert('RGB')))
    texts = result.txts if result.txts is not None else ()
    scores = result.scores if result.scores is not None else ()
    return ''.join(texts).strip(), min(scores) if len(scores) else 0


def color_groups(image, kind):
    """Find digit plates or legacy yellow glyphs; dimensions follow HUD scale."""
    import cv2
    w, h = image.size
    start = math.floor(h*.90)
    a = np.asarray(image.crop((0, start, math.ceil(w*.30), h)).convert('RGB'))
    if kind == 'orange':
        mask = ((a[:, :, 0] > 160) & (a[:, :, 1] > 55) & (a[:, :, 1] < 200)
                & (a[:, :, 2] < 100) & (a[:, :, 0] > a[:, :, 1]*1.3))
    else:
        mask = (a[:, :, 0] > 170) & (a[:, :, 1] > 160) & (a[:, :, 2] < 110)
    _, _, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'))
    parts = []
    for x, y, cw, ch, area in stats[1:]:
        if (.009*h <= ch <= .055*h and 2 <= cw <= .07*w
                and area >= 15 and y+start >= .93*h):
            parts.append([int(x), int(y+start), int(x+cw), int(y+start+ch)])
    parts.sort()
    groups = []
    for box in parts:
        bh = box[3]-box[1]
        for group in reversed(groups):
            gh = group[3]-group[1]
            if (0 <= box[0]-group[2] <= max(bh, gh)*.8
                    and abs(box[1]-group[1]) <= max(bh, gh)*.20
                    and min(bh, gh)/max(bh, gh) >= .65):
                group[0] = min(group[0], box[0]); group[1] = min(group[1], box[1])
                group[2] = max(group[2], box[2]); group[3] = max(group[3], box[3])
                break
        else:
            groups.append(box)
    return [g for g in groups if g[2]-g[0] <= w*.13]


class HudLocator:
    def __init__(self, engine, detector=None):
        self.engine = engine
        self.detector = detector

    def read_level(self, image):
        import tracker
        levels = []
        w, h = image.size
        for kind in ('orange', 'yellow'):
            for x, y, right, bottom in color_groups(image, kind)[:10]:
                dh = bottom-y
                # A level must have a visible LV anchor directly to its left.
                anchor_box = (max(0, x-round(dh*3.2)), max(0, y-3),
                              min(w, right+2), min(h, bottom+3))
                anchored, _ = recognize(self.engine, image.crop(anchor_box))
                if not re.match(r'^\s*L\s*V[.:\s]', anchored, re.I):
                    continue
                box = (max(0, x-2), max(0, y-2), min(w, right+2), min(h, bottom+2))
                digits = image.crop(box).convert('RGB')
                a = np.asarray(digits)
                if kind == 'orange':
                    binary = Image.fromarray((a.min(axis=2) > 185).astype('uint8')*255)
                else:
                    binary = Image.fromarray(((a[:, :, 0] > 170) & (a[:, :, 1] > 160)
                                             & (a[:, :, 2] < 110)).astype('uint8')*255)
                readings = [recognize(self.engine, digits), recognize(self.engine, binary)]
                votes = Counter(int(raw) for raw, score in readings
                                if score >= .90 and re.fullmatch(r'[1-9]\d{0,2}', raw)
                                and int(raw) in tracker.EXP_TABLE)
                if votes and votes.most_common(1)[0][1] >= 2:
                    levels.append((votes.most_common(1)[0][0], normalized(box, image.size),
                                   'classic' if kind == 'orange' else 'new'))
        unique = {(level, style) for level, _, style in levels}
        if len(unique) != 1:
            raise HudNotFound('레벨 표시 확인 중 · LV와 숫자가 보이게 해 주세요')
        return levels[0]

    def _detector(self):
        if self.detector is None:
            from rapidocr import RapidOCR, ModelType
            import tracker
            model = tracker.RESOURCES/'models'/'PP-OCRv6_det_tiny.onnx'
            if not model.is_file():
                raise HudNotFound('자동 탐색 모델을 찾을 수 없어요')
            self.detector = RapidOCR(params={
                'Global.use_det': True, 'Global.use_cls': False, 'Global.use_rec': False,
                'Det.model_type': ModelType.TINY, 'Det.model_path': str(model),
                'Det.limit_type': 'max', 'Det.limit_side_len': 1600,
                'EngineConfig.onnxruntime.intra_op_num_threads': 2,
                'EngineConfig.onnxruntime.inter_op_num_threads': 1})
        return self.detector

    def locate(self, image):
        level, level_roi, ui_kind = self.read_level(image)
        # The classic EXP row is anchored to the bottom of the HUD. Keep
        # changing scenery above it out of text detection when possible.
        bands = (.93, .85) if ui_kind == 'classic' else (.85,)
        for band in bands:
            found = self._find_exp(image, level, level_roi, ui_kind, band)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise HudNotFound('경험치 후보가 여러 곳이에요 · 수동 영역을 지정해 주세요')
        raise HudNotFound('경험치 위치 확인 중 · 하단 표시를 확인해 주세요')

    def _find_exp(self, image, level, level_roi, ui_kind, band):
        import tracker
        from temporal_ocr import consistent_value
        from window_capture import pixel_rect
        w, h = image.size
        start = math.floor(h*band)
        result = self._detector()(np.asarray(image.crop((0, start, w, h)).convert('RGB')))
        candidates = []
        for points in ([] if result.boxes is None else result.boxes):
            if not np.isfinite(points).all():
                continue
            x = max(0, math.floor(float(points[:, 0].min()))-6)
            y = max(start, math.floor(float(points[:, 1].min()))+start-3)
            right = min(w, math.ceil(float(points[:, 0].max()))+6)
            bottom = min(h, math.ceil(float(points[:, 1].max()))+start+3)
            cw, ch = right-x, bottom-y
            if (y >= h*.90 and w*.035 <= cw <= w*.55 and h*.007 <= ch <= h*.065
                    and 3 <= cw/ch <= 35):
                candidates.append((x, y, right, bottom))
        # The detector can split the number and percentage into adjacent boxes.
        # Join only neighboring fragments on the same text row, then verify the
        # resulting complete line with the unchanged strict EXP parser.
        fragments = list(candidates)
        for first in fragments:
            for second in fragments:
                if first[0] >= second[0]:
                    continue
                fh, sh = first[3]-first[1], second[3]-second[1]
                gap = second[0]-first[2]
                if (-min(first[2]-first[0], second[2]-second[0])*.4 <= gap <= max(fh, sh)*.8
                        and abs(first[1]-second[1]) <= max(fh, sh)*.25
                        and min(fh, sh)/max(fh, sh) >= .6):
                    combined = (first[0], min(first[1], second[1]), second[2], max(first[3], second[3]))
                    if combined[2]-combined[0] <= w*.55 and combined not in candidates:
                        candidates.append(combined)
        found = []
        for x, y, right, bottom in sorted(candidates, key=lambda b: (-b[1], -(b[2]-b[0])))[:20]:
            value, score, _, _ = tracker.LineReader(self.engine).read(
                image.crop((x, y, right, bottom)), time.monotonic(), force=True, level=level)
            if value is None or not consistent_value(value, tracker.EXP_TABLE[level], level):
                continue
            roi = None
            for pad in (max(3, round((bottom-y)*.25)), 3, 0):
                candidate = normalized((max(0, x-pad), y, min(w, right+pad), bottom), image.size)
                px, py, pw, ph = pixel_rect(candidate, w, h)
                checked = tracker.LineReader(self.engine).read(image.crop((px, py, px+pw, py+ph)),
                                                               time.monotonic(), force=True, level=level)[0]
                if checked == value:
                    roi = candidate
                    break
            if roi is None or any(overlap(roi, old.exp_roi) >= .65 for old in found):
                continue
            found.append(Hud(level, value, roi, level_roi, image.info.get('window_id'), image.size, ui_kind))
        return found


def exp_image(full, hud):
    from window_capture import pixel_rect
    x, y, w, h = pixel_rect(hud.exp_roi, *full.size)
    image = full.crop((x, y, x+w, y+h))
    image.info.update(full.info, window_region=hud.exp_roi, detected_level=hud.level,
                      level_region=hud.level_roi, auto_detected=True, full_game_image=full,
                      ui_kind=hud.ui_kind, game_image_size=hud.size)
    return image


class AutomaticCapture:
    """Own all discovery state on the worker thread, scoped to its generation."""
    def __init__(self, client, locator, clock=time.monotonic):
        self.client, self.locator, self.clock = client, locator, clock
        self.gate = Confirmation()
        self.generation = None
        self.hud = None
        self.next_check = 0
        self.verified_at = None
        self.content_points = None

    def reset(self):
        self.hud = None
        self.next_check = 0
        self.gate.clear()
        self.verified_at = None
        self.content_points = None

    def read(self, generation, expected_level, baseline_set, include_full):
        if generation != self.generation:
            self.reset()
            self.generation = generation
        now = self.clock()
        if self.hud is None or now >= self.next_check:
            try:
                full = self.client.snapshot()
                hud = self.locator.locate(full)
            except Exception:
                self.reset()
                raise
            if not self.gate.accept(hud, now):
                raise HudNotFound(f'레벨 {hud.level} · 경험치 위치 재확인 {self.gate.count}/3')
            if baseline_set and hud.level != expected_level:
                raise HudLevelChanged(expected_level, hud.level)
            self.hud, self.next_check = hud, now+5
            self.verified_at = now
            self.content_points = full.info.get('content_points')
            image = exp_image(full, hud)
            image.info['auto_confirmed'] = True
            image.info['ui_verified_at'] = self.verified_at
            if not include_full:
                image.info.pop('full_game_image', None)
            return image
        try:
            image = self.client.snapshot(roi=self.hud.exp_roi, include_full=include_full)
        except ValueError:
            self.reset()
            raise HudNotFound('게임 창 크기 변경 · 표시 위치 다시 찾는 중')
        except Exception:
            self.reset()
            raise
        if image.info.get('window_id') != self.hud.window_id:
            self.reset()
            raise HudNotFound('게임 창 변경 · 표시 위치 다시 찾는 중')
        if image.info.get('content_points') != self.content_points:
            self.reset()
            raise HudNotFound('게임 창 크기 변경 · 표시 위치 다시 찾는 중')
        image.info.update(detected_level=self.hud.level, level_region=self.hud.level_roi,
                          auto_detected=True, auto_confirmed=True, ui_kind=self.hud.ui_kind,
                          game_image_size=self.hud.size, ui_verified_at=self.verified_at)
        return image
