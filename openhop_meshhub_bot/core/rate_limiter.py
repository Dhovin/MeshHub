import time
import asyncio
import threading
import logging
from collections import OrderedDict

logger = logging.getLogger("RateLimiter")

class BotTxRateLimiter:
    """
    Radio transmission rate limiter to prevent LoRa channel overload and
    adhere to frequency duty-cycle limits.
    """
    def __init__(self, seconds: float = 1.0):
        self.seconds = max(0.0, float(seconds))
        self.last_tx = 0.0
        self._total_tx = 0
        self._total_throttled = 0
        self._lock = asyncio.Lock()

    def can_tx(self) -> bool:
        if self.seconds <= 0.0:
            return True
        return (time.monotonic() - self.last_tx) >= self.seconds

    def time_until_next_tx(self) -> float:
        if self.seconds <= 0.0:
            return 0.0
        elapsed = time.monotonic() - self.last_tx
        return max(0.0, self.seconds - elapsed)

    def record_tx(self):
        self.last_tx = time.monotonic()
        self._total_tx += 1

    async def wait_for_tx(self):
        """
        Async wait until transmission is permitted by the airtime limiter.
        Thread-safe under asyncio.
        """
        if self.seconds <= 0.0:
            self.record_tx()
            return

        async with self._lock:
            wait_time = self.time_until_next_tx()
            if wait_time > 0:
                self._total_throttled += 1
                logger.debug(f"[RateLimiter] Throttling transmission: waiting {wait_time:.2f}s for airtime clearance")
                await asyncio.sleep(wait_time)
            self.record_tx()

    def get_stats(self) -> dict:
        total = self._total_tx + self._total_throttled
        rate = (self._total_throttled / total) if total > 0 else 0.0
        return {
            "total_tx": self._total_tx,
            "total_throttled": self._total_throttled,
            "throttle_rate": rate,
            "limit_seconds": self.seconds
        }


class PerUserRateLimiter:
    """
    Per-user rate limiter to prevent spam or bot reply loops.
    Keyed by public key or sender nickname.
    Uses an OrderedDict LRU cache bounded to max_entries.
    """
    def __init__(self, seconds: float = 30.0, max_entries: int = 1000):
        self.seconds = max(0.0, float(seconds))
        self.max_entries = max_entries
        self._last_send: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _normalize_key(self, key: str) -> str:
        return str(key).strip().lower() if key else ""

    def can_send(self, key: str) -> bool:
        if self.seconds <= 0.0:
            return True
        norm = self._normalize_key(key)
        if not norm:
            return True
        with self._lock:
            last = self._last_send.get(norm, 0.0)
            return (time.monotonic() - last) >= self.seconds

    def time_until_next(self, key: str) -> float:
        if self.seconds <= 0.0:
            return 0.0
        norm = self._normalize_key(key)
        if not norm:
            return 0.0
        with self._lock:
            last = self._last_send.get(norm, 0.0)
            elapsed = time.monotonic() - last
            return max(0.0, self.seconds - elapsed)

    def record_send(self, key: str):
        norm = self._normalize_key(key)
        if not norm:
            return
        with self._lock:
            if norm in self._last_send:
                self._last_send.move_to_end(norm)
            elif len(self._last_send) >= self.max_entries:
                self._last_send.popitem(last=False)
            self._last_send[norm] = time.monotonic()

    def reset(self):
        with self._lock:
            self._last_send.clear()


class ChannelRateLimiter:
    """
    Per-channel rate limiter to enforce airtime spacing on individual channels.
    """
    def __init__(self, channel_limits: dict[str, float] = None, default_seconds: float = 0.0):
        self.default_seconds = max(0.0, float(default_seconds))
        self.channel_limits = {}
        if channel_limits:
            for ch, sec in channel_limits.items():
                try:
                    self.channel_limits[self._normalize_channel(ch)] = max(0.0, float(sec))
                except (ValueError, TypeError):
                    pass
        self._last_send: dict[str, float] = {}
        self._lock = threading.Lock()

    def _normalize_channel(self, channel) -> str:
        return str(channel).strip().lower().lstrip("#")

    def _get_limit(self, channel) -> float:
        norm = self._normalize_channel(channel)
        return self.channel_limits.get(norm, self.default_seconds)

    def can_send(self, channel) -> bool:
        limit = self._get_limit(channel)
        if limit <= 0.0:
            return True
        norm = self._normalize_channel(channel)
        with self._lock:
            last = self._last_send.get(norm, 0.0)
            return (time.monotonic() - last) >= limit

    def time_until_next(self, channel) -> float:
        limit = self._get_limit(channel)
        if limit <= 0.0:
            return 0.0
        norm = self._normalize_channel(channel)
        with self._lock:
            last = self._last_send.get(norm, 0.0)
            elapsed = time.monotonic() - last
            return max(0.0, limit - elapsed)

    def record_send(self, channel):
        norm = self._normalize_channel(channel)
        with self._lock:
            self._last_send[norm] = time.monotonic()
