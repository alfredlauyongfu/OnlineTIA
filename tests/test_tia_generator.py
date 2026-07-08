"""Tests for tia_generator. Pure-logic helpers tested directly; the
HTTP-touching `_call_rag_chat` method is tested with `requests.post`
monkey-patched so the suite runs offline."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from tia_generator import TiaGenerationError, TiaReportGenerator, TIA_SYSTEM_PROMPT
from tests.helpers import FakeResponse as _FakeResponse


def _make_gen(tmp_path: Path) -> TiaReportGenerator:
    return TiaReportGenerator(
        base_url="https://example.invalid",
        api_key="fake",
        llm_model="fake-model",
        output_dir=tmp_path / "out",
    )


def _ledger_analysis(rows) -> str:
    """A canonical-analysis string with a parseable `## Assessment Ledger`.
    `rows` = list of (category, subject, question, answer, criticality, detail);
    use "—" for an unflagged row's criticality."""
    out = [
        "## Environment Facts", "| Fact | Value |", "|---|---|", "| version | 7.4.1 |",
        "", "## Assessment Ledger",
        "| ID | Category | Subject | Question | Answer | Criticality | Detail |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, (cat, sub, q, ans, crit, det) in enumerate(rows, start=1):
        out.append(f"| R{i} | {cat} | {sub} | {q} | {ans} | {crit} | {det} |")
    out += ["", "## Positive Confirmations", "- none",
            "", "## Criticality Tally", "Red Flag: 0 ()"]
    return "\n".join(out)


def _analysis_fake_call(rows, section_body="body"):
    """Build a fake `_call_rag_chat` that returns a ledger analysis for the
    canonical/verification passes and simple bodies for the LLM sections."""
    def fake_call(self, user_message, section=None, read_timeout=None):
        if section in (TiaReportGenerator.ANALYSIS_LABEL,
                       TiaReportGenerator.VERIFICATION_LABEL):
            return _ledger_analysis(rows)
        return f"## {section}\n{section_body}"
    return fake_call


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


def test_call_rag_chat_non_dict_json_raises(tmp_path: Path) -> None:
    """2xx with a JSON *array* body → TiaGenerationError, NOT AttributeError.
    An AttributeError would escape run.py's per-file handling, abort the whole
    multi-file run, and skip finalize_to_processed_dir()."""
    gen = _make_gen(tmp_path)
    resp = _FakeResponse(ok=True, status_code=200, text='["boom"]', json_body=["boom"])
    with patch("tia_generator.requests.post", return_value=resp):
        with pytest.raises(TiaGenerationError) as exc_info:
            gen._call_rag_chat("user msg")
    assert "non-object JSON" in str(exc_info.value)


def test_call_rag_chat_handles_missing_citations(tmp_path: Path) -> None:
    """`rag_citations` absent → no crash; content still returned."""
    gen = _make_gen(tmp_path)
    payload = {"content": "report body"}
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        assert gen._call_rag_chat("user msg") == "report body"


# ---------- truncation guardrail (finish_reason, not a token guess) ----------

def test_call_rag_chat_warns_on_finish_reason_length(tmp_path, caplog) -> None:
    """finish_reason signalling a token cut-off → truncation WARNING."""
    import logging
    gen = _make_gen(tmp_path)
    payload = {
        "content": "## Security\nbody",
        "rag_citations": [],
        "finish_reason": "length",
        "llm_usage": {"completion_tokens": 4096},
    }
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with caplog.at_level(logging.WARNING):
            gen._call_rag_chat("msg", section="Security")
    assert any("TRUNCATED" in r.message for r in caplog.records)


def test_call_rag_chat_no_warning_when_complete_despite_high_tokens(tmp_path, caplog) -> None:
    """A large completion that finished naturally (5745 tokens, finish_reason
    stop) must NOT warn — the old ~4096 token heuristic is gone."""
    import logging
    gen = _make_gen(tmp_path)
    payload = {
        "content": "## Security\nbody",
        "rag_citations": [],
        "finish_reason": "stop",
        "llm_usage": {"completion_tokens": 5745},
    }
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with caplog.at_level(logging.WARNING):
            gen._call_rag_chat("msg")
    assert not any("TRUNCATED" in r.message for r in caplog.records)


def test_call_rag_chat_no_warning_when_finish_reason_absent(tmp_path, caplog) -> None:
    """No finish_reason in the payload → no token-based false alarm."""
    import logging
    gen = _make_gen(tmp_path)
    payload = {"content": "## Security\nbody", "llm_usage": {"completion_tokens": 9000}}
    with patch("tia_generator.requests.post",
               return_value=_FakeResponse(ok=True, json_body=payload)):
        with caplog.at_level(logging.WARNING):
            gen._call_rag_chat("msg")
    assert not any("TRUNCATED" in r.message for r in caplog.records)


def test_extract_finish_reason_shapes() -> None:
    f = TiaReportGenerator._extract_finish_reason
    assert f({"finish_reason": "stop"}) == "stop"
    assert f({"stop_reason": "length"}) == "length"
    assert f({"choices": [{"finish_reason": "length"}]}) == "length"
    assert f({"content": "x"}) is None


# ---------- sectioned generate() ----------

def test_generate_produces_all_sections(tmp_path, monkeypatch) -> None:
    """generate() runs analysis + verification, LLM-generates the three
    narrative sections, and code-renders the Detailed Assessment from the
    ledger. The 7 categories are NOT LLM calls."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"SQL connection encrypted": "No"}),
                                encoding="utf-8")
    gen = _make_gen(tmp_path)

    seen_sections: list[str] = []

    def fake_call(self, user_message, section=None, read_timeout=None):
        seen_sections.append(section)
        if section in (TiaReportGenerator.ANALYSIS_LABEL,
                       TiaReportGenerator.VERIFICATION_LABEL):
            return _ledger_analysis([
                ("SQL Server", "SQL connection encrypted",
                 "Are the connections secured?", "No", "Red Flag", "Encrypt it."),
            ])
        return f"## {section}\nContent for {section}."

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)

    out = gen.generate(src, filename_prefix="TIA_test")
    text = out.read_text(encoding="utf-8")

    # Only analysis, verification, and the 3 narrative sections are LLM calls —
    # the 7 Detailed Assessment categories are code-rendered.
    assert seen_sections == [
        TiaReportGenerator.ANALYSIS_LABEL, TiaReportGenerator.VERIFICATION_LABEL,
        "Summary", "Key Findings", "Outstanding Questions",
    ]
    assert text.startswith("# Technical Infrastructure Assessment")
    for h in ("## Summary", "## Key Findings", "## Detailed Assessment",
              "## Outstanding Questions"):
        assert h in text
    # The code-rendered category block came from the ledger.
    assert "### SQL Server" in text
    assert "Are the connections secured? — Red Flag" in text
    assert f"## {TiaReportGenerator.ANALYSIS_LABEL}" not in text


def test_generate_also_writes_sibling_docx(tmp_path, monkeypatch) -> None:
    """generate() returns the .md path but also writes a sibling .docx with the
    same stem (independent, best-effort Word output)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)

    def fake_call(self, user_message, section=None, read_timeout=None):
        return f"## {section}\nbody"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    out = gen.generate(src, filename_prefix="TIA_test")

    assert out.suffix == ".md" and out.exists()
    docx = out.with_suffix(".docx")
    assert docx.exists() and docx.stat().st_size > 0


