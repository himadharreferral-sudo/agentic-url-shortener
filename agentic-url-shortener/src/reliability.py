import time
import threading
from collections import deque
from typing import Deque, Dict


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int = 120, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        with self._lock:
            q = self._buckets.setdefault(key, deque())
            cutoff = current - self.window_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(current)
            return True


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 10.0):
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.failures = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allow_request(self, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        with self._lock:
            if self.state == "CLOSED":
                return True
            if self.state == "OPEN":
                if self.opened_at is not None and (current - self.opened_at) >= self.recovery_seconds:
                    self.state = "HALF_OPEN"
                    return True
                return False
            return True  # HALF_OPEN allows probe

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.state = "CLOSED"
            self.opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.state = "OPEN"
                self.opened_at = now if now is not None else time.time()
