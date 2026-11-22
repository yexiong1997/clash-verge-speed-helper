"""In-memory, monotonic, non-overlapping test schedule."""

import math
import threading
import time

INTERVALS = frozenset((15, 30, 60, 120, 360, 720, 1440))


class TestSchedule:
    def __init__(self):
        self._lock = threading.RLock()
        self.minutes = None
        self.deadline = None
        self.last = None
        self.active = None

    def arm(self, minutes, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if minutes not in INTERVALS or not math.isfinite(now):
                self.disarm()
                raise ValueError("invalid schedule")
            deadline = now + minutes * 60
            if not math.isfinite(deadline) or deadline <= now:
                self.disarm()
                raise ValueError("invalid schedule")
            self.minutes, self.deadline, self.last = minutes, deadline, now

    def disarm(self):
        with self._lock:
            self.minutes = self.deadline = self.last = None

    def display(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.minutes is None or not math.isfinite(now):
                return None, None, False
            return self.minutes, max(0, math.ceil(self.deadline - now)), self.active is not None

    def begin_manual(self, external_busy=False):
        with self._lock:
            if self.active is not None or external_busy:
                return None
            self.active = object()
            return self.active

    def poll(self, now=None, external_busy=False):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.minutes is None:
                return "disarmed", None
            if not math.isfinite(now) or not math.isfinite(now + self.minutes * 60):
                self.disarm()
                return "invalid", None
            if now < self.last:
                self.last, self.deadline = now, now + self.minutes * 60
                return "clock_moved", None
            self.last = now
            if now < self.deadline:
                return "not_due", None
            self.deadline = now + self.minutes * 60
            if self.active is not None or external_busy:
                return "skipped_busy", None
            self.active = object()
            return "started", self.active

    def finish(self, token, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.active is not token or token is None:
                return False
            self.active = None
            if self.minutes is not None:
                deadline = now + self.minutes * 60
                if not math.isfinite(now) or not math.isfinite(deadline) or deadline <= now:
                    self.disarm()
                else:
                    self.deadline, self.last = deadline, now
            return True
