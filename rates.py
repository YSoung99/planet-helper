"""Rates use matching EXP/time endpoints; display cadence belongs to the UI."""


def estimate(samples, anchor, now, ended=None, window=180, minimum=60, idle=0.0):
    """`idle` is break time between anchor and the last sample; the cumulative
    average uses hunting time only, the recent window stays in real time."""
    result = dict(cumulative=None, recent=None, duration=0.0, recent_seconds=0.0,
                  asof=None, age=0.0, status='waiting', recent_status='waiting')
    if anchor is None or not samples:
        return result
    start, baseline = anchor
    end, current = samples[-1]
    duration = max(0.0, end-start-idle)
    age = max(0.0, (ended if ended is not None else now)-end)
    result.update(duration=duration, asof=end, age=age)
    if ended is None and age > 6:
        result.update(status='stale', recent_status='stale')
        return result
    if duration < minimum:
        result.update(status='warming', recent_status='warming')
        return result
    result.update(cumulative=max(0, current-baseline)*60/duration, status='ready')
    cutoff = max(start, end-window)
    span = end-cutoff
    result['recent_seconds'] = span
    points = [anchor] + [(t, value) for t, value in samples if t > start]
    # A long internal gap has unknown reward timing. Keep the cumulative total,
    # but withhold the recent-window estimate until that gap leaves the window.
    for (left, _), (right, _) in zip(points, points[1:]):
        if right > cutoff and left < end and right-left > 10:
            result['recent_status'] = 'gap'
            return result
    boundary = None
    for (left, left_exp), (right, right_exp) in zip(points, points[1:]):
        if left <= cutoff <= right:
            boundary = left_exp+(right_exp-left_exp)*(cutoff-left)/(right-left)
            break
    if boundary is None or span < minimum:
        result['recent_status'] = 'warming'
        return result
    result.update(recent=max(0.0, current-boundary)*60/span, recent_status='ready')
    return result
