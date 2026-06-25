"""Tests for docx_writer — render sample Markdown to a .docx and reopen it
with python-docx to assert the native structure round-trips."""

from __future__ import annotations

from pathlib import Path

from docx import Document

from docx_writer import write_docx


_SECTION = "\n".join([
    "## Executive Summary",
    "",
    "An assessment with **bold** text and an `inline code` span.",
    "",
    "| Area | Severity |",
    "|---|---|",
    "| SQL Connection | Critical |",
    "| Encryption Keys | High |",
    "",
    "> A blockquote note about the environment.",
    "",
    "- first bullet",
    "- second bullet",
    "",
    "1. first step",
    "2. second step",
])


def test_write_docx_round_trips_structure(tmp_path: Path) -> None:
    title = "# Technical Infrastructure Assessment\n### Blue Prism Enterprise\n"
    out = tmp_path / "report.docx"

    write_docx(title, [_SECTION], out)
    assert out.exists()

    doc = Document(str(out))

    # Headings: the title and the section heading are real Word headings.
    heading_texts = [
        p.text for p in doc.paragraphs if p.style.name.startswith("Heading")
    ]
    assert "Technical Infrastructure Assessment" in heading_texts
    assert "Executive Summary" in heading_texts

    # Exactly one table: 2 columns, header + 2 body rows.
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.columns) == 2
    assert len(table.rows) == 3
    assert table.rows[0].cells[0].text == "Area"
    assert table.rows[0].cells[1].text == "Severity"
    assert table.rows[1].cells[0].text == "SQL Connection"
    assert table.rows[2].cells[1].text == "High"

    # Header cells are bold.
    hdr_runs = [r for p in table.rows[0].cells[0].paragraphs for r in p.runs]
    assert hdr_runs and all(r.bold for r in hdr_runs)

    # List styles applied.
    styles = {p.style.name for p in doc.paragraphs}
    assert "List Bullet" in styles
    assert "List Number" in styles
    assert "Intense Quote" in styles

    # A bold run from the inline **bold** exists somewhere in body paragraphs.
    assert any(r.bold for p in doc.paragraphs for r in p.runs)


def test_write_docx_handles_plain_and_empty(tmp_path: Path) -> None:
    """Plain text with no Markdown constructs still produces a valid doc."""
    out = tmp_path / "plain.docx"
    write_docx("# Title\n", ["## S\n\nJust a sentence.\n"], out)
    doc = Document(str(out))
    assert any("Just a sentence." in p.text for p in doc.paragraphs)
    assert len(doc.tables) == 0


def test_write_docx_creates_parent_dir(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "r.docx"
    write_docx("# T\n", ["## S\ncontent"], out)
    assert out.exists()