def test_generate_writes_partial_report_on_section_failure(tmp_path, monkeypatch) -> None:
    """If a section fails after retries, a flagged PARTIAL .md + .docx are
    written from the completed sections and generate() raises (so run.py
    defers the file) — completed work is not discarded."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)

    fail_on = "Key Findings"   # an LLM section (categories can't fail — code-rendered)

    def fake_call(self, user_message, section=None, read_timeout=None):
        if section in (TiaReportGenerator.ANALYSIS_LABEL,
                       TiaReportGenerator.VERIFICATION_LABEL):
            return _ledger_analysis([("SQL Server", "s", "q?", "a", "Red Flag", "d")])
        if section == fail_on:
            raise TiaGenerationError("simulated gateway failure")
        return f"## {section}\nbody"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)

    with pytest.raises(TiaGenerationError):
        gen.generate(src, filename_prefix="TIA_test")

    mds = list((tmp_path / "out").glob("TIA_test_*.md"))
    assert len(mds) == 1                                  # partial .md written
    text = mds[0].read_text(encoding="utf-8")
    assert "INCOMPLETE REPORT" in text                    # flagged
    assert fail_on in text                                # names the failed section
    assert "## Summary" in text                           # earlier sections salvaged
    assert mds[0].with_suffix(".docx").exists()           # partial .docx too


def test_generate_docx_failure_does_not_break_md(tmp_path, monkeypatch) -> None:
    """If the independent .docx step throws, the .md is still written, the
    return value is the .md, and no exception escapes (decoupled outputs)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)
    monkeypatch.setattr(
        TiaReportGenerator, "_call_rag_chat",
        lambda self, m, section=None, read_timeout=None: f"## {section}\nbody",
    )

    import docx_writer

    def boom(*a, **k):
        raise RuntimeError("docx exploded")

    monkeypatch.setattr(docx_writer, "write_docx", boom)

    out = gen.generate(src, filename_prefix="TIA_test")
    assert out.suffix == ".md" and out.exists()              # md still written
    assert not out.with_suffix(".docx").exists()             # docx skipped, no crash


