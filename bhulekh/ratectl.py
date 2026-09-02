"""Adaptive concurrency: ramp tabs up while the portal is healthy, halve on error bursts."""
from __future__ import annotations

import time
from collections import deque


class RateController:
    def __init__(self, start: int, max_tabs: int, min_tabs: int, ramp_every_s: float, backoff_factor: float):
        self.target = max(min_tabs, min(start, max_tabs))
        self.max = max_tabs
        self.min = min_tabs
        self.ramp_every_s = ramp_every_s
        self.backoff = backoff_factor
        self.last_change = time.time()
        self.errors: deque = deque()
        self.successes = 0
        self.total_errors = 0

    def _recent_errors(self, window_s: float = 30.0) -> int:
        now = time.time()
        while self.errors and now - self.errors[0] > window_s:
            self.errors.popleft()
        return len(self.errors)

    def record_success(self):
        self.successes += 1
        now = time.time()
        if (self.target < self.max and now - self.last_change >= self.ramp_every_s
                and self._recent_errors() == 0):
            self.target = min(self.max, self.target + 2)
            self.last_change = now

    def record_error(self):
        self.total_errors += 1
        self.errors.append(time.time())
        if self._recent_errors() >= 3:
            new = max(self.min, int(self.target * self.backoff))
            if new < self.target:
                self.target = new
                self.last_change = time.time()
            self.errors.clear()

    def allows(self, active: int) -> bool:
        return active < self.target
