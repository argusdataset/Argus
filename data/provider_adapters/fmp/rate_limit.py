"""Token-bucket rate limiting for FMP requests.

Two separate budgets, because FMP enforces two very different limits:
standard endpoints run at the plan's per-minute allowance (300 on
Starter, 750 on Premium, 3000 on Ultimate), while bulk CSV downloads are
throttled to roughly one per ten seconds regardless of plan. Sharing one
bucket between them would either waste the standard budget or blow
through the bulk one.

The bucket refills continuously rather than in per-minute steps, so a
600/min budget means one slot every 100ms rather than 600 requests at the
top of the minute followed by a stall.
"""

from __future__ import annotations

import asyncio
import time

from data.provider_adapters.fmp.endpoints import EndpointTier


class TokenBucket:
    """An asyncio-safe token bucket.

    Capacity equals the per-minute rate, so a burst up to a full minute's
    allowance is permitted after an idle period, then throughput settles
    to the sustained rate.
    """

    def __init__(self, rate_per_minute: int, *, time_source=time.monotonic) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._capacity = float(rate_per_minute)
        self._tokens = float(rate_per_minute)
        self._refill_per_second = rate_per_minute / 60.0
        self._time = time_source
        self._updated_at = time_source()
        self._lock = asyncio.Lock()

    @property
    def available_tokens(self) -> float:
        return self._tokens

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._updated_at
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
            self._updated_at = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_seconds = deficit / self._refill_per_second
            await asyncio.sleep(wait_seconds)


class RateLimiter:
    """Routes each request to the bucket for its endpoint tier."""

    def __init__(
        self,
        requests_per_minute: int,
        bulk_requests_per_minute: int,
        *,
        time_source=time.monotonic,
    ) -> None:
        self._buckets = {
            EndpointTier.STANDARD: TokenBucket(requests_per_minute, time_source=time_source),
            EndpointTier.BULK: TokenBucket(bulk_requests_per_minute, time_source=time_source),
        }

    async def acquire(self, tier: EndpointTier) -> None:
        await self._buckets[tier].acquire()

    def bucket(self, tier: EndpointTier) -> TokenBucket:
        return self._buckets[tier]


def backoff_delay(
    attempt: int,
    *,
    base_seconds: float,
    max_seconds: float,
    retry_after_seconds: float | None = None,
) -> float:
    """Delay before retry `attempt` (1-based).

    A server-supplied Retry-After always wins — it is better information
    than any local guess. Otherwise the delay doubles per attempt, capped.

    No random jitter: this adapter is a single coordinated job rather
    than a fleet of independent clients, so there is no thundering herd
    to spread out, and a deterministic schedule is easier to test and to
    reason about when reading a stalled job's logs.
    """
    if retry_after_seconds is not None:
        return min(retry_after_seconds, max_seconds)
    return min(base_seconds * (2 ** (attempt - 1)), max_seconds)
