"""Per-account throttling so the bot never hammers the university site."""
import asyncio
import random
import time
from collections import deque

from . import config


class RateLimited(Exception):
    """Raised when an hourly cap is reached; the caller should back off."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry in {retry_after:.0f}s")
        self.retry_after = retry_after


class Throttle:
    def __init__(
        self,
        min_interval: float = config.POLL_SECONDS,
        step_interval: float = config.STEP_SECONDS,
        jitter: float = config.POLL_JITTER,
        per_hour: int = config.MAX_REQUESTS_PER_HOUR,
        logins_per_hour: int = config.MAX_LOGINS_PER_HOUR,
    ):
        self.min_interval = max(config.MIN_POLL_FLOOR, min_interval)
        self.step_interval = max(config.MIN_STEP_FLOOR, step_interval)
        self.jitter = jitter
        self.per_hour = per_hour
        self.logins_per_hour = logins_per_hour
        self._last = -1e9
        self._hits: deque[float] = deque()
        self._logins: deque[float] = deque()
        self._lock = asyncio.Lock()

    @staticmethod
    def _check_window(hits: deque[float], cap: int) -> None:
        now = time.monotonic()
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if len(hits) >= cap:
            raise RateLimited(3600 - (now - hits[0]) + 1)

    def reset(self) -> None:
        """Clear all sliding window history so rate limits are immediately zeroed."""
        self._hits.clear()
        self._logins.clear()
        self._last = -1e9

    def stats(self) -> dict:
        """Return current window statistics (requests and logins in the past 60 minutes)."""
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 3600:
            self._hits.popleft()
        while self._logins and now - self._logins[0] > 3600:
            self._logins.popleft()
        return {
            "hits": len(self._hits),
            "max_hits": self.per_hour,
            "logins": len(self._logins),
            "max_logins": self.logins_per_hour,
            "is_limited": len(self._hits) >= self.per_hour or len(self._logins) >= self.logins_per_hour,
        }

    def lane(self) -> "Throttle":
        """A throttle with its own spacing that shares this one's hourly caps.

        Used for on-demand runs (login test, menu fetch) so they don't queue
        behind a job's polling interval, while still counting toward the caps.
        """
        other = Throttle(self.min_interval, self.step_interval, self.jitter, self.per_hour, self.logins_per_hour)
        other._hits, other._logins = self._hits, self._logins
        return other

    async def wait(self, step: bool = False, interval: float | None = None) -> None:
        """Call before every request (page load / postback) to the site.

        A new polling round (step=False) waits the full poll interval since the
        previous request (`interval` overrides it per job, never below
        MIN_JOB_INTERVAL); follow-up postbacks inside the same round
        (step=True) only wait the short STEP_SECONDS gap.
        """
        async with self._lock:
            self._check_window(self._hits, self.per_hour)
            if step:
                base = self.step_interval
            elif interval:
                base = max(config.MIN_JOB_INTERVAL, interval)
            else:
                base = self.min_interval
            gap = base * (1 + random.uniform(0, self.jitter))
            delay = self._last + gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
            self._hits.append(self._last)

    def login_attempt(self) -> None:
        self._check_window(self._logins, self.logins_per_hour)
        self._logins.append(time.monotonic())
