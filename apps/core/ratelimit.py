"""Small fixed-window rate limiter backed by the Django cache.

Used for login, registration, recovery and invite endpoints (SDD: rate-limited).
Production should use a shared cache (database or Redis) so limits apply across
worker processes.
"""
import hashlib
import time

from django.conf import settings
from django.core.cache import cache

from .errors import Throttled


def client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "unknown")


def check_rate(bucket: str, identity: str) -> None:
    limit, window = settings.RATE_LIMITS[bucket]
    slot = int(time.time() // window)
    digest = hashlib.sha256(f"{bucket}:{identity}".encode()).hexdigest()[:32]
    key = f"rl:{bucket}:{digest}:{slot}"
    added = cache.add(key, 1, timeout=window + 5)
    count = 1 if added else cache.incr(key)
    if count > limit:
        raise Throttled("Too many attempts. Please wait a few minutes and try again.")
