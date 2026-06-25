"""Render a TIA report into a native Microsoft Word (.docx) document.

This writer is INDEPENDENT of the Markdown file: it builds a fresh Word
document from the report's in-memory section content (the same per-section
Markdown the LLM produced), mapping each Markdown construct to a native Word
element (headings, tables, lists, blockquotes, bold/code runs). It does NOT
parse or depend on the written `.md` file.

The generator calls `write_docx` in a best-effort try/except, so a missing
`python-docx` or any render error degrades to a logged warning without
affecting the `.md` output or the pipeline.

Only the limited, well-formed set of Markdown constructs the TIA reports
actually use is handled; anything unrecognised becomes a plain paragraph so
rendering never raises on unexpected input.
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.document import Document as _Document
from docx.shared import Pt
from docx.text.paragraph import Paragraph


# A Markdown heading line: 1-6 leading '#', then the text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# A table separator row, e.g. |---|:--:|---| (dashes, colons, pipes, spaces).
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# Unordered list item: '-' or '*' bullet.
_ULIST_RE = re.compile(r"^\s*[-*]\s+(.*)$")
# Ordered list item: '1.' / '2)' etc.
_OLIST_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
# Inline split points: **bold** or `code` (kept as delimiters via capture).
_INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
_MONO_FONT = "Consolas"


def write_docx(title: str, section_texts: list[str], docx_path: Path) -> None:
    """Build a .docx at `docx_path` from the report title and section bodies.

    `section_texts` are the per-section Markdown strings (already stripped of
    code fences) in document order; `title` is the report's Markdown title
    block (the same one the `.md` carries).
    """
    doc = Document()
    _render_block(doc, title)
    for section in section_texts:
        _render_block(doc, section)
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(docx_path))


# ---- internals ----

def _render_block(doc: _Document, markdown: str) -> None:
    """Render one Markdown block (title or section) into `doc`."""
    lines = markdown.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Blank line — nothing to add (Word paragraphs already separate).
        if not stripped:
            i += 1
            continue

        # Thematic break: a line of only dashes (not a table separator, which
        # is handled in the table branch). Render as a thin spacer paragraph.
        if stripped in ("---", "***", "___"):
            i += 1
            continue

        # Heading.
        m = _HEADING_RE.match(stripped)
        if m:
            level = len(m.group(1))
            doc.add_heading(m.group(2).strip(), level=min(level, 4))
            i += 1
            continue

        # Table: a '|' line immediately followed by a separator row.
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            i = _render_table(doc, lines, i)
            continue

        # Blockquote.
        if stripped.startswith(">"):
            text = stripped.lstrip(">").strip()
            p = doc.add_paragraph(style="Intense Quote")
            _add_runs(p, text)
            i += 1
            continue

        # Unordered list.
        m = _ULIST_RE.match(line)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            _add_runs(p, m.group(1).strip())
            i += 1
            continue

        # Ordered list.
        m = _OLIST_RE.match(line)
        if m:
            p = doc.add_paragraph(style="List Number")
            _add_runs(p, m.group(1).strip())
            i += 1
            continue

        # Plain paragraph.
        p = doc.add_paragraph()
        _add_runs(p, stripped)
        i += 1


def _render_table(doc: _Document, lines: list[str], start: int) -> int:
    """Render a GFM pipe table starting at `lines[start]` (the header row,
    with `lines[start+1]` the separator). Returns the index just past the
    table."""
    header = _split_row(lines[start])
    body: list[list[str]] = []
    i = start + 2  # skip header + separator
    while i < len(lines) and "|" in lines[i] and lines[i].strip():
        body.append(_split_row(lines[i]))
        i += 1

    ncols = max([len(header)] + [len(r) for r in body]) or 1
    table = doc.add_table(rows=1, cols=ncols)
    try:
        table.style = "Table Grid"
    except Exception:
        pass  # style not in the default template — leave unstyled

    # Header row (bold).
    hdr_cells = table.rows[0].cells
    for c in range(ncols):
        text = header[c] if c < len(header) else ""
        para = hdr_cells[c].paragraphs[0]
        _add_runs(para, text, bold=True)

    # Body rows.
    for row in body:
        cells = table.add_row().cells
        for c in range(ncols):
            text = row[c] if c < len(row) else ""
            _add_runs(cells[c].paragraphs[0], text)

    return i


def _split_row(line: str) -> list[str]:
    """Split a Markdown table row into trimmed cell strings, dropping the
    leading/trailing pipe-induced empties."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _add_runs(paragraph: Paragraph, text: str, bold: bool = False) -> None:
    """Append `text` to `paragraph`, honouring inline **bold** and `code`.

    `bold=True` makes the whole segment bold (used for table headers); inline
    `**...**` still forces bold and `` `...` `` renders monospaced.
    """
    for part in _INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = _MONO_FONT
            run.font.size = Pt(10)
            if bold:
                run.bold = True
        else:
            run = paragraph.add_run(part)
            if bold:
                run.bold = True
