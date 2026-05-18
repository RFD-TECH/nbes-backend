"""apps/users/throttle.py — IP-level brute-force defence.

SRS §1.2.6:
- 100 failed logins from a single IP in 60 s -> 15-minute throttle
- 1000 failed logins from a single IP in 24 h -> 24-hour block

Uses Django's cache (Redis-backed). Cache keys:
  ipthrottle:fails:1m:<ip>     -> rolling 1-minute fail count
  ipthrottle:fails:1d:<ip>     -> rolling 24-hour fail count
  ipthrottle:blocked:short:<ip> -> presence means 15-min throttle active
  ipthrottle:blocked:long:<ip>  -> presence means 24-hour block active
"""
from django.conf import settings
from django.core.cache import cache


_PREFIX = "ipthrottle:"


def _k(name: str, ip: str) -> str:
    return f"{_PREFIX}{name}:{ip}"


def is_blocked(ip: str) -> bool:
    """True if either short throttle or long block is active for this IP."""
    return bool(cache.get(_k("blocked:short", ip)) or cache.get(_k("blocked:long", ip)))


def record_failure(ip: str) -> None:
    """Increment 1-minute and 24-hour counters; engage throttle/block at thresholds."""
    one_min_key = _k("fails:1m", ip)
    one_day_key = _k("fails:1d", ip)

    one_min_count = cache.get(one_min_key, 0) + 1
    cache.set(one_min_key, one_min_count, timeout=60)

    one_day_count = cache.get(one_day_key, 0) + 1
    cache.set(one_day_key, one_day_count, timeout=24 * 60 * 60)

    if one_min_count >= settings.IP_THROTTLE_FAILS_PER_MINUTE:
        cache.set(
            _k("blocked:short", ip),
            True,
            timeout=settings.IP_THROTTLE_MINUTES * 60,
        )

    if one_day_count >= settings.IP_THROTTLE_FAILS_PER_DAY:
        cache.set(
            _k("blocked:long", ip),
            True,
            timeout=settings.IP_BLOCK_HOURS * 60 * 60,
        )


def record_success(ip: str) -> None:
    """Successful auth clears the rolling 1-minute counter for the IP.

    We deliberately keep the 24-hour counter — credential stuffing campaigns
    often include a few successful guesses; the long counter should still trip.
    """
    cache.delete(_k("fails:1m", ip))
