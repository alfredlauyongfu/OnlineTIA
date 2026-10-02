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


def _customer_json(answers: dict[str, str] | None = None) -> str:
    """A form export in the Power Automate flow's shape: submission metadata as a
    plain value, plus one {"question", "answer"} object per answered field."""
    answers = {"SQL connection encrypted": "No"} if answers is None else answers
    payload: dict = {"Submission time": "2026-07-04T20:22:06Z"}
    for key, answer in answers.items():
        payload[key] = {"question": f"{key}? (full form wording)", "answer": answer}
    return json.dumps(payload)


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


# ---------- form-export shape: questions, cover meta, old-format rejection ----------

def _export(fields: dict) -> dict[str, dict]:
    """customer_content as _read_customer_content returns it, for one file."""
    return {"a.json": fields}


def test_extract_fields_maps_key_to_question_and_answer() -> None:
    fields = TiaReportGenerator._extract_fields(_export({
        "Submission time": "2026-07-04T20:22:06Z",
        "Database hosting": {"question": "What infrastructure hosts your Blue Prism "
                                         "database?", "answer": "Physical server"},
        "Antivirus": {"question": "Is antivirus enabled?", "answer": ""},
    }))
    # Metadata (a plain value) is not a question and gets no entry.
    assert fields == {
        "Database hosting": {
            "question": "What infrastructure hosts your Blue Prism database?",
            "answer": "Physical server"},
        "Antivirus": {"question": "Is antivirus enabled?", "answer": ""},
    }


def test_extract_fields_strips_stray_section_headers() -> None:
    """Two of the form's section headers were typed into a question title and an
    answer option; they must not reach customer-facing report text."""
    fields = TiaReportGenerator._extract_fields(_export({
        "Process and Object count": {
            "question": "How many Processes and Objects do you have in total? "
                        "(Run the report.) Section: Database & SQL Server",
            "answer": "1,193"},
        "Encryption key storage": {
            "question": "Where are your encryption-scheme keys stored?",
            "answer": "Don't know\nSection: Logging & Data Management"},
    }))
    assert fields["Process and Object count"]["question"] == (
        "How many Processes and Objects do you have in total? (Run the report.)")
    assert fields["Encryption key storage"]["answer"] == "Don't know"


def test_extract_fields_leaves_normal_content_untouched() -> None:
    """The stripper stops at '?' so it can never truncate real question wording."""
    fields = TiaReportGenerator._extract_fields(_export({
        "Q": {"question": "Which Section: A or B do you use? (Pick one.)",
              "answer": "Section: A"},
    }))
    assert fields["Q"]["question"] == "Which Section: A or B do you use? (Pick one.)"
    assert fields["Q"]["answer"] == "Section: A"


def test_extract_fields_ignores_flat_old_format() -> None:
    """A pre-flow-update export yields no questions — which is what makes
    generate() reject it rather than silently assess nothing."""
    assert TiaReportGenerator._extract_fields(
        _export({"Database hosting": "Physical server"})) == {}


def test_backfill_adds_rows_for_questions_the_ledger_dropped() -> None:
    """Coverage is code-enforced: a question the model omitted still gets a row,
    built from the customer's own data and left unflagged."""
    rows = [{"category": "SQL Server", "subject": "Database hosting",
             "question": "q", "answer": "Physical server",
             "criticality": "Red Flag", "detail": "d"}]
    fields = {
        "Database hosting": {"question": "q", "answer": "Physical server"},
        "Login Agent": {"question": "Do you use Login Agent?", "answer": "Yes"},
        "Monitoring": {"question": "Do you monitor?", "answer": ""},
    }
    out = TiaReportGenerator._backfill_missing_rows(rows, fields)
    assert len(out) == 3
    added = {r["subject"]: r for r in out if r["subject"] != "Database hosting"}
    assert added["Login Agent"]["answer"] == "Yes"
    assert added["Login Agent"]["criticality"] is None
    assert added["Monitoring"]["answer"] == "not provided"   # blank -> placeholder
    # The row the model did produce is untouched.
    assert out[0]["criticality"] == "Red Flag"