def test_generate_injects_verified_analysis_into_every_section(tmp_path, monkeypatch) -> None:
    """The phase-1 draft is audited by the verification pass, and it is the
    VERIFIED analysis (not the raw draft) that is injected into each section as
    the authoritative source of truth."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = _make_gen(tmp_path)

    section_prompts: dict[str, str] = {}

    def fake_call(self, user_message, section=None, read_timeout=None):
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

    def fake_call(self, user_message, section=None, read_timeout=None):
        return f"```markdown\n## {section}\nbody\n```"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    out = gen.generate(src, filename_prefix="TIA_test")
    text = out.read_text(encoding="utf-8")
    assert "```markdown" not in text


# ---------- assembly / directive / fence helpers ----------

def test_section_directive_names_the_section() -> None:
    d = TiaReportGenerator._section_directive("Security", "cover encryption")
    assert '"Security"' in d
    assert "cover encryption" in d                        # hint carried through
    assert "authoritative analysis" in d                  # anchoring instruction


# ---------- ideal-report structure, criticality scale, version neutrality ----------

def test_report_sections_match_lean_skeleton() -> None:
    """Lean 4-part structure: Summary, Key Findings, the 7 Detailed Assessment
    categories, then Outstanding Questions (Introduction + Assessment categories
    dropped)."""
    titles = [t for t, _ in TiaReportGenerator.REPORT_SECTIONS]
    assert titles == [
        "Summary",
        "Key Findings",
        "General Information",
        "SQL Server",
        "Application Server(s)",
        "Interactive Clients",
        "Runtime Resources (Robots)",
        "Disaster Recovery",
        "Security",
        "Outstanding Questions",
    ]
    assert "Introduction" not in titles
    assert "Assessment categories" not in titles


def test_system_prompt_uses_assessment_category_scale() -> None:
    """The four ideal-report criticality labels replace Critical/High/Medium/Low."""
    for label in ("Red Flag", "Strong Recommendation", "Recommendation", "Suggestion"):
        assert label in TIA_SYSTEM_PROMPT
    assert "- Critical:" not in TIA_SYSTEM_PROMPT
    assert "Medium" not in TIA_SYSTEM_PROMPT


def test_system_prompt_version_neutrality_and_brevity() -> None:
    """Reference-document versions are banned, upgrade advice is unversioned,
    the brevity style block is present, and no release line is name-dropped."""
    assert "NEVER name or imply the version of any reference document" in TIA_SYSTEM_PROMPT
    assert "without citing any version number" in TIA_SYSTEM_PROMPT
    assert "may be quoted as a fact" in TIA_SYSTEM_PROMPT      # customer's own version
    assert "be concise" in TIA_SYSTEM_PROMPT
    assert "7.x" not in TIA_SYSTEM_PROMPT


# ---------- code-rendered Detailed Assessment (from the ledger) ----------

_SAMPLE_ROWS = [
    ("General Information", "Booking ID", "", "id1234", "—", ""),
    ("SQL Server", "SQL connection encrypted",
     "Are the connections secured?", "No", "Red Flag", "Encrypt it | now."),
    ("SQL Server", "Database size and growth", "How big is it?", "1111 GB", "—", ""),
    ("Security", "Antivirus", "Is AV installed?", "No antivirus",
     "Strong Recommendation", "Deploy AV."),
]


def test_parse_ledger_reads_rows() -> None:
    rows = TiaReportGenerator._parse_ledger(_ledger_analysis(_SAMPLE_ROWS))
    assert len(rows) == 4
    sql = rows[1]
    assert sql["category"] == "SQL Server"
    assert sql["question"] == "Are the connections secured?"
    assert sql["criticality"] == "Red Flag"
    assert sql["detail"] == "Encrypt it | now."          # pipe in Detail preserved
    # An unflagged row: em-dash criticality → None, blank Question falls back later.
    assert rows[0]["criticality"] is None
    assert rows[0]["answer"] == "id1234"


def test_parse_ledger_missing_section_returns_empty() -> None:
    assert TiaReportGenerator._parse_ledger("## Environment Facts\nno ledger") == []


def test_parse_ledger_normalises_dash_placeholders() -> None:
    """A draft that puts '—' in the Question column for an admin field must not
    become a '—' heading — Question normalises to blank so the heading falls
    back to the Subject."""
    analysis = _ledger_analysis([
        ("General Information", "Submission time", "—", "2026-07-04", "—", "—"),
    ])
    rows = TiaReportGenerator._parse_ledger(analysis)
    assert rows[0]["question"] == ""                      # '—' → blank
    gi = TiaReportGenerator._render_category("General Information", rows, first=False)
    assert "**1. Submission time**" in gi                 # falls back to subject
    assert "**1. —**" not in gi


def test_render_category_one_block_per_row() -> None:
    """Full question in the heading; unflagged rows carry no Recommendation;
    blank question falls back to the subject; first category opens the parent."""
    rows = TiaReportGenerator._parse_ledger(_ledger_analysis(_SAMPLE_ROWS))
    gi = TiaReportGenerator._render_category("General Information", rows, first=True)
    assert gi.startswith("## Detailed Assessment")
    assert "### General Information" in gi
    assert "**1. Booking ID**" in gi                      # blank question → subject
    assert "Answer: id1234" in gi
    assert "Recommendation:" not in gi                    # unflagged → no rec

    sql = TiaReportGenerator._render_category("SQL Server", rows, first=False)
    assert not sql.startswith("## Detailed Assessment")   # only the first opens it
    assert "**1. Are the connections secured? — Red Flag**" in sql
    assert "Recommendation: Encrypt it | now." in sql
    assert "**2. How big is it?**" in sql                 # numbering restarts, unflagged

    dr = TiaReportGenerator._render_category("Disaster Recovery", rows, first=False)
    assert "No questions in this category." in dr


def test_render_category_block_count_equals_rows() -> None:
    """Exactly one block per ledger row — the coverage guarantee."""
    rows = TiaReportGenerator._parse_ledger(_ledger_analysis(_SAMPLE_ROWS))
    sql = TiaReportGenerator._render_category("SQL Server", rows, first=False)
    assert len(re.findall(r"^\*\*\d+\.", sql, flags=re.M)) == 2  # 2 SQL rows → 2 blocks


def test_analysis_directive_three_part_ledger() -> None:
    d = TiaReportGenerator._analysis_directive()
    assert "## Environment Facts" in d
    assert "## Assessment Ledger" in d
    assert "## Positive Confirmations" in d
    assert "ID | Category | Subject | Question | Answer | Criticality | Detail" in d
    assert "EVERY question" in d                          # exhaustive coverage
    assert "never flagged" in d                           # admin fields rule
    assert "General Information" in d                     # renamed category (no clash)
    assert "backup and recovery questions" in d           # category mapping anchor
    assert "'not provided'" in d                          # blank answers -> no finding
    assert "'Don't know'" in d                            # uncertain answers may be flagged
    assert "NOT repeated in Outstanding Questions" in d   # no double-counting
    assert "never merge two keys" in d                    # no fabricated/merged rows
    assert "FULL question text from the matched REFERENCE SCORING GUIDANCE" in d
    assert "Red Flag first" in d
    assert "EXHAUSTIVE and FINAL" in d                    # sections can't add rows
    assert "## Criticality Tally" in d                    # counts are copied, not derived


def test_key_findings_hint_carries_criticality() -> None:
    """Flagged Key Findings show their criticality; positives are ✓-prefixed."""
    hints = dict(TiaReportGenerator.REPORT_SECTIONS)
    h = hints["Key Findings"]
    assert "## Key Findings" in h
    assert "`**<Category> – <Subject> — <Criticality>**`" in h
    assert "✓" in h
    assert "carry no criticality" in h


def test_count_criticalities_tallies_block_headings() -> None:
    """Criticalities are counted from numbered Q&A block headings; 'Strong
    Recommendation' is never miscounted as 'Recommendation'."""
    sql = (
        "### SQL Server\n"
        "**1. Are connections secured? — Strong Recommendation**\nAnswer: No\n\n"
        "**2. Index maintenance? — Red Flag**\nAnswer: None\n\n"
        "**3. Database size and growth**\nAnswer: 1111 GB\n\n"       # unflagged
        "**4. Backup method? — Recommendation**\nAnswer: Full\n"
    )
    sec = "### Security\n**1. Antivirus? — Red Flag**\nAnswer: No\n"
    counts = TiaReportGenerator._count_criticalities([sql, sec])
    assert counts == {
        "Red Flag": 2, "Strong Recommendation": 1,
        "Recommendation": 1, "Suggestion": 0,
    }


