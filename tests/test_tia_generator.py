"""Tests for tia_generator. Pure-logic helpers tested directly; the
HTTP-touching `_call_rag_chat` method is tested with `requests.post`
monkey-patched so the suite runs offline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from tia_generator import TiaGenerationError, TiaReportGenerator, TIA_SYSTEM_PROMPT


def _make_gen(tmp_path: Path) -> TiaReportGenerator:
    return TiaReportGenerator(
        base_url="https://example.invalid",
        api_key="fake",
        llm_model="fake-model",
        output_dir=tmp_path / "out",
    )


# ---------- _read_customer_content ----------

def test_read_customer_content_reads_all_json(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"k": 1}), encoding="utf-8")
    (src / "b.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    # Non-JSON file is ignored by the glob.
    (src / "notes.txt").write_text("ignore me")
    result = TiaReportGenerator._read_customer_content(src)
    assert result == {"a.json": {"k": 1}, "b.json": [1, 2, 3]}


def test_read_customer_content_skips_unreadable_json(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (src / "bad.json").write_text("not json at all{", encoding="utf-8")
    result = TiaReportGenerator._read_customer_content(src)
    # Bad file is logged and skipped, good file is returned.
    assert "good.json" in result
    assert "bad.json" not in result


def test_read_customer_content_empty_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    assert TiaReportGenerator._read_customer_content(src) == {}


# ---------- _build_user_message ----------

def test_build_user_message_contains_payload(tmp_path: Path) -> None:
    content = {"sheet_a.json": {"name": "Alice", "n": 5}}
    msg = TiaReportGenerator._build_user_message(content)
    # The customer content shows up pretty-printed inside a json code fence.
    assert "Generate the Technical Infrastructure Assessment" in msg
    assert "```json" in msg
    assert '"name": "Alice"' in msg
    assert "sheet_a.json" in msg


# ---------- ctor defaults ----------

def test_default_tags_is_tia_reference(tmp_path: Path) -> None:
    gen = _make_gen(tmp_path)
    assert gen.reference_tags == ["tia_reference"]


def test_custom_tags_override_default(tmp_path: Path) -> None:
    gen = TiaReportGenerator(
        base_url="https://example.invalid",
        api_key="fake",
        llm_model="fake-model",
        output_dir=tmp_path / "out",
        reference_tags=["other_tag", "another"],
    )
    assert gen.reference_tags == ["other_tag", "another"]


def test_base_url_trailing_slash_stripped(tmp_path: Path) -> None:
    gen = TiaReportGenerator(
        base_url="https://example.invalid/",
        api_key="fake",
        llm_model="fake-model",
        output_dir=tmp_path / "out",
    )
    assert gen.base_url == "https://example.invalid"


def test_system_prompt_is_non_empty() -> None:
    # Sanity: the prompt text is present and mentions the key concept.
    # Collapse whitespace before substring match so we don't break when the
    # source string wraps "Technical\nInfrastructure Assessment".
    normalized = " ".join(TIA_SYSTEM_PROMPT.split())
    assert "Technical Infrastructure Assessment" in normalized
    assert "Markdown" in normalized


# ---------- _build_output_path ----------

def test_build_output_path_format(tmp_path: Path) -> None:
    gen = _make_gen(tmp_path)
    out = gen._build_output_path("PREFIX")
    # Format: {prefix}_{YYYYMMDD_HHMMSS}.md under output_dir
    assert out.parent == gen.output_dir
    assert out.suffix == ".md"
    assert out.name.startswith("PREFIX_")
    # Timestamp portion is 15 chars: YYYYMMDD_HHMMSS
    ts_part = out.stem.split("_", 1)[1]
    assert len(ts_part) == 15
    assert ts_part[8] == "_"


# ---------- _call_rag_chat: HTTP behaviour (mocked) ----------

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


def test_call_rag_chat_happy_path_returns_content(tmp_path: Path) -> None:
    """Well-formed gateway response: `content` extracted, returned verbatim."""
    gen = _make_gen(tmp_path)
    payload = {
        "content": "# Executive Summary\n\nFindings...",
        "rag_citations": [
            {"file_name": "ref.json", "page_number": 1, "score": 0.91},
        ],
    }
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)) as mock_post:
        result = gen._call_rag_chat("user msg")

    assert result == "# Executive Summary\n\nFindings..."
    assert mock_post.call_count == 1
    args, kwargs = mock_post.call_args
    assert args[0] == "https://example.invalid/rag/chat/completions"
    assert kwargs["headers"]["X-API-Key"] == "fake"
    # Tags from constructor get sent through.
    assert kwargs["json"]["tags"] == ["tia_reference"]
    assert kwargs["json"]["llm_name"] == "fake-model"
    # System prompt is wired in.
    assert "Infrastructure" in kwargs["json"]["rag_system_prompt"]


def test_call_rag_chat_non_2xx_raises(tmp_path: Path) -> None:
    gen = _make_gen(tmp_path)
    bad = _FakeResponse(ok=False, status_code=503, text="gateway boom")
    with patch("tia_generator.requests.post", return_value=bad):
        with pytest.raises(TiaGenerationError) as exc_info:
            gen._call_rag_chat("user msg")
    assert "rag/chat HTTP 503" in str(exc_info.value)
    assert "gateway boom" in str(exc_info.value)


def test_call_rag_chat_connection_error_propagates(tmp_path: Path) -> None:
    """ConnectionError / Timeout pass straight through — caller decides whether
    to retry or escalate to scheduler-level handling."""
    gen = _make_gen(tmp_path)
    with patch("tia_generator.requests.post",
               side_effect=requests.exceptions.ConnectionError("dns failed")):
        with pytest.raises(requests.exceptions.ConnectionError):
            gen._call_rag_chat("user msg")


def test_call_rag_chat_missing_content_raises(tmp_path: Path) -> None:
    """Payload lacks `content` → TiaGenerationError; nothing to write."""
    gen = _make_gen(tmp_path)
    payload = {"rag_citations": []}
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(TiaGenerationError) as exc_info:
            gen._call_rag_chat("user msg")
    assert "missing non-empty 'content'" in str(exc_info.value)


def test_call_rag_chat_empty_content_raises(tmp_path: Path) -> None:
    """Empty string is treated the same as missing — no useful report to write."""
    gen = _make_gen(tmp_path)
    payload = {"content": "   \n"}
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with pytest.raises(TiaGenerationError):
            gen._call_rag_chat("user msg")


def test_call_rag_chat_non_json_body_raises(tmp_path: Path) -> None:
    """2xx but body doesn't parse as JSON → TiaGenerationError."""
    gen = _make_gen(tmp_path)
    resp = _FakeResponse(ok=True, status_code=200, text="not json", json_body=None)
    with patch("tia_generator.requests.post", return_value=resp):
        with pytest.raises(TiaGenerationError) as exc_info:
            gen._call_rag_chat("user msg")
    assert "non-JSON response" in str(exc_info.value)