def test_parse_ledger_strips_internal_id_citations() -> None:
    """Ledger IDs are internal; a Detail citing them would print "(R14)" to the
    customer. Only the parenthesised citation is removed."""
    analysis = _ledger_analysis([
        ("SQL Server", "Anything else", "q", "slow", "Red Flag",
         "No index maintenance (R14), no archiving (R19, R23) are to blame."),
    ])
    detail = TiaReportGenerator._parse_ledger(analysis)[0]["detail"]
    assert detail == "No index maintenance, no archiving are to blame."


def test_render_category_omits_a_category_with_no_questions() -> None:
    """An empty category is dropped rather than rendered as a stub section."""
    assert TiaReportGenerator._render_category("Disaster Recovery", [], first=False) == ""


def test_assemble_report_drops_empty_sections_without_stray_rules() -> None:
    """An omitted category must not leave a dangling `---` separator behind."""
    md = TiaReportGenerator._assemble_report(["## Summary\nx", "", "## Security\ny"])
    assert "## Summary" in md and "## Security" in md
    assert "---\n\n---" not in md
    assert md.count("\n---\n") == 2      # title rule + one between the two sections


def test_normalise_categories_repairs_a_near_miss_name() -> None:
    """"Runtime Resources" must land in "Runtime Resources (Robots)" — an exact
    match is required to render, so a near-miss would drop the question."""
    rows = [{"category": "Runtime Resources", "subject": "s", "question": "q",
             "answer": "a", "criticality": None, "detail": ""}]
    assert (TiaReportGenerator._normalise_categories(rows)[0]["category"]
            == "Runtime Resources (Robots)")


@pytest.mark.parametrize("raw, expected", [
    ("SQL Server", "SQL Server"),                       # exact
    ("sql server", "SQL Server"),                       # case-insensitive
    ("Application Server", "Application Server(s)"),    # shortened
    ("Runtime Resources (Robots) — extra", "Runtime Resources (Robots)"),
    ("Totally Unrelated", "General Information"),       # last resort
    ("", "General Information"),                        # blank
])
def test_normalise_categories_matching(raw: str, expected: str) -> None:
    rows = [{"category": raw, "subject": "s", "question": "q", "answer": "a",
             "criticality": None, "detail": ""}]
    assert TiaReportGenerator._normalise_categories(rows)[0]["category"] == expected


def test_normalise_categories_never_drops_a_row() -> None:
    """Whatever the model wrote, every row survives into a rendered category."""
    rows = [{"category": c, "subject": f"s{i}", "question": "q", "answer": "a",
             "criticality": None, "detail": ""}
            for i, c in enumerate(["Runtime Resources", "Nonsense", "Security"])]
    out = TiaReportGenerator._normalise_categories(rows)
    assert len(out) == 3
    assert all(r["category"] in TiaReportGenerator._DETAILED_CATEGORIES for r in out)


def test_backfill_is_a_noop_when_every_question_has_a_row() -> None:
    rows = [{"category": "SQL Server", "subject": "Database hosting",
             "question": "q", "answer": "a", "criticality": None, "detail": ""}]
    fields = {"Database hosting": {"question": "q", "answer": "a"}}
    assert TiaReportGenerator._backfill_missing_rows(rows, fields) == rows


def test_extract_cover_meta_reads_nested_answers() -> None:
    org, date = TiaReportGenerator._extract_cover_meta(_export({
        "Submission time": "2026-07-04T20:22:06Z",
        "Organisation": {"question": "Your organisation / business unit.",
                         "answer": "Acme Corporation"},
    }))
    assert org == "Acme Corporation"
    assert date == "04 July 2026"


def test_extract_cover_meta_falls_back_when_absent() -> None:
    org, date = TiaReportGenerator._extract_cover_meta(_export({"Q": {"answer": "x"}}))
    assert org == "Customer"
    assert date  # today's date, format-checked elsewhere