def test_reconcile_summary_counts_overwrites_wrong_table() -> None:
    """The Summary count table is rewritten from the rendered block
    criticalities, correcting any drift from the LLM's own tally."""
    gen = _make_gen(Path("/x"))  # output dir unused here
    titles = [t for t, _ in TiaReportGenerator.REPORT_SECTIONS]
    section_texts = [""] * len(titles)
    # Summary with a WRONG table (says 5 Red Flags).
    section_texts[titles.index("Summary")] = (
        "## Summary\nSubmitted today.\n\n"
        "| Criticality | Number of instances |\n|---|---|\n"
        "| Red Flag | 5 |\n| Strong Recommendation | 9 |\n"
        "| Recommendation | 9 |\n| Suggestion | 9 |\n"
    )
    # Actual blocks: 2 Red Flag, 1 Strong Recommendation.
    section_texts[titles.index("SQL Server")] = (
        "### SQL Server\n**1. Q? — Red Flag**\nAnswer: a\n\n"
        "**2. Q2? — Strong Recommendation**\nAnswer: b\n"
    )
    section_texts[titles.index("Security")] = (
        "### Security\n**1. Q3? — Red Flag**\nAnswer: c\n"
    )
    gen._reconcile_summary_counts(section_texts)
    summary = section_texts[titles.index("Summary")]
    assert "| Red Flag | 2 |" in summary
    assert "| Strong Recommendation | 1 |" in summary
    assert "| Recommendation | 0 |" in summary
    assert "| Suggestion | 0 |" in summary
    assert "| Red Flag | 5 |" not in summary                 # old drift gone


