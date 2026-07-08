"""Shared test doubles for the suite."""

from __future__ import annotations


class FakeResponse:
    """Minimal stand-in for `requests.Response`: `ok`, `status_code`, `text`,
    and `.json()` — which raises ValueError when constructed with
    `json_body=None`, mimicking a body that doesn't parse as JSON."""

    def __init__(self, *, ok: bool = True, status_code: int = 200,
                 text: str = "", json_body=None):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json
