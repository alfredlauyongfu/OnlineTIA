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

def _field(question: str, answer) -> dict:
    """One field in the form export's nested shape."""
    return {"question": question, "answer": answer}


def test_report_prefix_uses_booking_id(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "TIA_id1234- 20260704_202211.json",
                      {"Booking ID": _field("Booking ID. (From ...)", "id1234"),
                       "Q": _field("Q?", "A")})
    assert run.report_prefix(src) == "TIA_id1234"


def test_report_prefix_sanitizes_booking_id(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "resp.json",
                      {"Booking ID": _field("Booking ID?", "  id 12/34  ")})
    assert run.report_prefix(src) == "TIA_id_12_34"


def test_report_prefix_missing_booking_id_falls_back_to_stem(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "some resp.json", {"Q": _field("Q?", "A")})
    assert run.report_prefix(src) == "TIA_some_resp"


def test_report_prefix_non_string_booking_id_falls_back(tmp_path: Path) -> None:
    """A changed form schema (numeric/null Booking ID) must not crash naming."""
    src = _write_json(tmp_path, "resp1.json", {"Booking ID": _field("B?", 1234)})
    assert run.report_prefix(src) == "TIA_resp1"
    src2 = _write_json(tmp_path, "resp2.json", {"Booking ID": _field("B?", "")})
    assert run.report_prefix(src2) == "TIA_resp2"


def test_report_prefix_does_not_double_the_tia_prefix(tmp_path: Path) -> None:
    """A blank Booking ID falls back to the stem — but the flow already names
    files "TIA_<booking>- <ts>.json", so the stem carries the prefix and the
    result must not come out as "TIA_TIA_..."."""
    src = _write_json(tmp_path, "TIA_- 20260915_091757_santander.json",
                      {"Booking ID": _field("Booking ID?", "")})
    assert run.report_prefix(src) == "TIA_20260915_091757_santander"


def test_report_prefix_tolerates_flat_booking_id(tmp_path: Path) -> None:
    """Naming is deliberately lenient — it reads a flat Booking ID too, so a
    hand-written file still names sensibly. The clean break against
    pre-flow-update exports is enforced in generation (which rejects them and
    writes no report), not here."""
    src = _write_json(tmp_path, "old resp.json", {"Booking ID": "id1234"})
    assert run.report_prefix(src) == "TIA_id1234"


def test_report_prefix_unreadable_json_falls_back(tmp_path: Path) -> None:
    src = tmp_path / "broken.json"
    src.write_text("not json {", encoding="utf-8")
    assert run.report_prefix(src) == "TIA_broken"


def test_report_prefix_excel_uses_stem_rule(tmp_path: Path) -> None:
    src = tmp_path / "Customer Response (Q1).xlsx"
    src.write_bytes(b"x")
    assert run.report_prefix(src) == "TIA_Customer_Response_Q1"
