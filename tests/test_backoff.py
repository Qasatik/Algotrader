"""Tests for the exponential backoff helper used by the carry runner loop.

The runner keeps the normal poll cadence for a couple of transient errors
(the grace window), then backs off exponentially (capped) on sustained
failures so a down exchange isn't hammered hundreds of times per hour.
"""
from utils.backoff import backoff_seconds


def test_grace_period_keeps_normal_cadence():
    # 0..grace errors -> base interval (transient hiccups stay responsive)
    assert backoff_seconds(0, base=5) == 5
    assert backoff_seconds(1, base=5) == 5
    assert backoff_seconds(2, base=5) == 5  # grace == 2


def test_exponential_growth_beyond_grace():
    assert backoff_seconds(3, base=5) == 10   # 5 * 2^1
    assert backoff_seconds(4, base=5) == 20   # 5 * 2^2
    assert backoff_seconds(5, base=5) == 40   # 5 * 2^3


def test_capped_at_default_max():
    # 5 * 2^18 is huge -> hard-capped at the 300s default
    assert backoff_seconds(20, base=5) == 300


def test_custom_cap_respected():
    assert backoff_seconds(10, base=5, cap=60) == 60


def test_custom_grace_respected():
    # grace=0 -> errors=1 already backs off
    assert backoff_seconds(1, base=5, grace=0) == 10
