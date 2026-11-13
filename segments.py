"""Find points inside one measurement where hunting efficiency clearly changed.

Breaks (minutes without EXP) are handled by idle detection, so they are left
out here; this looks only at minutes of active hunting and asks "what changed?"
when the per-minute EXP level shifts and stays shifted.
"""
import json
import math
from pathlib import Path

import tracker

MIN_SIDE = 4          # active minutes needed on each side of a change
MIN_CHANGE = .25      # relative change in mean per-minute EXP
MIN_SCORE = 2.75      # difference / standard error (best-of-many splits, so >2 by chance)


def active_minutes(snapshots):
    """(minute index, EXP) for full hunting minutes, skipping breaks and their edges."""
    values = [s.get('one') for s in snapshots]
    out = []
    for i, value in enumerate(values):
        if not value:
            continue
        neighbours = [values[j] for j in (i-1, i+1) if 0 <= j < len(values)]
        if any(not v for v in neighbours):
            continue  # a minute next to a break is partly idle
        out.append((snapshots[i]['index'], value))
    return out


def _best_split(points):
    best = None
    for k in range(MIN_SIDE, len(points)-MIN_SIDE+1):
        left = [v for _, v in points[:k]]
        right = [v for _, v in points[k:]]
        ml, mr = sum(left)/len(left), sum(right)/len(right)
        var = (sum((v-ml)**2 for v in left)+sum((v-mr)**2 for v in right))/max(1, len(points)-2)
        error = math.sqrt(var*(1/len(left)+1/len(right))) or 1e-9
        score = abs(ml-mr)/error
        if best is None or score > best[0]:
            best = (score, k, ml, mr)
    return best


def find_changes(snapshots, limit=3):
    """Return changes as dicts: minute (first minute of the new level), before, after."""
    points = active_minutes(snapshots)
    changes = []

    def split(part):
        if len(changes) >= limit or len(part) < 2*MIN_SIDE:
            return
        best = _best_split(part)
        if best is None:
            return
        score, k, before, after = best
        if score < MIN_SCORE or abs(after-before) < MIN_CHANGE*max(before, after):
            return
        changes.append(dict(minute=part[k][0], before=before, after=after))
        split(part[:k]); split(part[k:])

    split(points)
    return sorted(changes, key=lambda c: c['minute'])


# ---- tags for detected changes: records/segment-tags.json {session_id: {minute: tag}} ----

def tag_path(base=None):
    return Path(base or tracker.BASE)/'records'/'segment-tags.json'


def load_tags(base=None):
    try:
        data = json.loads(tag_path(base).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tag(session_id, minute, text, base=None):
    """Store a tag; an empty text records that the question was dismissed."""
    tags = load_tags(base)
    tags.setdefault(session_id, {})[str(minute)] = ' '.join(str(text).split())[:60]
    path = tag_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(tags, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
