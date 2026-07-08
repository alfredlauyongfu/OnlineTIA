"""Tests for run.py's pure helpers (the pipeline orchestration itself is
exercised live; see README's verification notes)."""

from __future__ import annotations

import json
from pathlib import Path

import run


def _write_json(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ---------- report_prefix ----------

def test_report_prefix_uses_booking_id(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "TIA_id1234- 20260704_202211.json",
                      {"Booking ID": "id1234", "Q": "A"})
    assert run.report_prefix(src) == "TIA_id1234"


def test_report_prefix_sanitizes_booking_id(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "resp.json", {"Booking ID": "  id 12/34  "})
    assert run.report_prefix(src) == "TIA_id_12_34"


def test_report_prefix_missing_booking_id_falls_back_to_stem(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "some resp.json", {"Q": "A"})
    assert run.report_prefix(src) == "TIA_some_resp"


def test_report_prefix_non_string_booking_id_falls_back(tmp_path: Path) -> None:
    """A changed form schema (numeric/null Booking ID) must not crash naming."""
    src = _write_json(tmp_path, "resp1.json", {"Booking ID": 1234})
    assert run.report_prefix(src) == "TIA_resp1"
    src2 = _write_json(tmp_path, "resp2.json", {"Booking ID": ""})
    assert run.report_prefix(src2) == "TIA_resp2"


def test_report_prefix_unreadable_json_falls_back(tmp_path: Path) -> None:
    src = tmp_path / "broken.json"
    src.write_text("not json {", encoding="utf-8")
    assert run.report_prefix(src) == "TIA_broken"


def test_report_prefix_excel_uses_stem_rule(tmp_path: Path) -> None:
    src = tmp_path / "Customer Response (Q1).xlsx"
    src.write_bytes(b"x")
    assert run.report_prefix(src) == "TIA_Customer_Response_Q1"
