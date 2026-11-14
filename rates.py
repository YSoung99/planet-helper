"""Rates use matching EXP/time endpoints; display cadence belongs to the UI."""

from itertools import chain, pairwise, takewhile


def estimate(samples, anchor, now, ended=None, window=180, minimum=60, idle=0.0,
             gap_seconds=0.0, gap_gain=0, last_gap=None):
    """`idle` is break time between anchor and the last sample; the cumulative
    average uses observed hunting time and its corresponding EXP only.
    The recent window stays in real time and is withheld if it crosses a gap.
    """
    result = dict(cumulative=None, recent=None, duration=0.0, recent_seconds=0.0,
                  asof=None, age=0.0, status='waiting', recent_status='waiting',
                  rate_gain=0, gap_seconds=gap_seconds, gap_gain=gap_gain)
    if anchor is None or not samples:
        return result
    start, baseline = anchor
    end, current = samples[-1]
    duration = max(0.0, round(end-start-idle-gap_seconds, 3))
    age = max(0.0, (ended if ended is not None else now)-end)
    gain = max(0, current-baseline-gap_gain)
    result.update(duration=duration, asof=end, age=age, rate_gain=gain)
    if ended is None and age > 6:
        result.update(status='stale', recent_status='stale')
        return result
    if duration <= 0 or duration < minimum:
        result.update(status='warming', recent_status='warming')
        return result
    result.update(cumulative=gain*60/duration, status='ready')
    cutoff = max(start, end-window)
    span = round(end-cutoff, 3)
    result['recent_seconds'] = span
    if last_gap and last_gap[0] < end and cutoff < last_gap[1]:
        result['recent_status'] = 'gap'
        return result
    # Walk backward only as far as the cutoff. The anchor precedes retained samples.
    points = chain(takewhile(lambda point: point[0] > start, reversed(samples)), (anchor,))
    boundary = None
    for (right, right_exp), (left, left_exp) in pairwise(points):
        if right > cutoff and left < end and round(right-left, 3) > 10:
            result['recent_status'] = 'gap'
            return result
        if left <= cutoff <= right:
            boundary = left_exp+(right_exp-left_exp)*(cutoff-left)/(right-left)
            break
    if boundary is None or span < minimum:
        result['recent_status'] = 'warming'
        return result
    result.update(recent=max(0.0, current-boundary)*60/span, recent_status='ready')
    return result
