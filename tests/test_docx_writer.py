"""Tests for docx_writer — render sample Markdown into the branded template and
reopen with python-docx to assert the cover and native structure round-trip."""

from __future__ import annotations

from pathlib import Path

from docx import Document

from docx_writer import COVER_SUBTITLE, write_docx


_SECTION = "\n".join([
    "## Summary",
    "",
    "An assessment with **bold** text and an `inline code` span.",
    "",
    "| Criticality | Number of instances |",
    "|---|---|",
    "| Red Flag | 1 |",
    "| Suggestion | 0 |",
    "",
    "## Detailed Assessment",
    "",
    "### SQL Server",
    "",
    "**1. Are the connections secured? — Red Flag**",
    "Answer: No",
    "",
    "## Outstanding Questions",
    "",
    "- Network latency was not provided.",
])


def _write(tmp_path, **kw):
    out = tmp_path / "report.docx"
    write_docx([_SECTION], out, organisation="Acme Corporation",
               assessment_date="04 July 2026", **kw)
    return out


def test_write_docx_uses_template_cover(tmp_path: Path) -> None:
    """The branded template is used: the cover carries the Organisation (Title)
    and the fixed Subtitle, and the assessment date."""
    doc = Document(str(_write(tmp_path)))
    by_style = {p.style.name: p.text for p in doc.paragraphs
                if p.style.name in ("Title", "Subtitle", "Subtitle2")}
    assert by_style.get("Title") == "Acme Corporation"
    assert by_style.get("Subtitle") == "Online Technical Infrastructure Assessment Report"
    assert "04 July 2026" in by_style.get("Subtitle2", "")
    # The cover graphic / logo / theme are carried by the template parts.
    assert len(doc.sections) >= 2                      # cover + main body


def test_write_docx_renders_body_with_branded_styles(tmp_path: Path) -> None:
    """`##` → Heading 1, `###` → Heading 2; table + bold runs round-trip."""
    doc = Document(str(_write(tmp_path)))
    h1 = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    h2 = [p.text for p in doc.paragraphs if p.style.name == "Heading 2"]
    assert "Summary" in h1 and "Detailed Assessment" in h1
    assert "SQL Server" in h2
    assert len(doc.tables) == 1
    t = doc.tables[0]
    assert t.rows[0].cells[0].text == "Criticality"
    assert t.rows[1].cells[0].text == "Red Flag"
    # A bold run from a **bold** block lead exists in the body.
    assert any(r.bold for p in doc.paragraphs for r in p.runs)


def test_write_docx_falls_back_when_template_missing(tmp_path: Path) -> None:
    """A missing template degrades to a blank document — still writes a usable
    report without raising."""
    out = tmp_path / "plain.docx"
    write_docx([_SECTION], out, organisation="Acme",
               assessment_date="04 July 2026",
               template_path=tmp_path / "does_not_exist.docx")
    assert out.exists()
    doc = Document(str(out))
    assert any("Are the connections secured?" in p.text for p in doc.paragraphs)


def test_output_carries_no_source_document_remnants(tmp_path: Path) -> None:
    """The template was derived from another organisation's report. No trace of
    that organisation may reach a generated document — not in the running
    headers, and not in the properties Word shows in File > Info and Explorer.

    Asserted positively (headers hold only the report title, metadata is ours)
    rather than by blacklisting the old text, so any foreign remnant is caught.
    """
    import re
    import zipfile

    z = zipfile.ZipFile(str(_write(tmp_path)))
    core = z.read("docProps/core.xml").decode("utf-8")
    title = re.search(r"<dc:title>([^<]*)</dc:title>", core)
    assert title and title.group(1) == COVER_SUBTITLE
    for tag in ("dc:subject", "cp:keywords", "cp:lastModifiedBy"):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", core)
        assert not (m and m.group(1).strip()), f"<{tag}> still carries source metadata"

    for name in z.namelist():
        if not re.match(r"word/header\d*\.xml", name):
            continue
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>",
                                  z.read(name).decode("utf-8")))
        leftover = text.replace(COVER_SUBTITLE, "").strip()
        assert not leftover, f"{name} carries unexpected header text: {leftover!r}"


def test_running_headers_are_static_text(tmp_path: Path) -> None:
    """The running header must not be built from STYLEREF fields: cached they
    read the report name, but Word re-resolves them on print/PDF to the cover
    Title (the customer's organisation), silently changing the header. Page
    numbering in the footer is a field and must survive."""
    import re
    import zipfile

    z = zipfile.ZipFile(str(_write(tmp_path)))
    for name in z.namelist():
        if not re.match(r"word/header\d*\.xml", name):
            continue
        xml = z.read(name).decode("utf-8")
        assert "STYLEREF" not in xml, f"{name} still resolves text from a style"
        assert "<w:fldChar" not in xml, f"{name} still contains a field"
    assert "PAGE" in z.read("word/footer3.xml").decode("utf-8")


def test_write_docx_creates_parent_dir(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deep" / "r.docx"
    write_docx(["## S\ncontent"], out, organisation="Acme", assessment_date="")
    assert out.exists()
