"""Tests for tia_generator pure-logic helpers (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from tia_generator import TiaReportGenerator, TIA_SYSTEM_PROMPT


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
