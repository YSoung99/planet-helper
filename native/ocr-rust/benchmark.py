"""Replay manually labelled, pixel-deduplicated EXP crops."""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from PIL import Image
import numpy as np

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=Path)
    parser.add_argument('labels', type=Path)
    parser.add_argument('--captures', type=Path)
    parser.add_argument('--mode', choices=['python', 'rust'], required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app.resolve()))
    import tracker
    from temporal_ocr import usable_image, consistent_value
    engine = tracker.create_ocr_engine(prefer_native=args.mode == 'rust')
    print(type(engine).__name__, flush=True)
    reader = tracker.LineReader(engine)
    source = json.loads(args.labels.read_text())
    captures = args.captures or args.app
    rows = []
    warmup = Image.open(captures / source[0]['image']).convert('RGB')
    reader.read(warmup, time.monotonic(), force=True, level=source[0]['level'])
    for case in source:
        image = Image.open(captures / case['image']).convert('RGB')
        pixels = np.asarray(image)
        r, g, b = (pixels[:, :, index] for index in range(3))
        covered = bool(((r > 130) & (g > 60) & (r > g * 1.1) & (b < 80)).mean() > .02)
        start = time.perf_counter()
        if covered or not usable_image(image):
            value, score, raw = None, 0, ''
        else:
            value, score, raw, _ = reader.read(image, time.monotonic(), force=True, level=case['level'])
        elapsed_ms = (time.perf_counter() - start) * 1000
        if value is not None and not consistent_value(value, tracker.EXP_TABLE.get(case['level']), case['level']):
            value = None
        row = {**case, 'value': value, 'raw': raw, 'score': score, 'ms': elapsed_ms, 'blocked': covered or not usable_image(image)}
        rows.append(row)
    usable = [r for r in rows if not r['blocked']]
    positives = [r for r in rows if r['expected'] is not None]
    negatives = [r for r in rows if r['expected'] is None]
    times = [r['ms'] for r in usable]
    print(json.dumps(dict(
        frames=len(rows), correct=sum(r['value'] is not None and list(r['value']) == r['expected'] for r in positives),
        readable=len(positives), wrong=sum(r['value'] is not None and list(r['value']) != r['expected'] for r in rows),
        negative=len(negatives), median_ms=statistics.median(times),
        p95_ms=statistics.quantiles(times, n=20, method='inclusive')[18],
        rows=rows), ensure_ascii=False))

if __name__ == '__main__':
    main()
