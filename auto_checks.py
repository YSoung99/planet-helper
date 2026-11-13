"""Offline, packaged model checks against retained real game captures."""
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw
import tracker
from auto_hud import HudLocator, HudNotFound, Confirmation, exp_image


def run(report_path):
    locator = HudLocator(tracker.create_ocr_engine())
    results = []
    fixtures = [('auto-new.png', 'classic', 170, (34514347, 8.06)),
                ('auto-old.png', 'new', 170, (10730498, 2.51)),
                ('auto-current.png', 'classic', 170, (198885906, 46.46)),
                ('auto-live-172.png', 'new', 172, (283993078, 59.63))]
    for name, expected_ui, expected_level, expected in fixtures:
        original = Image.open(tracker.RESOURCES/'verification'/name).convert('RGB')
        for factor in (1, .8, 1.2):
            image = original if factor == 1 else original.resize(
                (round(original.width*factor), round(original.height*factor)), Image.Resampling.LANCZOS)
            image.info['window_id'] = 123
            began = time.monotonic()
            print(name, factor, flush=True)
            hud = locator.locate(image)
            assert hud.ui_kind == expected_ui, (name, factor, hud.ui_kind)
            assert hud.level == expected_level, (name, factor, hud)
            assert hud.value == expected, (name, factor, hud)
            gate = Confirmation()
            assert not gate.accept(hud, 1)
            assert not gate.accept(hud, 2)
            assert gate.accept(hud, 3)
            roi = exp_image(image, hud)
            assert roi.info['detected_level'] == expected_level
            assert tracker.LineReader(locator.engine).read(roi, 4, force=True)[0] == expected
            results.append(dict(fixture=name, scale=factor, level=hud.level, value=hud.value,
                                exp_roi=hud.exp_roi, ui_kind=hud.ui_kind, seconds=time.monotonic()-began))
        if name == 'auto-current.png':
            masked = original.copy()
            ImageDraw.Draw(masked).rectangle((0, int(masked.height*.9), int(masked.width*.3), masked.height), fill='black')
            try:
                locator.locate(masked)
            except HudNotFound:
                results.append(dict(fixture='hidden-level', withheld=True))
            else:
                raise AssertionError('Missing level must not use EXP to guess it')
    try:
        locator.locate(Image.new('RGB', (1280, 720)))
    except HudNotFound:
        results.append(dict(fixture='blank-window', withheld=True))
    else:
        raise AssertionError('Blank window accepted')
    report = dict(version=tracker.APP_VERSION, frozen=bool(getattr(sys, 'frozen', False)),
                  passed=True, cases=results, data_directory=str(tracker.BASE))
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
