"""Volatile, continuous EXP observations collected between measurements."""
from collections import deque


class RecentExp:
    WINDOW = 300.0
    MAX_GAP = 10.0
    FRESHNESS = 6.0

    def __init__(self):
        self.clear()

    def clear(self):
        self.source = None
        self.points = deque()
        self.readable = False

    def observe(self, source, stamp, value):
        if self.points:
            previous_stamp, previous_value = self.points[-1]
            if stamp <= previous_stamp:
                return
            if (source != self.source or round(stamp-previous_stamp, 3) > self.MAX_GAP
                    or value[0] < previous_value[0] or value[1] < previous_value[1]):
                self.clear()
        self.source = source
        self.points.append((stamp, value))
        self.readable = True
        self.prune(stamp)

    def prune(self, now):
        while self.points and self.points[0][0] < now-self.WINDOW:
            self.points.popleft()

    def available(self, now):
        self.prune(now)
        return (self.readable and len(self.points) >= 2
                and 0 <= now-self.points[-1][0] <= self.FRESHNESS
                and self.points[-1][1][0] > self.points[0][1][0])

    def increase_stamps(self):
        """Capture times at which the EXP value rose above the previous point."""
        points = list(self.points)
        return [stamp for (_, before), (stamp, after) in zip(points, points[1:])
                if after[0] > before[0]]
