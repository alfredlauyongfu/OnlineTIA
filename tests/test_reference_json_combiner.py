"""Tests for reference_json_combiner. Pure-logic helpers tested directly;
the HTTP-touching `_call_llm` method is tested with `requests.post`
monkey-patched so the suite runs offline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from reference_json_combiner import GatewayUnreachable, ReferenceJsonCombiner


@pytest.mark.parametrize(
    "filename, expected",
    [
        # Files produced by ExcelToJsonConverter use `__` between workbook
        # stem and sheet name; the combiner should pull out the sheet part.
        ("Technical Infrastructure Assessment V2.6.4__SQL_Server.json", "SQL_Server"),
        ("foo__bar.json", "bar"),
        ("plain_no_separator.json", "plain_no_separator"),
        ("a__b__c.json", "b__c"),   # only split on the FIRST `__`
        ("__leading_sep.json", "leading_sep"),
    ],
)
def test_sheet_name_from_filename(filename: str, expected: str) -> None:
    assert ReferenceJsonCombiner._sheet_name_from_filename(Path(filename)) == expected


# ---------- _call_llm: HTTP behaviour (mocked) ----------

class _FakeResponse:
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


def _make_combiner(tmp_path: Path) -> ReferenceJsonCombiner:
    return ReferenceJsonCombiner(
        api_url="https://example.invalid",
        api_key="fake-key",
        user_id="user-fake",
        use_case_id="uc-fake",
        model="fake-model",
        reference_json_dir=tmp_path,
    )


def test_call_llm_happy_path_parses_choices_message_content(tmp_path: Path) -> None:
    """A well-formed gateway response is unwrapped, JSON-parsed, and returned."""
    combiner = _make_combiner(tmp_path)
    payload = {
        "choices": [{"message": {"content": '{"topic_a": [1, 2], "topic_b": "x"}'}}],
    }
    with patch("reference_json_combiner.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)) as mock_post:
        result = combiner._call_llm("sys", "user", 100, label="extract:Sheet1")

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
    combiner = _make_combiner(tmp_path)
    bad = _FakeResponse(ok=False, status_code=500, text="server explosion")
    with patch("reference_json_combiner.requests.post", return_value=bad):
        with pytest.raises(RuntimeError) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "LLM HTTP 500" in str(exc_info.value)
    assert "server explosion" in str(exc_info.value)


def test_call_llm_connection_error_raises_gateway_unreachable(tmp_path: Path) -> None:
    """ConnectionError / Timeout must be re-raised as GatewayUnreachable so
    the calling loop can abort fast instead of trying every other sheet."""
    combiner = _make_combiner(tmp_path)
    with patch("reference_json_combiner.requests.post",
               side_effect=requests.exceptions.ConnectionError("boom")):
        with pytest.raises(GatewayUnreachable) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "Cannot reach gateway" in str(exc_info.value)


def test_call_llm_malformed_outer_json_raises_runtime(tmp_path: Path) -> None:
    """Body claims 2xx but doesn't parse as JSON → RuntimeError."""
    combiner = _make_combiner(tmp_path)
    resp = _FakeResponse(ok=True, status_code=200, text="not json", json_body=None)
    with patch("reference_json_combiner.requests.post", return_value=resp):
        with pytest.raises(RuntimeError) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "Unexpected LLM response shape" in str(exc_info.value)


def test_call_llm_inner_content_not_json_raises_runtime(tmp_path: Path) -> None:
    """The wrapping shape parses, but the inner `content` is not JSON."""
    combiner = _make_combiner(tmp_path)
    payload = {"choices": [{"message": {"content": "definitely not JSON"}}]}
    with patch("reference_json_combiner.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "non-JSON content" in str(exc_info.value)


def test_call_llm_inner_content_not_dict_raises_runtime(tmp_path: Path) -> None:
    """Content parses as JSON but is a list/scalar — caller expects an object."""
    combiner = _make_combiner(tmp_path)
    payload = {"choices": [{"message": {"content": "[1, 2, 3]"}}]}
    with patch("reference_json_combiner.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "non-object JSON" in str(exc_info.value)


def test_call_llm_empty_content_raises_runtime(tmp_path: Path) -> None:
    combiner = _make_combiner(tmp_path)
    payload = {"choices": [{"message": {"content": ""}}]}
    with patch("reference_json_combiner.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(RuntimeError) as exc_info:
            combiner._call_llm("sys", "user", 100, label="extract:X")
    assert "no content" in str(exc_info.value)