def test_generate_count_table_matches_rendered_blocks(tmp_path, monkeypatch) -> None:
    """End-to-end: the written report's Summary count table equals the
    criticalities in the Detailed Assessment blocks, regardless of what the
    LLM put in its own table."""
    import re as _re
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text('{"q1": "v", "q2": "v"}', encoding="utf-8")
    gen = _make_gen(tmp_path)

    rows = [
        ("SQL Server", "s1", "Q1?", "No", "Red Flag", "d"),
        ("SQL Server", "s2", "Q2?", "Shared", "Strong Recommendation", "d"),
        ("Security", "s3", "Q3?", "No", "Red Flag", "d"),
    ]

    def fake_call(self, user_message, section=None, read_timeout=None):
        if section in (TiaReportGenerator.ANALYSIS_LABEL,
                       TiaReportGenerator.VERIFICATION_LABEL):
            return _ledger_analysis(rows)
        if section == "Summary":
            # A deliberately WRONG table (all 9s) — code must overwrite it.
            return ("## Summary\nReport for org.\n\n"
                    "| Criticality | Number of instances |\n|---|---|\n"
                    "| Red Flag | 9 |\n| Strong Recommendation | 9 |\n"
                    "| Recommendation | 9 |\n| Suggestion | 9 |")
        return f"## {section}\nbody"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    out = gen.generate(src, filename_prefix="TIA_test")
    text = out.read_text(encoding="utf-8")
    # Ledger has 2 Red Flag + 1 Strong Recommendation → table must say exactly that.
    assert "| Red Flag | 2 |" in text
    assert "| Strong Recommendation | 1 |" in text
    assert "| Recommendation | 0 |" in text
    assert "| Suggestion | 0 |" in text
    assert "| Red Flag | 9 |" not in text