def test_generate_rejects_old_flat_export(tmp_path: Path) -> None:
    """Clean break: a pre-flow-update export fails loudly, naming the cause,
    instead of being assessed with no questions."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(json.dumps({"Booking ID": "id1234",
                                            "Database hosting": "Physical server"}),
                                encoding="utf-8")
    with pytest.raises(TiaGenerationError) as exc:
        _make_gen(tmp_path).generate(src, filename_prefix="TIA_test")
    assert "predates the Power Automate flow update" in str(exc.value)


def test_render_category_heading_prefers_form_wording() -> None:
    """The heading comes from the questions map (keyed by the row's Subject),
    falling back to the ledger's Question, then the Subject itself."""
    rows = [
        {"category": "SQL Server", "subject": "Database hosting",
         "question": "model's paraphrase", "answer": "Physical server",
         "criticality": None, "detail": ""},
        {"category": "SQL Server", "subject": "Unmapped key",
         "question": "ledger question", "answer": "x",
         "criticality": None, "detail": ""},
    ]
    out = TiaReportGenerator._render_category(
        "SQL Server", rows, first=False,
        questions={"Database hosting": "What infrastructure hosts your database?"},
    )
    assert "**1. What infrastructure hosts your database?**" in out
    assert "model's paraphrase" not in out
    # No mapping for this key -> the ledger's Question is used.
    assert "**2. ledger question**" in out


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

def test_build_output_path_uses_the_stem_verbatim(tmp_path: Path) -> None:
    """run.py's report_prefix already identifies the submission, so no timestamp
    is appended — a re-run replaces that submission's previous report."""
    gen = _make_gen(tmp_path)
    out = gen._build_output_path("TIA_Acme_Bank_Production_2026-09-16_BK123456")
    assert out.parent == gen.output_dir
    assert out.name == "TIA_Acme_Bank_Production_2026-09-16_BK123456.md"
    # Calling twice yields the same path, so the report is replaced not duplicated.
    assert gen._build_output_path("TIA_Acme") == gen._build_output_path("TIA_Acme")


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
    (src / "a.json").write_text(_customer_json({"SQL connection encrypted": "No"}), encoding="utf-8")
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

    # Only analysis, verification and 2 narrative sections are LLM calls — the 7
    # Detailed Assessment categories AND Key Findings are code-rendered.
    assert seen_sections == [
        TiaReportGenerator.ANALYSIS_LABEL, TiaReportGenerator.VERIFICATION_LABEL,
        "Summary", "Outstanding Questions",
    ]
    assert text.startswith("# Technical Infrastructure Assessment")
    for h in ("## Summary", "## Key Findings", "## Detailed Assessment",
              "## Outstanding Questions"):
        assert h in text
    # The code-rendered category block came from the ledger.
    assert "### SQL Server" in text
    # The heading is the form's own wording (from the export), NOT the ledger's
    # Question column — the ledger said "Are the connections secured?".
    assert "SQL connection encrypted? (full form wording) — Red Flag" in text
    assert "Are the connections secured?" not in text
    assert f"## {TiaReportGenerator.ANALYSIS_LABEL}" not in text


def test_generate_also_writes_sibling_docx(tmp_path, monkeypatch) -> None:
    """generate() returns the .md path but also writes a sibling .docx with the
    same stem (independent, best-effort Word output)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
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
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
    gen = _make_gen(tmp_path)

    fail_on = "Outstanding Questions"   # an LLM section (Key Findings and the categories are code-rendered)

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

    mds = list((tmp_path / "out").glob("TIA_test*.md"))
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
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
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
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
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
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
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

    # A category with no ledger rows is omitted entirely, not stubbed out.
    dr = TiaReportGenerator._render_category("Disaster Recovery", rows, first=False)
    assert dr == ""


def test_render_category_block_count_equals_rows() -> None:
    """Exactly one block per ledger row — the coverage guarantee."""
    rows = TiaReportGenerator._parse_ledger(_ledger_analysis(_SAMPLE_ROWS))
    sql = TiaReportGenerator._render_category("SQL Server", rows, first=False)
    assert len(re.findall(r"^\*\*\d+\.", sql, flags=re.M)) == 2  # 2 SQL rows → 2 blocks


def test_analysis_directive_three_part_ledger() -> None:
    d = TiaReportGenerator._analysis_directive()
    assert "## Environment Facts" in d
    assert "## Assessment Ledger" in d
    # Positives were dropped: nothing consumes them, and a findings report should
    # not spend analysis tokens on configurations that need no action.
    assert "Positive Confirmations" not in d
    assert "ID | Category | Subject | Question | Answer | Criticality | Detail" in d
    assert "EVERY question" in d                          # exhaustive coverage
    assert "never flagged" in d                           # admin fields rule
    assert "General Information" in d                     # renamed category (no clash)
    assert "backup and recovery questions" in d           # category mapping anchor
    assert "'not provided'" in d                          # blank answers -> no finding
    # "Don't know" is not a finding — the point could not be assessed.
    assert "'Don't know', 'unsure') is NOT a finding" in d
    # Unknowns are routed to Outstanding Questions rather than counted as findings.
    assert "lists them under Outstanding Questions" in d
    assert "never merge two keys" in d                    # no fabricated/merged rows
    # Question wording now comes from the customer data's own 'question' field
    # (carried by the form export), not from the reference scoring guidance.
    assert "key's own 'question' field from the customer data" in d
    assert "Red Flag first" in d
    assert "EXHAUSTIVE and FINAL" in d                    # sections can't add rows
    assert "## Criticality Tally" in d                    # counts are copied, not derived


def test_uncovered_questions_are_capped_at_suggestion() -> None:
    """A rating above Suggestion must be traceable to a rubric entry. Where the
    reference guidance has no entry, the model may only reach Suggestion — stated
    in the system prompt, at the point of assignment, and enforced by the audit."""
    assert "Criticality ceiling for uncovered questions" in TIA_SYSTEM_PROMPT
    assert "never Recommendation, Strong Recommendation or Red Flag" in TIA_SYSTEM_PROMPT
    # The ceiling lowers a level; it must not delete rubric-backed findings, nor
    # manufacture findings out of clean answers.
    assert "The ceiling only LOWERS a level" in TIA_SYSTEM_PROMPT
    assert "never removes a finding" in TIA_SYSTEM_PROMPT
    assert "never turns an unproblematic or administrative" in TIA_SYSTEM_PROMPT
    # Guidance is matched by subject matter — its wording differs from the form's.
    assert "Match guidance entries by SUBJECT MATTER, not wording" in TIA_SYSTEM_PROMPT

    directive = TiaReportGenerator._analysis_directive()
    assert "Criticality above Suggestion requires a" in directive

    audit = TiaReportGenerator._verification_directive("draft")
    assert "Enforce the Suggestion ceiling" in audit
    assert "downgrade a Criticality above Suggestion to Suggestion" in audit


def test_multi_step_remediations_must_be_reported_in_full() -> None:
    """A rubric entry whose fix has several required actions (e.g. Runtime
    Resource authentication needs a switch, a user role AND a setting) must reach
    the report whole — a partial fix sends the customer away half-remediated."""
    directive = TiaReportGenerator._analysis_directive()
    assert "remediation_steps" in directive
    assert "must name EVERY one of them" in directive
    assert "never list two of three required actions" in directive
    # Brevity rules must not silently truncate those steps.
    assert "Completeness of a fix outranks brevity" in TIA_SYSTEM_PROMPT


@pytest.mark.parametrize("answer, unknown", [
    ("Don't know", True), ("don't know", True), ("Dont know", True),
    ("Unknown", True), ("Unsure", True), ("N/A", True), ("", False),
    ("Yes", False), ("Physical server", False),
    # A multi-select that merely includes it is still a real answer.
    ("Virtual servers or desktops; Don't know", False),
])
def test_is_unknown_answer(answer: str, unknown: bool) -> None:
    assert TiaReportGenerator._is_unknown_answer(answer) is unknown


def test_unknown_answers_render_as_not_assessed_not_a_severity() -> None:
    """A customer who cannot answer must not look healthier than one who can:
    "Don't know" gets its own bucket instead of sitting at the bottom of the
    severity scale."""
    rows = [
        {"category": "Security", "subject": "Antivirus", "question": "AV?",
         "answer": "Don't know", "criticality": "Suggestion", "detail": "Confirm it."},
        {"category": "Security", "subject": "RR auth", "question": "Auth?",
         "answer": "No", "criticality": "Strong Recommendation", "detail": "Fix it."},
    ]
    out = TiaReportGenerator._render_category("Security", rows, first=False)
    assert "— Not assessed**" in out
    assert "Antivirus? — Suggestion" not in out
    assert "— Strong Recommendation**" in out        # real findings unaffected
    assert "Recommendation: Confirm it." in out      # the follow-up still shows


def test_unflag_unknown_rows_clears_severity_in_the_source_ledger() -> None:
    """Key Findings is written from the analysis TEXT, so the severity has to be
    cleared there — not just in the rendered blocks — or Key Findings calls a row
    "Suggestion" while the count table calls it "Not assessed"."""
    analysis = _ledger_analysis([
        ("Security", "Antivirus", "AV?", "Don't know", "Suggestion", "Confirm it."),
        ("Security", "RR auth", "Auth?", "No", "Strong Recommendation", "Fix it."),
    ])
    out = TiaReportGenerator._unflag_unknown_rows(analysis)
    rows = {r["subject"]: r for r in TiaReportGenerator._parse_ledger(out)}
    assert rows["Antivirus"]["criticality"] is None       # unknown -> unflagged
    assert rows["Antivirus"]["detail"] == "Confirm it."   # follow-up preserved
    assert rows["RR auth"]["criticality"] == "Strong Recommendation"


def test_unflag_unknown_rows_leaves_a_clean_ledger_untouched() -> None:
    analysis = _ledger_analysis([
        ("SQL Server", "Encryption", "Enc?", "No", "Red Flag", "Encrypt it."),
    ])
    assert TiaReportGenerator._unflag_unknown_rows(analysis) == analysis


def test_answer_coverage_line_reports_gaps(tmp_path: Path) -> None:
    gen = _make_gen(tmp_path)
    gen._fields = {
        "a": {"question": "a?", "answer": "Yes"},
        "b": {"question": "b?", "answer": "Don't know"},
        "c": {"question": "c?", "answer": "Don't know"},
        "d": {"question": "d?", "answer": ""},
    }
    line = gen._answer_coverage_line()
    assert "1 of 4 questions were answered" in line
    assert "2 answered" in line and "1 left blank" in line


def test_answer_coverage_line_when_everything_answered(tmp_path: Path) -> None:
    gen = _make_gen(tmp_path)
    gen._fields = {"a": {"question": "a?", "answer": "Yes"}}
    assert gen._answer_coverage_line() == "All 1 questions were answered.\n\n"


def test_key_findings_is_code_rendered_not_an_llm_call() -> None:
    """As an LLM section it kept contradicting the ledger it summarised."""
    from tia_generator import _CODE_RENDERED
    assert dict(TiaReportGenerator.REPORT_SECTIONS)["Key Findings"] == _CODE_RENDERED


def test_orphan_bullet_list_is_flagged(caplog) -> None:
    """A list introduced by a completed sentence is an orphan."""
    md = ("## Key Findings\n\nThis removes a layer of access control in "
          "production.\n\n- Add the /sso switch.\n- Untick anonymous.\n")
    with caplog.at_level("WARNING"):
        TiaReportGenerator._warn_orphan_bullet_lists(md)
    assert "no introducing stem line" in caplog.text


def test_bullets_after_a_stem_or_a_label_are_not_flagged(caplog) -> None:
    """A colon stem introduces the list; a heading or bold label is its own
    signpost (Outstanding Questions groups by category that way)."""
    md = ("## Key Findings\n\nThree changes are required:\n\n- Add the switch.\n"
          "\n## Outstanding Questions\n\n**SQL Server**\n- Retention not stated.\n"
          "\n### Security\n- Something else.\n")
    with caplog.at_level("WARNING"):
        TiaReportGenerator._warn_orphan_bullet_lists(md)
    assert "no introducing stem line" not in caplog.text


def test_free_text_catch_all_is_exempt_from_the_ceiling() -> None:
    """The "anything else / known issues" question can never have a rubric entry,
    yet it is where a customer reports a live problem. Capping it at Suggestion
    would soften real production instability, so it is exempt."""
    assert "EXEMPTION — open free-text questions" in TIA_SYSTEM_PROMPT
    assert "up to and including Red Flag" in TIA_SYSTEM_PROMPT
    assert "must not be softened to Suggestion" in TIA_SYSTEM_PROMPT
    # The audit pass must not undo the exemption.
    audit = TiaReportGenerator._verification_directive("draft")
    assert "does NOT\napply to open free-text questions" in audit.replace(
        "does NOT apply", "does NOT\napply")


def _row(cat, subj, crit, detail="d", answer="No"):
    return {"category": cat, "subject": subj, "question": f"{subj}?",
            "answer": answer, "criticality": crit, "detail": detail}


def test_answer_is_printed_verbatim_from_the_submission() -> None:
    """The ledger's Answer cell had been rewording the customer — abbreviating a
    job title, translating Spanish into English. A report sent back to that
    customer must show what they actually wrote."""
    rows = [{"category": "General Information", "subject": "Anything else",
             "question": "q", "answer": "Platform deployed on Azure, redundant "
                                        "Application Servers.",
             "criticality": None, "detail": ""}]
    fields = {"Anything else": {
        "question": "Anything else?",
        "answer": "Plataforma desplegada sobre Azure, con servidores de "
                  "aplicación redundados."}}
    out = TiaReportGenerator._render_category(
        "General Information", rows, first=False, fields=fields)
    assert "Answer: Plataforma desplegada sobre Azure" in out
    assert "Translation: Platform deployed on Azure" in out


@pytest.mark.parametrize("submitted, rendered, same", [
    # All four were printed as bogus "translations" in a real report.
    ("Álvaro Núñez Martín; Responsable del servicio",
     "Álvaro Núñez Martín / Responsable del servicio", True),
    ("1300", "1,300", True),
    ("Public cloud managed database - PaaS",
     "Public cloud managed database – PaaS", True),
    ("0-5 ms", "0–5 ms", True),
    # A genuine translation must still be detected as different.
    ("Plataforma desplegada sobre Azure, con servidores redundados",
     "Platform deployed on Azure, with redundant servers", False),
])
def test_same_text_ignores_punctuation_but_not_wording(
        submitted: str, rendered: str, same: bool) -> None:
    assert TiaReportGenerator._same_text(submitted, rendered) is same


def test_no_translation_line_when_the_answer_was_copied() -> None:
    """An English answer copied unchanged needs no translation line."""
    rows = [{"category": "SQL Server", "subject": "Auto statistics", "question": "q",
             "answer": "Both enabled", "criticality": None, "detail": ""}]
    fields = {"Auto statistics": {"question": "q?", "answer": "Both  enabled"}}
    out = TiaReportGenerator._render_category(
        "SQL Server", rows, first=False, fields=fields)
    assert "Answer: Both  enabled" in out
    assert "Translation:" not in out


def test_multiline_answer_is_flattened_but_keeps_every_word() -> None:
    rows = [{"category": "General Information", "subject": "Name and role",
             "question": "q", "answer": "A - Manager; B - BA",
             "criticality": None, "detail": ""}]
    fields = {"Name and role": {"question": "q?",
                                "answer": "A - Manager\nB - Business Analyst"}}
    out = TiaReportGenerator._render_category(
        "General Information", rows, first=False, fields=fields)
    assert "Answer: A - Manager; B - Business Analyst" in out
    assert "BA" not in out.split("Translation:")[0]   # not abbreviated in the Answer


def test_render_key_findings_lists_flagged_rows_worst_first() -> None:
    out = TiaReportGenerator._render_key_findings([
        _row("SQL Server", "Log levels", "Suggestion"),
        _row("Security", "RR auth", "Red Flag"),
        _row("App", "Load balancer", "Strong Recommendation"),
    ])
    order = [out.index(s) for s in ("Red Flag", "Strong Recommendation", "Suggestion")]
    assert order == sorted(order)
    assert out.startswith("## Key Findings")
    assert "✓" not in out


def test_render_key_findings_omits_unflagged_and_unassessed_rows() -> None:
    """The exact contradiction this replaced: unflagged rows being listed as
    findings while the count table reported none."""
    out = TiaReportGenerator._render_key_findings([
        _row("Security", "Antivirus", None, answer="Don't know"),
        _row("SQL Server", "Encryption", "Red Flag"),
    ])
    assert "Antivirus" not in out
    assert "Encryption" in out


def test_render_key_findings_with_nothing_flagged() -> None:
    out = TiaReportGenerator._render_key_findings(
        [_row("Security", "Antivirus", None, answer="Don't know")])
    assert out.rstrip().endswith("No findings were raised.")


def test_render_key_findings_caps_the_list_and_says_so() -> None:
    rows = [_row("SQL Server", f"S{i}", "Suggestion") for i in range(15)]
    out = TiaReportGenerator._render_key_findings(rows, limit=12)
    assert len(re.findall(r"^\d+\. \*\*", out, re.M)) == 12
    assert "A further 3 lower-severity findings" in out


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
    sec = ("### Security\n**1. Antivirus? — Red Flag**\nAnswer: No\n\n"
           "**2. Encryption keys? — Not assessed**\nAnswer: Don't know\n")
    counts = TiaReportGenerator._count_criticalities([sql, sec])
    assert counts == {
        "Red Flag": 2, "Strong Recommendation": 1,
        "Recommendation": 1, "Suggestion": 0,
        # Tallied alongside the severities, but never as one of them.
        "Not assessed": 1,
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
    (src / "a.json").write_text(_customer_json({"q1": "v", "q2": "v"}), encoding="utf-8")
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
    """Outstanding Questions lists the points the assessment could not cover —
    real blank or "Don't know" answers — never invented reference topics."""
    hints = dict(TiaReportGenerator.REPORT_SECTIONS)
    h = hints["Outstanding Questions"]
    assert "blank/missing or was 'Don't know'/unsure" in h
    assert "list EVERY one of them" in h
    assert "topics the form never asked" in h


def test_key_findings_criticalities_cannot_exceed_the_count_table() -> None:
    """Both are derived from the same rows, so the section can no longer claim a
    severity the table does not have — the bug that produced six invented Strong
    Recommendations against a table reading zero."""
    rows = [_row("Security", "RR auth", "Red Flag"),
            _row("SQL Server", "Log levels", "Suggestion"),
            _row("App", "Hosting", None, answer="Don't know")]
    kf = TiaReportGenerator._render_key_findings(rows)
    listed = re.findall(r"^\d+\. \*\*.*— (.+?)\*\*$", kf, re.M)
    from collections import Counter
    ledger = Counter(r["criticality"] for r in rows if r["criticality"])
    assert Counter(listed) == ledger


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
    (src / "a.json").write_text(_customer_json(), encoding="utf-8")
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
        "Summary", "Outstanding Questions"}
    for title in ("Summary", "Outstanding Questions"):
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