def test_call_rag_chat_handles_missing_citations(tmp_path: Path) -> None:
    """`rag_citations` absent → no crash; content still returned."""
    gen = _make_gen(tmp_path)
    payload = {"content": "report body"}
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        assert gen._call_rag_chat("user msg") == "report body"


# ---------- truncation guardrail ----------

def test_call_rag_chat_warns_when_completion_tokens_near_cap(tmp_path, caplog) -> None:
    """completion_tokens at/above the threshold → WARNING about truncation."""
    import logging
    gen = _make_gen(tmp_path)
    payload = {
        "content": "## Security\nbody",
        "rag_citations": [],
        "llm_usage": {"completion_tokens": 4096},
    }
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with caplog.at_level(logging.WARNING):
            gen._call_rag_chat("msg", section="Security")
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_call_rag_chat_no_warning_under_threshold(tmp_path, caplog) -> None:
    import logging
    gen = _make_gen(tmp_path)
    payload = {
        "content": "## Security\nbody",
        "rag_citations": [],
        "llm_usage": {"completion_tokens": 1200},
    }
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with caplog.at_level(logging.WARNING):
            gen._call_rag_chat("msg")
    assert not any("TRUNCATED" in r.message for r in caplog.records)


# ---------- sectioned generate() ----------

def test_generate_produces_all_sections(tmp_path, monkeypatch) -> None:
    """generate() runs the canonical analysis pass first, then one RAG call per
    section (in order), and assembles every section heading plus the document
    title. The internal analysis pass is NOT emitted as report content."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"k": "v"}), encoding="utf-8")
    gen = _make_gen(tmp_path)

    seen_sections: list[str] = []

    def fake_call(self, user_message, section=None):
        seen_sections.append(section)
        return f"## {section}\nContent for {section}."

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)

    out = gen.generate(src, filename_prefix="TIA_test")
    text = out.read_text(encoding="utf-8")

    expected = [title for title, _ in TiaReportGenerator.REPORT_SECTIONS]
    # Phase 1 analysis + 1b verification run first, then one call per section.
    assert seen_sections[0] == TiaReportGenerator.ANALYSIS_LABEL
    assert seen_sections[1] == TiaReportGenerator.VERIFICATION_LABEL
    assert seen_sections[2:] == expected
    assert text.startswith("# Technical Infrastructure Assessment")
    for title in expected:
        assert f"## {title}" in text                      # every section present
    # The internal analysis/verification passes are scaffolding, not sections.
    assert f"## {TiaReportGenerator.ANALYSIS_LABEL}" not in text
    assert f"## {TiaReportGenerator.VERIFICATION_LABEL}" not in text


def test_generate_injects_verified_analysis_into_every_section(tmp_path, monkeypatch) -> None:
    """The phase-1 draft is audited by the verification pass, and it is the
    VERIFIED analysis (not the raw draft) that is injected into each section as
    the authoritative source of truth."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)

    section_prompts: dict[str, str] = {}

    def fake_call(self, user_message, section=None):
        if section == TiaReportGenerator.ANALYSIS_LABEL:
            return "## Findings Ledger\nDRAFT-MARKER"
        if section == TiaReportGenerator.VERIFICATION_LABEL:
            # The verification pass receives the draft to audit...
            assert "DRAFT-MARKER" in user_message
            return "## Findings Ledger\nVERIFIED-MARKER"
        section_prompts[section] = user_message
        return f"## {section}\nbody"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    gen.generate(src, filename_prefix="TIA_test")

    assert section_prompts  # at least one section was rendered
    for section, prompt in section_prompts.items():
        # ...and it is the verified output that anchors the sections.
        assert "VERIFIED-MARKER" in prompt, f"{section} prompt missing verified analysis"
        assert "DRAFT-MARKER" not in prompt
        assert "AUTHORITATIVE ANALYSIS" in prompt


