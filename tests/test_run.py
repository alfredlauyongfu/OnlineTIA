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


def _submission(org="Acme Bank", env="Production",
                submitted="2026-09-16T16:15:27Z", booking="E30E0AD5-F9BA-456C"):
    """A form export carrying the four fields the report name is built from."""
    payload = {"Submission time": submitted}
    for key, answer in (("Organisation", org), ("Environment", env),
                        ("Booking ID", booking)):
        if answer is not None:
            payload[key] = _field(f"{key}?", answer)
    return payload


def test_report_prefix_is_org_env_date_and_booking(tmp_path: Path) -> None:
    """The readable name: who, which environment, when, and the booking link."""
    src = _write_json(tmp_path, "TIA_E30E0AD5- 20260916_161532.json", _submission())
    assert run.report_prefix(src) == "TIA_Acme_Bank_Production_2026-09-16_E30E0AD5"


def test_report_prefix_transliterates_accents(tmp_path: Path) -> None:
    """A customer's name must stay recognisable: plain sanitising would turn
    "Crédito Agrícola" into "Cr_dito_Agr_cola"."""
    src = _write_json(tmp_path, "r.json", _submission(org="Crédito Agrícola"))
    assert run.report_prefix(src).startswith("TIA_Credito_Agricola_Production_")


def test_report_prefix_keeps_only_the_leading_organisation_segment(tmp_path: Path) -> None:
    """Customers often answer "Company - Department - Team"; the company alone
    is what makes the filename recognisable."""
    src = _write_json(tmp_path, "r.json", _submission(
        org="Crédito Agrícola - Direção de Agilidade e Transformação - Produtividade"))
    assert run.report_prefix(src) == (
        "TIA_Credito_Agricola_Production_2026-09-16_E30E0AD5")


def test_report_prefix_caps_a_very_long_organisation(tmp_path: Path) -> None:
    src = _write_json(tmp_path, "r.json", _submission(org="A" * 80))
    org = run.report_prefix(src).split("_Production_")[0][len("TIA_"):]
    assert len(org) <= run.ORGANISATION_MAX_CHARS


def test_report_prefix_omits_a_blank_booking_id(tmp_path: Path) -> None:
    """An absent segment is dropped, not left as a dangling separator."""
    src = _write_json(tmp_path, "r.json", _submission(booking=""))
    assert run.report_prefix(src) == "TIA_Acme_Bank_Production_2026-09-16"


def test_report_prefix_falls_back_to_run_date_on_bad_submission_time(tmp_path: Path) -> None:
    import datetime as dt
    src = _write_json(tmp_path, "r.json", _submission(submitted="not a date"))
    today = dt.datetime.now().strftime("%Y-%m-%d")
    assert run.report_prefix(src) == f"TIA_Acme_Bank_Production_{today}_E30E0AD5"


def test_report_prefix_without_organisation_falls_back_to_stem(tmp_path: Path) -> None:
    """No Organisation means no recognisable name, so the stem rule applies."""
    src = _write_json(tmp_path, "some resp.json", _submission(org=None))
    assert run.report_prefix(src) == "TIA_some_resp"


def test_report_prefix_non_string_booking_id_still_names(tmp_path: Path) -> None:
    """A changed form schema (numeric Booking ID) must not crash naming — the
    segment is simply dropped."""
    src = _write_json(tmp_path, "resp1.json", _submission(booking=1234))
    assert run.report_prefix(src) == "TIA_Acme_Bank_Production_2026-09-16"


def test_report_prefix_does_not_double_the_tia_prefix(tmp_path: Path) -> None:
    """No Organisation falls back to the stem — but the flow already names files
    "TIA_<booking>- <ts>.json", so the stem carries the prefix and an empty
    booking-id separator. The result must not come out as "TIA_TIA_-_..."."""
    src = _write_json(tmp_path, "TIA_- 20260915_091757_acme.json",
                      {"Booking ID": _field("Booking ID?", "")})
    assert run.report_prefix(src) == "TIA_20260915_091757_acme"


def test_report_prefix_tolerates_flat_fields(tmp_path: Path) -> None:
    """Naming is deliberately lenient — it reads flat (pre-flow-update) values
    too, so a hand-written file still names sensibly. The clean break against
    those exports is enforced in generation (which rejects them and writes no
    report), not here."""
    src = _write_json(tmp_path, "old resp.json", {
        "Organisation": "Acme Bank", "Environment": "Production",
        "Submission time": "2026-09-16T16:15:27Z", "Booking ID": "id1234"})
    assert run.report_prefix(src) == "TIA_Acme_Bank_Production_2026-09-16_id1234"


def test_report_prefix_unreadable_json_falls_back(tmp_path: Path) -> None:
    src = tmp_path / "broken.json"
    src.write_text("not json {", encoding="utf-8")
    assert run.report_prefix(src) == "TIA_broken"


def test_report_prefix_excel_uses_stem_rule(tmp_path: Path) -> None:
    src = tmp_path / "Customer Response (Q1).xlsx"
    src.write_bytes(b"x")
    assert run.report_prefix(src) == "TIA_Customer_Response_Q1"
