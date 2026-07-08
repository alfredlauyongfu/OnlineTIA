"""Hard wall-clock cap + bounded retry around a synchronous HTTP call.

`requests`' `timeout=` is a read-INACTIVITY timeout (max gap between bytes),
not a total cap. Under machine sleep (freezes the socket timer) or a
trickling/keepalive connection it can hang far beyond the nominal value — which
once hung a run for ~3 hours. `call_resilient` runs the call on a daemon thread
and bounds it with `Thread.join(hard_cap)`, so:

  - a hang is bounded by wall clock and RAISES `requests.exceptions.Timeout`
    (which every caller already catches) instead of hanging the pipeline;
  - a transient stall is auto-retried (the incident above would have recovered
    in minutes);
  - a leaked stuck thread is a daemon, so it never blocks process exit; on
    machine wake the join deadline has already passed, so it returns at once.

Retries fire only on Timeout/ConnectionError. HTTP non-2xx is raised by the
caller AFTER this returns the Response, so it stays out of the retry loop —
deterministic failures are not hammered.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import requests


logger = logging.getLogger(__name__)


# Tunable defaults (no env vars — adjust here if the gateway's latency changes).
# The longest *legitimate* calls observed are ~3 min (the SQL_Server extraction
# and the largest TIA sections), so a 300s read timeout never clips a real call.
# The hard cap is always derived as read_timeout + HARD_CAP_MARGIN, so a caller
# that raises its read timeout is never silently clipped by a stale fixed cap;
# with the defaults that bounds a hang to 330s ≈ 5.5 min/attempt.
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 300.0
HARD_CAP_MARGIN = 30.0
ATTEMPTS = 2          # 1 retry
BACKOFF = 3.0         # seconds between attempts

# The gateway-down / transient pair. Shared project-wide: it is both the
# retry trigger here and the "gateway unreachable" catch tuple in every caller.
TRANSIENT_ERRORS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)


def call_resilient(
    fn: Callable[[], Any],
    *,
    label: str,
    read_timeout: float = READ_TIMEOUT,
    hard_cap: float | None = None,
    attempts: int = ATTEMPTS,
    backoff: float = BACKOFF,
) -> Any:
    """Run no-arg `fn` (an HTTP call returning a Response) under a hard
    wall-clock cap with bounded retries.

    `read_timeout` is the per-request read timeout the caller gave `fn`; when
    `hard_cap` is not set explicitly it is derived as
    `read_timeout + HARD_CAP_MARGIN`, so the watchdog always sits just above
    the request's own timeout instead of clipping longer-configured calls.

    Returns `fn()`'s result, or raises:
      - `requests.exceptions.Timeout` if `fn` exceeds `hard_cap` (a hang), or
      - the underlying Timeout/ConnectionError once `attempts` is exhausted.
    A non-retryable exception from `fn` propagates immediately (no retry).
    """
    if hard_cap is None:
        hard_cap = read_timeout + HARD_CAP_MARGIN
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        box: dict[str, Any] = {}

        def _run() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - captured for the caller
                box["error"] = exc

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(hard_cap)

        if worker.is_alive():
            # Hung past the cap — the watchdog fires. The daemon thread is left
            # to die on its own (connect, read) timeout; it can't block exit.
            last_exc = requests.exceptions.Timeout(
                f"{label}: no response within {hard_cap:.0f}s (hard cap)"
            )
            logger.warning(
                "resilient: %s hit hard cap %.0fs (attempt %d/%d)",
                label, hard_cap, attempt, attempts,
            )
        elif "error" in box:
            last_exc = box["error"]
            if not isinstance(last_exc, TRANSIENT_ERRORS):
                raise last_exc  # deterministic failure — do not retry
            logger.warning(
                "resilient: %s failed: %s (attempt %d/%d)",
                label, last_exc, attempt, attempts,
            )
        else:
            return box["result"]

        if attempt < attempts:
            time.sleep(backoff)

    assert last_exc is not None
    raise last_exc


def raise_for_status(
    response: requests.Response, *, label: str, error_cls: type[Exception]
) -> None:
    """If `response` isn't 2xx, log and raise `error_cls` with the project's
    standard `{label} HTTP {status}: {body}` message. Shared by every gateway
    caller so the error format (and its truncation) is defined once."""
    if response.ok:
        return
    logger.error(
        "%s FAILED: HTTP %d: %s",
        label, response.status_code, response.text[:500],
    )
    raise error_cls(
        f"{label} HTTP {response.status_code}: {response.text[:500]}"
    )


def parse_json(
    response: requests.Response, *, label: str, error_cls: type[Exception]
) -> Any:
    """`response.json()` with the project's standard log + `error_cls` when
    the body doesn't parse as JSON."""
    try:
        return response.json()
    except ValueError as exc:
        logger.error(
            "%s FAILED: bad JSON: %s; body=%s", label, exc, response.text[:500],
        )
        raise error_cls(
            f"{label} non-JSON response: {response.text[:500]}"
        ) from exc