def test_read_customer_content_excludes_reference_sheets(tmp_path) -> None:
    """The embedded reference scaffolding sheets (Data, QandAData) are dropped
    from the customer payload; answer sheets are kept."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "wb__Summary.json").write_text('{"v": 6.8}', encoding="utf-8")
    (src / "wb__Questions.json").write_text('{"q": "a"}', encoding="utf-8")
    (src / "wb__Data.json").write_text('{"ref": "lookup"}', encoding="utf-8")
    (src / "wb__QandAData.json").write_text('{"ref": "guidance"}', encoding="utf-8")

    content = TiaReportGenerator._read_customer_content(src)

    assert set(content) == {"wb__Summary.json", "wb__Questions.json"}
    assert "wb__Data.json" not in content
    assert "wb__QandAData.json" not in content


def test_generate_strips_section_code_fences(tmp_path, monkeypatch) -> None:
    """A section the model wrapped in a ```markdown fence is unwrapped before
    assembly, so the final doc has no stray fences."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)

    def fake_call(self, user_message, section=None):
        return f"```markdown\n## {section}\nbody\n```"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    out = gen.generate(src, filename_prefix="TIA_test")
    text = out.read_text(encoding="utf-8")
    assert "```markdown" not in text


# ---------- assembly / directive / fence helpers ----------

def test_section_directive_names_the_section() -> None:
    d = TiaReportGenerator._section_directive("Security", "cover encryption")
    assert '"Security"' in d
    assert "## Security" in d
    assert "cover encryption" in d


def test_assemble_report_has_title_and_rules() -> None:
    out = TiaReportGenerator._assemble_report(["## A\nx", "## B\ny"])
    assert out.startswith("# Technical Infrastructure Assessment")
    assert "## A" in out and "## B" in out
    assert "\n---\n" in out


def test_strip_code_fence_unwraps_and_leaves_plain() -> None:
    fenced = "```markdown\n## Title\nbody\n```"
    assert TiaReportGenerator._strip_code_fence(fenced) == "## Title\nbody"
    plain = "## Title\nbody"
    assert TiaReportGenerator._strip_code_fence(plain) == plain
