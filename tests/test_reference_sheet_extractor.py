"""Tests for reference_sheet_extractor. Pure-logic helpers tested directly;
the HTTP-touching `_call_llm` method is tested with `requests.post`
monkey-patched so the suite runs offline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from reference_sheet_extractor import GatewayUnreachable, ReferenceSheetExtractor
from tests.helpers import FakeResponse as _FakeResponse


@pytest.mark.parametrize(
    "filename, expected",
    [
        # Files produced by ExcelToJsonConverter use `__` between workbook
        # stem and sheet name; the extractor should pull out the sheet part.
        ("Technical Infrastructure Assessment V2.6.4__SQL_Server.json", "SQL_Server"),
        ("foo__bar.json", "bar"),
        ("plain_no_separator.json", "plain_no_separator"),
        ("a__b__c.json", "b__c"),   # only split on the FIRST `__`
        ("__leading_sep.json", "leading_sep"),
    ],
)
def test_sheet_name_from_filename(filename: str, expected: str) -> None:
    assert ReferenceSheetExtractor._sheet_name_from_filename(Path(filename)) == expected


# ---------- extract_sheets: a partial rubric must report failure ----------

def _source_sheets(tmp_path: Path, *names: str) -> Path:
    """Per-sheet source JSONs as ExcelToJsonConverter writes them."""
    import json
    for n in names:
        (tmp_path / f"WB__{n}.json").write_text(
            json.dumps([{"q": "a"}]), encoding="utf-8")
    return tmp_path


def test_a_failed_sheet_leaves_the_previous_rubric_intact(
        tmp_path: Path, monkeypatch) -> None:
    """A partial extraction must be a NO-OP.

    Previously the old set was wiped up-front and each sheet written as it went,
    so a mid-run failure destroyed a complete rubric and left a fragment. Now
    nothing is written unless every sheet succeeds.
    """
    src = _source_sheets(tmp_path, "SQL_Server", "Security")
    previous = src / "extracted_SQL_Server_20260101_000000.json"
    previous.write_text('{"sql_servers": {"kept": true}}', encoding="utf-8")
    ex = _make_extractor(src)

    def fake_extract(self, sheet_name, sheet_data):
        if sheet_name == "SQL_Server":
            raise RuntimeError("HTTP 429: Monthly spend limit exceeded")
        return {"security": {"x": 1}}

    monkeypatch.setattr(ReferenceSheetExtractor, "_extract_sheet", fake_extract)
    assert ex.extract_sheets() == 1
    # The good previous rubric survives, and the half-run wrote nothing.
    assert [p.name for p in tmp_path.glob("extracted_*.json")] == [previous.name]
    assert previous.read_text(encoding="utf-8") == '{"sql_servers": {"kept": true}}'


def test_quota_error_aborts_the_remaining_sheets(tmp_path: Path, monkeypatch) -> None:
    """A 429 means no later sheet can succeed either, so stop immediately
    instead of walking every remaining sheet into the same wall."""
    from reference_sheet_extractor import QuotaExceeded

    ex = _make_extractor(_source_sheets(tmp_path, "A_Sheet", "B_Sheet", "C_Sheet"))
    attempts: list[str] = []

    def fake_extract(self, sheet_name, sheet_data):
        attempts.append(sheet_name)
        raise QuotaExceeded("LLM HTTP 429 (rate limit or spend cap)")

    monkeypatch.setattr(ReferenceSheetExtractor, "_extract_sheet", fake_extract)
    assert ex.extract_sheets() == 1
    assert attempts == ["A_Sheet"]                       # aborted after the first
    assert list(tmp_path.glob("extracted_*.json")) == []


def test_extract_sheets_succeeds_when_every_sheet_extracts(
        tmp_path: Path, monkeypatch) -> None:
    ex = _make_extractor(_source_sheets(tmp_path, "SQL_Server", "Security"))
    monkeypatch.setattr(ReferenceSheetExtractor, "_extract_sheet",
                        lambda self, n, d: {"topic": {"x": 1}})
    assert ex.extract_sheets() == 0
    assert len(list(tmp_path.glob("extracted_*.json"))) == 2


def test_extract_sheets_empty_sheet_is_not_a_failure(
        tmp_path: Path, monkeypatch) -> None:
    """A sheet with genuinely nothing to extract is normal, not an error."""
    ex = _make_extractor(_source_sheets(tmp_path, "Change_Log"))
    monkeypatch.setattr(ReferenceSheetExtractor, "_extract_sheet",
                        lambda self, n, d: {})
    assert ex.extract_sheets() == 0


# ---------- _call_llm: HTTP behaviour (mocked) ----------

def _make_extractor(tmp_path: Path) -> ReferenceSheetExtractor:
    return ReferenceSheetExtractor(
        api_url="https://example.invalid",
        api_key="fake-key",
        user_id="user-fake",
        use_case_id="uc-fake",
        model="fake-model",
        reference_json_dir=tmp_path,
    )


def test_call_llm_happy_path_parses_choices_message_content(tmp_path: Path) -> None:
    """A well-formed gateway response is unwrapped, JSON-parsed, and returned."""
    extractor = _make_extractor(tmp_path)
    payload = {
        "choices": [{"message": {"content": '{"topic_a": [1, 2], "topic_b": "x"}'}}],
    }
    with patch("reference_sheet_extractor.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)) as mock_post:
        result = extractor._call_llm("sys", "user", 100, label="extract:Sheet1")

    assert result == {"topic_a": [1, 2], "topic_b": "x"}
    # One call, to the expected URL, with bearer auth + custom headers.
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == "https://example.invalid/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer fake-key"
    assert kwargs["headers"]["X-User-Id"] == "user-fake"
    assert kwargs["headers"]["X-Use-Case-Id"] == "uc-fake"
    assert kwargs["json"]["model"] == "fake-model"
    assert kwargs["json"]["max_tokens"] == 100


def test_call_llm_non_2xx_raises_runtime(tmp_path: Path) -> None:
    extractor = _make_extractor(tmp_path)
    bad = _FakeResponse(ok=False, status_code=500, text="server explosion")
    with patch("reference_sheet_extractor.requests.post", return_value=bad):
        with pytest.raises(RuntimeError) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "LLM HTTP 500" in str(exc_info.value)
    assert "server explosion" in str(exc_info.value)


def test_call_llm_connection_error_raises_gateway_unreachable(tmp_path: Path) -> None:
    """ConnectionError / Timeout must be re-raised as GatewayUnreachable so
    the calling loop can abort fast instead of trying every other sheet."""
    extractor = _make_extractor(tmp_path)
    with patch("reference_sheet_extractor.requests.post",
               side_effect=requests.exceptions.ConnectionError("boom")):
        with pytest.raises(GatewayUnreachable) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "Cannot reach gateway" in str(exc_info.value)


def test_call_llm_malformed_outer_json_raises_runtime(tmp_path: Path) -> None:
    """Body claims 2xx but doesn't parse as JSON → RuntimeError (shared
    parse_json helper's message)."""
    extractor = _make_extractor(tmp_path)
    resp = _FakeResponse(ok=True, status_code=200, text="not json", json_body=None)
    with patch("reference_sheet_extractor.requests.post", return_value=resp):
        with pytest.raises(RuntimeError) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "non-JSON response" in str(exc_info.value)


def test_call_llm_inner_content_not_json_raises_runtime(tmp_path: Path) -> None:
    """The wrapping shape parses, but the inner `content` is not JSON."""
    extractor = _make_extractor(tmp_path)
    payload = {"choices": [{"message": {"content": "definitely not JSON"}}]}
    with patch("reference_sheet_extractor.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "non-JSON content" in str(exc_info.value)


def test_call_llm_inner_content_not_dict_raises_runtime(tmp_path: Path) -> None:
    """Content parses as JSON but is a list/scalar — caller expects an object."""
    extractor = _make_extractor(tmp_path)
    payload = {"choices": [{"message": {"content": "[1, 2, 3]"}}]}
    with patch("reference_sheet_extractor.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "non-object JSON" in str(exc_info.value)


def test_call_llm_empty_content_raises_runtime(tmp_path: Path) -> None:
    extractor = _make_extractor(tmp_path)
    payload = {"choices": [{"message": {"content": ""}}]}
    with patch("reference_sheet_extractor.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            extractor._call_llm("sys", "user", 100, label="extract:X")
    assert "no content" in str(exc_info.value)
