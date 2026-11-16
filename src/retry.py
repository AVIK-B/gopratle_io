"""
retry.py — small retry-with-backoff wrapper for any live API call.

Both ChatGPT docs correctly flag "no retry logic, failures silently break
the pipeline" as a baseline flaw, then don't actually implement retries
anywhere in their code snippets. This is the real version: a plain
decorator, no new dependency, used on every LIVE_* API call site
(src/enrichment.py's paid-API stage, src/outreach.py's real send).

Deliberately NOT applied to the mock/demo-mode paths — there's nothing
to retry when nothing is actually a network call.
"""
from __future__ import annotations

import time
from functools import wraps
from typing import Callable, TypeVar

T = TypeVar("T")


def retry_with_backoff(max_attempts: int = 3, base_delay: float = 0.5, exceptions=(Exception,)):
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    time.sleep(base_delay * (2 ** (attempt - 1)))  # 0.5s, 1s, 2s...
            raise last_exc
        return wrapped
    return decorator
