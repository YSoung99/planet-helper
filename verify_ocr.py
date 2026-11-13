"""Read a saved Maple Planet game frame or run the bundled EXP-line checks."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "current"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--image", type=Path, help="Full game window image, without desktop margins")
    args = parser.parse_args()

    from PIL import Image
    import tracker
    from auto_hud import HudLocator, HudNotFound

    models = tracker.RESOURCES / "models"
    for name in ("PP-OCRv6_rec_medium.onnx", "PP-OCRv6_det_tiny.onnx"):
        if not (models / name).is_file():
            parser.error(f"Missing bundled model: {name}")

    engine = tracker.create_ocr_engine()
    locator = HudLocator(engine)
    if args.image:
        try:
            with Image.open(args.image) as frame:
                hud = locator.locate(frame)
        except (HudNotFound, OSError) as error:
            print(json.dumps({"recognized": False, "error": str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "recognized": True, "ui_kind": hud.ui_kind, "level": hud.level,
            "exp": hud.value[0], "percent": hud.value[1], "size": hud.size,
            "exp_roi": hud.exp_roi, "level_roi": hud.level_roi,
        }, ensure_ascii=False, indent=2))
        return 0

    import numpy as np

    # Load and exercise the local detector without depending on a game window.
    locator._detector()(np.zeros((160, 480, 3), dtype=np.uint8))
    cases = (
        ("0.4.4_인식복구.png", (395571663, 97.48)),
        ("new-ui-exp.png", (34514347, 8.06)),
        ("new-ui-label-dot.png", (40148011, 9.38)),
    )
    results = []
    for name, expected in cases:
        with Image.open(tracker.RESOURCES / "verification" / name) as image:
            value, _, _, _ = tracker.LineReader(engine).read(
                image.convert("RGB"), time.monotonic(), force=True)
        results.append({"image": name, "expected": expected, "value": value, "passed": value == expected})

    try:
        locator.locate(Image.new("RGB", (1280, 720)))
    except HudNotFound:
        results.append({"image": "blank frame", "passed": True})
    else:
        results.append({"image": "blank frame", "passed": False})

    passed = all(case["passed"] for case in results)
    print(json.dumps({"version": tracker.APP_VERSION, "passed": passed, "cases": results},
                     ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
