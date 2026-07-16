"""Exponential backoff helper for the carry runner main loop.

When the exchange is unreachable, polling at the normal ``--interval`` spams
hundreds of failed requests per hour and risks rate-limiting.  This helper
keeps the normal cadence for the first couple of transient errors, then
backs off exponentially (capped) on sustained failures.
"""
from __future__ import annotations


def backoff_seconds(
    consecutive_errors: int, base: float, cap: float = 300.0, grace: int = 2,
) -> float:
    """Return the sleep duration after *consecutive_errors* failures.

    * 0–``grace`` errors → ``base`` (transient hiccups keep the normal cadence).
    * Beyond grace → ``base × 2^(errors - grace)``, hard-capped at ``cap``.

    Examples with base=5, cap=300, grace=2::

        errors=0  → 5s      (normal)
        errors=2  → 5s      (still in grace)
        errors=3  → 10s
        errors=4  → 20s
        errors=5  → 40s
        errors=8  → 300s    (capped)
    """
    if consecutive_errors <= grace:
        return base
    factor = 2 ** (consecutive_errors - grace)
    return min(base * factor, cap)