def test_summary_hint_copies_tally_and_has_no_definitions() -> None:
    """The lean Summary section holds the intro + count table (numbers copied
    verbatim from the analysis tally), with no criticality definitions."""
    hints = dict(TiaReportGenerator.REPORT_SECTIONS)
    h = hints["Summary"]
    assert "## Summary" in h
    assert "Criticality | Number of instances" in h
    assert "Criticality Tally" in h
    assert "VERBATIM" in h
    assert "No criticality definitions" in h


def test_system_prompt_advisory_tone() -> None:
    """Recommendations must be evidence-based and advisory, never commanding:
    urgency adverbs are banned (the criticality label carries the urgency), and
    the fact → consequence → suggestion structure is required."""
    assert "never commanding" in TIA_SYSTEM_PROMPT
    assert '"immediately"' in TIA_SYSTEM_PROMPT           # in the ban list
    assert '"without delay"' in TIA_SYSTEM_PROMPT
    assert "consequence of delay" in TIA_SYSTEM_PROMPT    # reason instead of adverb
    assert '"consider"' in TIA_SYSTEM_PROMPT              # measured verbs
    assert "observed fact" in TIA_SYSTEM_PROMPT           # evidence-based structure


def test_system_prompt_rubric_grounding_rules() -> None:
    """The reference scoring guidance is the authoritative rubric: levels come
    from it, good-rated answers are never flagged, and uncovered questions may
    not be assessed from general knowledge."""
    assert "REFERENCE SCORING GUIDANCE" in TIA_SYSTEM_PROMPT
    assert "AUTHORITATIVE rubric" in TIA_SYSTEM_PROMPT
    normalized = " ".join(TIA_SYSTEM_PROMPT.split())
    assert "must NOT be flagged or recommended against" in normalized
    assert "never from general" in TIA_SYSTEM_PROMPT
    assert "knowledge alone" in TIA_SYSTEM_PROMPT
    assert "leave the row unflagged" in TIA_SYSTEM_PROMPT


def test_verification_directive_audits_against_guidance() -> None:
    d = TiaReportGenerator._verification_directive("draft text")
    assert "REFERENCE SCORING GUIDANCE" in d
    assert "REMOVE the flag" in d
    assert "align the Detail's direction" in d
    assert "each Subject is a customer data key copied verbatim" in d


def test_extraction_prompt_preserves_full_question() -> None:
    """The reference extraction must keep each item's full question text so the
    report can show the complete question, not just the short label."""
    from reference_sheet_extractor import EXTRACT_SYSTEM_PROMPT
    assert '"question" field' in EXTRACT_SYSTEM_PROMPT
    assert "VERBATIM" in EXTRACT_SYSTEM_PROMPT


def test_outstanding_questions_hint_is_grounded() -> None:
    """Outstanding Questions lists only real blank/ambiguous form answers, never
    invented reference topics or repeated findings."""
    hints = dict(TiaReportGenerator.REPORT_SECTIONS)
    h = hints["Outstanding Questions"]
    assert "blank/missing or explicitly" in h
    assert "do NOT already carry a criticality" in h
    assert "topics the form never asked" in h


def test_output_forbids_naming_the_rubric() -> None:
    assert "REFERENCE SCORING GUIDANCE is INTERNAL" in TIA_SYSTEM_PROMPT
    assert "the guidance rates this as" in TIA_SYSTEM_PROMPT   # banned phrase example


