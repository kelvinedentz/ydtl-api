from typing import Any

import redis

from app.config import get_settings

WINDOW_SECONDS = 60


class RateLimited(Exception):
    """Raised when a client is over the limit. The API turns it into a 429."""

    def __init__(self, retry_after: int):
        super().__init__("Rate limit exceeded")
        self.retry_after = retry_after


def check_rate_limit(r: redis.Redis, client_id: str, scope: str = "jobs") -> None:
    """Count one request for this client. Raises RateLimited if over the limit.

    Fixed window: the first request starts a 60 s window and every later
    request in it increments the same counter.
    """
    key = f"rl:{scope}:{client_id}"

    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, WINDOW_SECONDS, nx=True)  # only sets a TTL if none exists
    pipe.ttl(key)
    results: list[Any] = pipe.execute()

    count: int = results[0]
    ttl: int = results[2]

    if count > get_settings().rate_limit_per_minute:
        # ttl is -1 or -2 only in odd edge cases; fall back to the full window.
        raise RateLimited(retry_after=ttl if ttl > 0 else WINDOW_SECONDS)
