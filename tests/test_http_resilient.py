"""Tests for http_resilient.call_resilient — the hard-cap + retry wrapper.

The decisive test is `test_hard_cap_bounds_a_hang`: it proves a stuck call is
bounded by wall clock (and raises) rather than hanging — the whole point of the
change."""

from __future__ import annotations

import time

import pytest
import requests

from http_resilient import call_resilient


def test_returns_result_on_success() -> None:
    assert call_resilient(lambda: "ok", label="x") == "ok"


def test_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("transient")
        return "ok"

    assert call_resilient(fn, label="x", attempts=2, backoff=0) == "ok"
    assert calls["n"] == 2  # failed once, retried, succeeded


def test_raises_after_attempts_exhausted() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise requests.exceptions.Timeout("always")

    with pytest.raises(requests.exceptions.Timeout):
        call_resilient(fn, label="x", attempts=2, backoff=0)
    assert calls["n"] == 2


def test_hard_cap_bounds_a_hang() -> None:
    """A call that sleeps far longer than the cap must raise quickly — the
    wall-clock watchdog, not the underlying call, bounds the time."""
    def fn():
        time.sleep(5)
        return "never"

    start = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        call_resilient(fn, label="x", hard_cap=0.2, attempts=1, backoff=0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # bounded by ~hard_cap (0.2s), NOT the 5s sleep


def test_default_hard_cap_derives_from_read_timeout(monkeypatch) -> None:
    """Without an explicit hard_cap, the cap is read_timeout + HARD_CAP_MARGIN,
    so a caller that raises its read timeout is never clipped by a stale fixed
    cap. (Margin shrunk here so the derived cap fires fast.)"""
    import http_resilient as hr

    monkeypatch.setattr(hr, "HARD_CAP_MARGIN", 0.1)

    def fn():
        time.sleep(5)
        return "never"

    start = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        hr.call_resilient(fn, label="x", read_timeout=0.1, attempts=1, backoff=0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0  # derived cap (0.1 + 0.1s) bounded it, not the 5s sleep


def test_default_cap_matches_documented_five_and_a_half_minutes() -> None:
    """Lock the documented default: READ_TIMEOUT + HARD_CAP_MARGIN == 330s
    (the "~5.5 min" hard cap the README's troubleshooting section promises)."""
    import http_resilient as hr

    assert hr.READ_TIMEOUT + hr.HARD_CAP_MARGIN == 330.0


def test_hard_cap_then_retry_succeeds() -> None:
    """First attempt hangs past the cap; the retry returns fast."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(5)   # blows the cap on attempt 1
        return "ok"

    assert call_resilient(fn, label="x", hard_cap=0.2, attempts=2, backoff=0) == "ok"
    assert calls["n"] == 2


def test_non_retryable_propagates_without_retry() -> None:
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("deterministic")

    with pytest.raises(ValueError):
        call_resilient(fn, label="x", attempts=3, backoff=0)
    assert calls["n"] == 1  # a non-Timeout/Connection error is not retried