def test_warn_guidance_leaks(caplog) -> None:
    import logging
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_guidance_leaks(
            "The connection mode is fine. The guidance rates this as a Suggestion."
        )
    assert any("internal scoring rubric" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_guidance_leaks(
            "The SQL connection is unencrypted; enabling TLS removes that exposure."
        )
    assert not caplog.records


def test_warn_coverage_block_count_and_duplicates(caplog) -> None:
    import logging
    # 3 distinct blocks, expecting 3 → silent.
    ok = (
        "## Detailed Assessment\n### SQL Server\n"
        "**1. Are connections secured? — Red Flag**\nAnswer: No\n\n"
        "**2. Is Unicode logging enabled?**\nAnswer: No\n\n"
        "### Security\n**1. Is antivirus installed? — Red Flag**\nAnswer: No\n\n"
        "## Outstanding Questions\n- Network latency not provided.\n"
    )
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_coverage(ok, expected_count=3)
    assert not caplog.records

    # 2 blocks but 3 expected → count-mismatch warning (a question was dropped).
    caplog.clear()
    short = (
        "## Detailed Assessment\n### SQL Server\n"
        "**1. Q one? — Red Flag**\nAnswer: No\n\n"
        "**2. Q two?**\nAnswer: No\n\n## Outstanding Questions\n"
    )
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_coverage(short, expected_count=3)
    assert any("block(s) rendered but 3" in r.message for r in caplog.records)

    # Duplicate heading → duplicate warning.
    caplog.clear()
    dup = (
        "## Detailed Assessment\n### SQL Server\n"
        "**1. Q one? — Red Flag**\nAnswer: No\n\n"
        "**2. Q one? — Red Flag**\nAnswer: No\n\n## Outstanding Questions\n"
    )
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_coverage(dup, expected_count=2)
    assert any("duplicate assessment heading" in r.message for r in caplog.records)


# ---------- reference guidance loading / injection ----------

def _make_guidance_dir(tmp_path: Path) -> Path:
    d = tmp_path / "refjson"
    d.mkdir()
    (d / "extracted_Notes_20260101_000000.json").write_text(
        '{"note": "misc"}', encoding="utf-8")
    (d / "extracted_SQL_Server_20260101_000000.json").write_text(
        '{"unicode_logging": {"good": "disabled"}}', encoding="utf-8")
    return d


def test_load_reference_guidance_prioritizes_rubric_files(tmp_path: Path) -> None:
    gen = TiaReportGenerator(
        base_url="https://example.invalid", api_key="k", llm_model="m",
        output_dir=tmp_path / "out",
        reference_guidance_dir=_make_guidance_dir(tmp_path),
    )
    guidance = gen._load_reference_guidance()
    assert guidance is not None
    assert "unicode_logging" in guidance
    # Rubric-dense SQL_Server file is injected before the Notes file.
    assert guidance.index("extracted_SQL_Server") < guidance.index("extracted_Notes")


def test_load_reference_guidance_missing_or_empty_dir(tmp_path: Path) -> None:
    gen = TiaReportGenerator(
        base_url="https://example.invalid", api_key="k", llm_model="m",
        output_dir=tmp_path / "out",
    )
    assert gen._load_reference_guidance() is None      # dir not configured
    gen.reference_guidance_dir = tmp_path / "nope"
    assert gen._load_reference_guidance() is None      # dir missing -> None, no crash


def test_load_reference_guidance_size_cap(tmp_path: Path, monkeypatch) -> None:
    gen = TiaReportGenerator(
        base_url="https://example.invalid", api_key="k", llm_model="m",
        output_dir=tmp_path / "out",
        reference_guidance_dir=_make_guidance_dir(tmp_path),
    )
    monkeypatch.setattr(TiaReportGenerator, "GUIDANCE_MAX_CHARS", 5)
    assert gen._load_reference_guidance() is None      # nothing fits -> None


def test_generate_injects_guidance_into_analysis_and_verification_only(
    tmp_path, monkeypatch,
) -> None:
    """The rubric block reaches exactly the two calls that author/audit the
    criticalities; the narrative section calls don't get it (and the
    code-rendered category sections make no call at all)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text("{}", encoding="utf-8")
    gen = TiaReportGenerator(
        base_url="https://example.invalid", api_key="k", llm_model="m",
        output_dir=tmp_path / "out",
        reference_guidance_dir=_make_guidance_dir(tmp_path),
    )

    messages: dict[str, str] = {}

    def fake_call(self, user_message, section=None, read_timeout=None):
        messages[section] = user_message
        return f"## {section}\nbody"

    monkeypatch.setattr(TiaReportGenerator, "_call_rag_chat", fake_call)
    gen.generate(src, filename_prefix="TIA_test")

    marker = "REFERENCE SCORING GUIDANCE"
    assert marker in messages[TiaReportGenerator.ANALYSIS_LABEL]
    assert marker in messages[TiaReportGenerator.VERIFICATION_LABEL]
    # Only the narrative LLM sections make a call; none carry the guidance.
    assert set(messages) - {TiaReportGenerator.ANALYSIS_LABEL,
                            TiaReportGenerator.VERIFICATION_LABEL} == {
        "Summary", "Key Findings", "Outstanding Questions"}
    for title in ("Summary", "Key Findings", "Outstanding Questions"):
        assert marker not in messages[title], f"guidance leaked into '{title}'"


def test_system_prompt_keeps_compliant_items_unflagged() -> None:
    """All questions appear in the report, but compliant answers carry no
    criticality and no recommendation."""
    assert "NO criticality and NO recommendation line" in TIA_SYSTEM_PROMPT
    assert "never manufacture a finding" in TIA_SYSTEM_PROMPT


def test_warn_version_leaks_fires_on_versioned_guide(caplog) -> None:
    """A version number near 'guide' triggers the log-only guardrail; the
    customer's bare version does not."""
    import logging
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_version_leaks(
            "Per the Blue Prism 7.5 installation guide, configure SPNs."
        )
    assert any("reference document version" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_version_leaks(
            "The installed version is 7.4.1. The environment has 88 Runtime "
            "Resources.\nThe installation guide recommends virtualisation."
        )
    assert not caplog.records


def test_warn_ledger_id_leaks_fires_on_internal_ids(caplog) -> None:
    """Internal ledger row IDs (R1, R2, ...) must never reach the report; a
    leak is surfaced as a WARNING. Ordinary prose does not trigger it."""
    import logging
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_ledger_id_leaks(
            "1. Shared SQL instance *(Strong Recommendation — R11, R32)*"
        )
    rec = [r for r in caplog.records if "ledger ID" in r.message]
    assert rec and "R11" in rec[0].message and "R32" in rec[0].message

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        TiaReportGenerator._warn_ledger_id_leaks(
            "The environment has 88 Runtime Resources across 2 Application Servers."
        )
    assert not caplog.records


def test_prompt_marks_ledger_ids_internal() -> None:
    """The system prompt forbids ledger IDs and reasoning dumps in output."""
    assert "INTERNAL references" in TIA_SYSTEM_PROMPT
    assert "NEVER write a ledger ID" in TIA_SYSTEM_PROMPT
    assert "no self-correction" in TIA_SYSTEM_PROMPT
    assert "never restart or repeat the section" in TIA_SYSTEM_PROMPT


def test_assemble_report_has_title_and_rules() -> None:
    out = TiaReportGenerator._assemble_report(["## A\nx", "## B\ny"])
    assert out.startswith("# Technical Infrastructure Assessment")
    assert "## A" in out and "## B" in out
    assert "\n---\n" in out


def test_strip_restarted_draft_keeps_final_rendering() -> None:
    """A section the model self-corrected (draft, reasoning dump with leaked
    ledger IDs, then a clean rewrite) is repaired to the final rendering."""
    text = (
        "### Runtime Resources (Robots)\n\n"
        "**1. Runtime Resource count**\nAnswer: 88\n\n"
        "Wait — I must follow the ledger exactly. Let me re-read the rows:\n"
        "- R29: Runtime Resources hosting — no criticality\n"
        "- R32: Antivirus — Strong Recommendation\n"
        "That is 2 rows. Let me rewrite correctly.\n\n"
        "### Runtime Resources (Robots)\n\n"
        "**1. Runtime Resources hosting**\nAnswer: Virtual servers\n"
    )
    out = TiaReportGenerator._strip_restarted_draft(text, section="Runtime Resources (Robots)")
    assert out.count("### Runtime Resources (Robots)") == 1   # one heading
    assert "Wait" not in out and "R29" not in out             # draft + IDs gone
    assert "Runtime Resources hosting" in out                 # final rendering kept
    assert "Runtime Resource count" not in out                # draft dropped


def test_strip_restarted_draft_noop_on_clean_section() -> None:
    """A well-formed section (single heading, incl. multi-subheading ones) is
    returned unchanged."""
    clean = "## Assessment Overview\n\n### Date of Assessment\n\nSubmitted 4 July.\n\n### Report Summary\n\ntable"
    assert TiaReportGenerator._strip_restarted_draft(clean) == clean


def test_strip_code_fence_unwraps_and_leaves_plain() -> None:
    fenced = "```markdown\n## Title\nbody\n```"
    assert TiaReportGenerator._strip_code_fence(fenced) == "## Title\nbody"
    plain = "## Title\nbody"
    assert TiaReportGenerator._strip_code_fence(plain) == plain
