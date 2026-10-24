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

    async def wait(self, step: bool = False) -> None:
        """Call before every request (page load / postback) to the site.

        A new polling round (step=False) waits the full poll interval since the
        previous request; follow-up postbacks inside the same round
        (step=True) only wait the short STEP_SECONDS gap.
        """
        async with self._lock:
            self._check_window(self._hits, self.per_hour)
            base = self.step_interval if step else self.min_interval
            gap = base * (1 + random.uniform(0, self.jitter))
            delay = self._last + gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()
            self._hits.append(self._last)

    def login_attempt(self) -> None:
        self._check_window(self._logins, self.logins_per_hour)
        self._logins.append(time.monotonic())
