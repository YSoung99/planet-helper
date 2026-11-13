"""Frozen captures corroborate a change before it reaches the ledger.

Repeated pixels may share an OCR error; hashes expose that limitation.
"""
import hashlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
import numpy as np


def usable_image(image):
    if image.width < 40 or image.height < 8:
        return False
    pixels = np.asarray(image.convert('RGB'))
    # Measure spatial contrast within each channel, not differences between RGB.
    return int(np.ptp(pixels, axis=(0, 1)).max()) >= 20


def consistent_value(value, total, level=None):
    exp, pct = value
    tolerance = .02000001 if level is not None else .04000001
    return total is None or (exp < total and abs(exp/total*100-pct) <= tolerance)


def valid(frame, total, level=None):
    return (frame['value'] is not None and frame['score'] >= .85
            and consistent_value(frame['value'], total, level))


def pixel_hash(image):
    prefix = f'{image.mode}:{image.size}:'.encode('ascii')
    return hashlib.sha256(prefix + image.tobytes()).hexdigest()


def decide(frames, total=None, level=None):
    """Return a source frame index, never an average or manufactured EXP value."""
    good = [i for i, frame in enumerate(frames) if valid(frame, total, level)]
    if len(good) < 2:
        return None, 'insufficient'
    if len(good) == 3:
        values = [frames[i]['value'] for i in good]
        if all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(values, values[1:])):
            return good[1], 'progression_middle'
    for right in reversed(good):
        if any(frames[left]['value'] == frames[right]['value'] for left in good if left < right):
            return right, 'stable'
    return None, 'inconsistent'


def verify(first, capture, total, previous, cancelled, wait, level=None):
    if cancelled():
        return None, {'checked': False, 'reason': 'cancelled'}, [first]
    if valid(first, total, level) and previous is not None and first['value'] == previous:
        return first, {'checked': False, 'reason': 'unchanged'}, []
    frames = [first]
    for _ in range(2):
        if wait(.15) or cancelled():
            return None, {'checked': True, 'reason': 'cancelled'}, frames
        frames.append(capture(True))
    if cancelled():
        return None, {'checked': True, 'reason': 'cancelled'}, frames
    index, reason = decide(frames, total, level)
    evidence = {'checked': True, 'reason': reason, 'selected': index,
                'frames': [{'capture_time': f['observed_at'], 'value': f['value'],
                            'score': float(f['score']), 'raw': f['raw'][:180],
                            'pixel_hash': pixel_hash(f['image']) if 'image' in f else None,
                            'rejection_reason': f.get('rejection_reason')} for f in frames]}
    return (frames[index] if index is not None else None), evidence, frames


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _prune(folder, limit):
    groups = sorted(p for p in folder.iterdir() if p.is_dir() and p.name.isdecimal())
    for old in groups[:max(0, len(groups)-limit)]:
        shutil.rmtree(old)


def _clean_legacy(folder, limit):
    # Only touch numeric filenames created by the previous diagnostic writer.
    manifests = sorted(p for p in folder.glob('*.json') if p.stem.isdecimal())
    keep = {p.stem for p in manifests[-limit:]}
    for path in folder.iterdir():
        match = re.fullmatch(r'(\d+)(?:-\d+\.png|\.json)', path.name)
        if path.is_file() and match and match[1] not in keep:
            path.unlink()


def save_rejected(folder, frames, evidence, limit=30, *, category=None):
    """Commit complete image groups atomically; retain obstructions separately."""
    if limit < 1:
        raise ValueError('limit must be positive')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _clean_legacy(folder, 30)
    category = category or ('unobservable' if frames and all(
        f.get('rejection_reason') in ('blank', 'covered') for f in frames) else 'mismatch')
    if category not in ('unobservable', 'mismatch', 'conflict'):
        raise ValueError('unknown evidence category')
    destination = folder/category
    destination.mkdir(exist_ok=True)
    for abandoned in destination.glob('.pending-*'):
        if abandoned.is_dir():
            shutil.rmtree(abandoned)
    signature = hashlib.sha256(''.join(pixel_hash(f['image']) for f in frames).encode('ascii')).hexdigest()
    cap = min(limit, 3) if category == 'unobservable' else limit
    if category == 'unobservable':
        for group in sorted(destination.iterdir(), reverse=True):
            if not group.is_dir() or not group.name.isdecimal():
                continue
            manifest = group/'evidence.json'
            try:
                previous = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(previous, dict):
                continue
            if previous.get('signature') == signature:
                previous.update(repetitions=previous.get('repetitions', 1)+1, last_seen=time.time())
                staged = group/'.evidence.pending'
                try:
                    _write_json(staged, previous)
                    staged.replace(manifest)
                finally:
                    staged.unlink(missing_ok=True)
                _prune(destination, cap)
                return
    staged = Path(tempfile.mkdtemp(prefix='.pending-', dir=destination))
    try:
        for index, frame in enumerate(frames):
            frame['image'].save(staged/f'{index}.png')
        payload = dict(evidence, category=category, signature=signature,
                       repetitions=1, last_seen=time.time())
        _write_json(staged/'evidence.json', payload)
        staged.rename(destination/str(time.time_ns()))
    finally:
        if staged.exists():
            shutil.rmtree(staged)
    _prune(destination, cap)


def remember_change(frames, evidence, session_id, generation, accepted_value):
    """Retain one small accepted-change group, without full-window metadata."""
    retained = []
    for frame in frames:
        image = frame['image'].copy()
        image.info.clear()
        retained.append(dict(frame, image=image))
    return dict(frames=retained, evidence=evidence, session_id=session_id, generation=generation,
                accepted_value=accepted_value)


def save_conflict(folder, previous, frames, evidence, ledger, session_id, generation):
    if previous and (previous['session_id'] != session_id or previous['generation'] != generation
                     or previous.get('accepted_value') != ledger or not previous['frames']):
        previous = None
    previous_frames = previous['frames'] if previous else []
    details = dict(reason='lower_than_ledger', ledger=ledger,
                   previous_change=previous['evidence'] if previous else None,
                   previous_change_missing=previous is None,
                   current=evidence, previous_frame_count=len(previous_frames),
                   current_frame_count=len(frames))
    save_rejected(folder, previous_frames+frames, details, limit=10, category='conflict')
