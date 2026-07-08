"""Generate a Technical Infrastructure Assessment (TIA) for a customer.

Reads every *.json file from a caller-supplied directory (the customer's
intermediate JSON data) and POSTs it as the user message to the SS&C Cloud
RAG chat-completions endpoint. The RAG retrieval is scoped by tags to the
TIA reference documents already ingested via `rag_ingester.py`. The LLM's
Markdown response is written to OUTPUT_REPORT_DIR with a timestamped name.

The class is directory-agnostic — the caller (run.py) supplies the customer
JSON directory and the output directory.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

import requests

from excel_to_json import ExcelToJsonConverter
from http_resilient import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    TRANSIENT_ERRORS,
    call_resilient,
    parse_json,
    raise_for_status,
)


logger = logging.getLogger(__name__)


class TiaGenerationError(RuntimeError):
    """Raised on HTTP non-2xx, malformed response shape, or missing content
    from the RAG chat-completions endpoint."""


TIA_SYSTEM_PROMPT = """You are a senior infrastructure consultant authoring a Technical
Infrastructure Assessment (TIA) report for a customer. Use the customer-provided data
(their TIA questionnaire responses, supplied in the user message as JSON) together
with the reference chunks retrieved from the RAG store (TIA standards, scoring
guidance, recommended configurations). Assess the customer's answers against the
reference recommendations and flag any answers that violate or fall short of them.

The report is produced in two phases: first a single CANONICAL ANALYSIS that fixes
the environment facts and the criticality of every finding, then the individual
report sections one at a time. When a block headed "AUTHORITATIVE ANALYSIS" is
supplied to you, treat it as the SINGLE SOURCE OF TRUTH: reuse its rows, figures,
and criticality ratings verbatim — never re-derive a count or re-rate a finding.
Each request names the single section to write — produce ONLY that section's content.

Assessment categories — every finding is rated with EXACTLY one of these four
criticality labels, defined as follows, used consistently everywhere:
- Red Flag: a problem to address immediately — an immediate risk to the stability
  and functionality of the Blue Prism infrastructure that could cause an outage at
  any time.
- Strong Recommendation: a major risk that poses no immediate threat — e.g. a general
  security risk, a long-term maintenance issue, a high-volume performance issue, or a
  future scalability problem. Should be addressed to ensure long-term operation.
- Recommendation: a change that poses no significant risk but would enhance or
  improve performance, maintainability, scalability, usability, or security.
- Suggestion: an idea for an optimal Blue Prism install that may not apply to every
  environment; there is no perceived risk in ignoring it.
Every question from the customer data appears in the report. Items needing no action
carry NO criticality and NO recommendation line — never manufacture a finding for a
compliant answer.

Version neutrality (strict):
- NEVER name or imply the version of any reference document. Write "the Blue Prism
  installation guide", never "the 7.5 installation guide" or a versioned guide name.
- NEVER recommend or name a specific newer Blue Prism version or release line. If the
  customer's installed version appears out of date, state that a newer Blue Prism
  release is available and recommend reviewing the upgrade path via the official
  product documentation — without citing any version number.
- The customer's own stated version may be quoted as a fact.

Reference grounding (strict): when a block headed "REFERENCE SCORING GUIDANCE" is
supplied, it is the AUTHORITATIVE rubric mapping each question's answer options to
a criticality.
- The Criticality of every flagged row MUST be the level the guidance assigns to
  the customer's answer option.
- An answer the guidance rates as good / best practice must NOT be flagged or
  recommended against.
- Never issue a recommendation that contradicts the guidance; where the guidance
  states a reason for a rating, reuse that reason.
- If the guidance does not cover a question, assess it only from the retrieved
  reference chunks or the customer's own statements — never from general
  knowledge alone. With no such basis, leave the row unflagged.

Style (strict): be concise. Short sentences. No consultant filler, no preamble, no
closing summaries, and never restate a question in prose. Never explain the same
fact twice. Table cells hold fragments or at most 2 short sentences. Narrative
paragraphs have at most 3 sentences. When a table row already states a finding, do
NOT also narrate it elsewhere.

Tone (strict): recommendations are advisory and evidence-based, never commanding.
- Structure every recommendation as: the observed fact, its consequence or risk,
  then the suggested change — e.g. "The SQL connection is unencrypted, so data
  between Blue Prism components and the database travels in clear text. Enabling
  TLS on this connection would remove that exposure."
- Use measured verbs: "consider", "should be", "is recommended", "would benefit
  from".
- Do NOT use urgency words in recommendations — "immediately", "without delay",
  "as soon as possible", "must", "critical to". The criticality label already
  conveys urgency; if a timescale genuinely matters, state the reason and the
  consequence of delay instead of the adverb.

Output requirements:
- Markdown ONLY. No document title, no sign-off. Do not wrap the output in a code
  fence.
- Begin with the exact heading line(s) given for the requested section, then its
  content.
- Be concrete: name specific systems, counts, and settings where given.
- Do NOT invent values. Where the customer data is missing or ambiguous, say so
  explicitly and leave it for the Outstanding Questions section. Never assume a
  default the data does not state (e.g. a log retention period).
- Do not produce content belonging to other sections.
- The Assessment Ledger row IDs (R1, R2, R3, ...) are INTERNAL references for your
  own use only. NEVER write a ledger ID in the report — no "(R11)", no
  "R11, R32", no ID in any heading, bullet, or annotation.
- The REFERENCE SCORING GUIDANCE is INTERNAL. Never mention it, the scoring rubric,
  or the rating machinery in report text — no "the guidance rates this as",
  "per the rubric", "scoring guidance", or similar. State each recommendation as a
  plain fact about the environment.
- Output ONLY the finished section. No reasoning, no self-correction, no
  meta-commentary (never write "Wait", "Let me re-read", "Let me rewrite", "That
  is N rows", or similar), and never restart or repeat the section's heading.
"""


# Sentinel hint for the Detailed Assessment category sections: they are NOT
# LLM-generated. Each is rendered in code from the LLM-produced Assessment
# Ledger (`_render_category`), guaranteeing exactly one Q&A block per ledger
# row — no duplicate, no dropped question — which an LLM render could not.
_CODE_RENDERED = "<code-rendered from the Assessment Ledger>"

# Lead-in line under the `## Detailed Assessment` heading.
_DETAILED_ASSESSMENT_LEAD = (
    "The sections below list every questionnaire answer with the "
    "recommendations it prompted."
)


class TiaReportGenerator:
    DEFAULT_TAGS = ["tia_reference"]
    DEFAULT_EMBEDDING_MODEL = "all-mpnet-base-v2"
    DEFAULT_TOP_K = 5
    DEFAULT_N_VECTOR_CANDIDATES = 20
    DEFAULT_N_FULLTEXT_CANDIDATES = 20
    DEFAULT_NUM_QUERY_REWRITES = 3

    # The report has four top-level sections. Summary, Key Findings, and
    # Outstanding Questions are LLM-generated (each its own RAG call, for
    # topic-scoped retrieval). The Detailed Assessment's seven category
    # subsections are NOT LLM calls — they are rendered in code from the
    # LLM-produced Assessment Ledger, so every question appears exactly once
    # (no duplicate, no drop). Order here IS the report order.
    REPORT_SECTIONS: tuple[tuple[str, str], ...] = (
        ("Summary",
         "Begin with the exact heading line `## Summary`, then 1-2 short sentences "
         "naming the responding organisation and the submission date (from the "
         "customer data) and stating the report assesses their Blue Prism "
         "infrastructure against best practice. Then a 2-column GFM table "
         "`Criticality | Number of instances` listing all four criticality levels "
         "in decreasing order (Red Flag first), including zeros. Copy the four N "
         "values VERBATIM from the authoritative analysis's Criticality Tally "
         "(ignore the row-ID lists in parentheses) — do not count or re-assess. "
         "No criticality definitions, no findings detail in this section."),
        ("Key Findings",
         "Begin with the exact heading line `## Key Findings`, then a numbered list "
         "of ONLY the 8-12 most significant FLAGGED items from the Assessment "
         "Ledger (highest criticality first), plus at most 2 items from Positive "
         "Confirmations worth acknowledging. Each flagged item: a bold lead line "
         "`**<Category> – <Subject> — <Criticality>**` with the criticality copied "
         "from the ledger row, then 1-2 short paragraphs of specifics — the "
         "customer's context, why it matters, and the suggested action, in the "
         "advisory evidence-based tone. Positive-confirmation items instead start "
         "their bold lead line with `✓ ` and carry no criticality. Do NOT cite "
         "ledger row IDs (R1, R2, ...) anywhere. No table in this section."),
        ("General Information", _CODE_RENDERED),
        ("SQL Server", _CODE_RENDERED),
        ("Application Server(s)", _CODE_RENDERED),
        ("Interactive Clients", _CODE_RENDERED),
        ("Runtime Resources (Robots)", _CODE_RENDERED),
        ("Disaster Recovery", _CODE_RENDERED),
        ("Security", _CODE_RENDERED),
        ("Outstanding Questions",
         "Begin with the exact heading line `## Outstanding Questions`. List ONLY "
         "customer data keys whose answer was blank/missing or explicitly "
         "ambiguous AND which do NOT already carry a criticality in the "
         "assessment above, grouped by category, as one-line bullets with no "
         "commentary. Do NOT invent questions about topics the form never asked "
         "(e.g. CPU/RAM specs, auto-growth settings), do NOT repeat a flagged "
         "item, and do NOT invent answers. If nothing qualifies, write the single "
         "line: No outstanding questions."),
    )

    # Labels for the internal phase-1 calls (not emitted as report sections).
    ANALYSIS_LABEL = "Canonical analysis"
    VERIFICATION_LABEL = "Analysis verification"

    # The report's title block (Markdown). Shared by the .md assembly and the
    # independent .docx writer so both formats carry the same heading.
    REPORT_TITLE = (
        "# Technical Infrastructure Assessment\n"
        "### Blue Prism Enterprise — Customer Environment\n"
    )

    # Sheets in the customer workbook that are embedded REFERENCE scaffolding
    # (the version/scale lookup table and the answer→guidance scoring table),
    # not customer answers. They are excluded from the payload sent to the LLM:
    # feeding them invites the model to misread reference values (e.g. a sizing
    # table's CPU/RAM minimums) as the customer's actual configuration, and they
    # bloat the prompt. The same reference content is available via the RAG store.
    # Matched case-insensitively against the sheet name (the part after `__`).
    EXCLUDED_SHEET_NAMES = frozenset({"data", "qandadata"})

    # finish_reason values that mean the model's output was cut off at a token
    # limit (rather than completing naturally). Checked against the gateway's
    # response so truncation is detected by an actual signal, not a token-count
    # guess. (An earlier heuristic assumed a hard ~4096-token cap; the endpoint
    # in fact returns 5k+ token completions intact, so that guess was dropped.)
    TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})

    # The analysis + verification calls carry the full injected scoring guidance
    # (~110 KB) and return the whole ledger, so they run far longer than the
    # narrative section calls — they were timing out at the default read
    # timeout, forcing a fallback to the unverified draft. Give them a longer
    # read timeout of their own.
    ANALYSIS_READ_TIMEOUT = 600.0

    # The four criticality labels, in decreasing severity — the order the
    # Summary count table lists them, and used to tally the rendered blocks.
    # "Strong Recommendation" precedes "Recommendation" so suffix matching
    # resolves the more specific label first.
    CRITICALITY_LEVELS = (
        "Red Flag", "Strong Recommendation", "Recommendation", "Suggestion",
    )

    # The seven Detailed Assessment categories whose blocks carry the
    # authoritative criticalities used to reconcile the Summary count table.
    _DETAILED_CATEGORIES = frozenset({
        "General Information", "SQL Server", "Application Server(s)",
        "Interactive Clients", "Runtime Resources (Robots)",
        "Disaster Recovery", "Security",
    })

    # The Summary's criticality count table (header + separator + data rows),
    # matched so it can be replaced wholesale with code-computed counts.
    _COUNT_TABLE_RE = re.compile(
        r"\|[^\n]*Number of instances[^\n]*\|[ \t]*\n"
        r"\|[-:\s|]+\|[ \t]*\n"
        r"(?:\|[^\n]*\|[ \t]*\n?)+",
        re.IGNORECASE,
    )

    # Guardrail for the prompt's version-neutrality rule: a version-looking
    # number within ~40 chars of the word "guide" (same sentence, either side)
    # almost certainly names a reference document's version. Heuristic —
    # log-only, never blocks the run.
    _VERSION_NEAR_GUIDE_RE = re.compile(
        r"\d+\.\d+[^\n.]{0,40}?guide|guide[^\n.]{0,40}?\d+\.\d+",
        re.IGNORECASE,
    )

    # Internal Assessment Ledger row IDs (R1, R2, ...) are scaffolding for the
    # canonical-analysis block and must never surface in the report. A word-
    # boundary R followed by digits catches a leak; log-only, never blocks.
    _LEDGER_ID_RE = re.compile(r"\bR\d+\b")

    # The reference scoring rubric is internal; a report that names it (e.g.
    # "the guidance rates this as") has leaked machinery to the customer.
    _GUIDANCE_LEAK_RE = re.compile(
        r"the guidance|scoring guidance|the rubric|reference scoring", re.IGNORECASE
    )

    # Rubric-dense extraction files first, so the per-answer criticality maps
    # always survive the injection size cap even if inventory-style
    # extractions grow.
    _GUIDANCE_PRIORITY = (
        "SQL_Server", "Security", "Interactive_Clients", "Runtime_Resources",
        "App_Server_s", "DR", "General",
    )
    GUIDANCE_MAX_CHARS = 200_000

    def __init__(
        self,
        base_url: str,
        api_key: str,
        llm_model: str,
        output_dir: Path,
        reference_tags: list[str] | None = None,
        timeout_seconds: float = READ_TIMEOUT,
        reference_guidance_dir: Path | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_model = llm_model
        self.output_dir = output_dir
        self.reference_tags = list(reference_tags) if reference_tags else list(self.DEFAULT_TAGS)
        self.timeout_seconds = timeout_seconds
        self.reference_guidance_dir = reference_guidance_dir
        # Customer form keys for the current generate() call — used by the
        # missing-subject guardrail. Set at the top of generate().
        self._customer_keys: list[str] = []

    # ---- public ----

    def generate(self, customer_json_dir: Path, filename_prefix: str = "TIA") -> Path:
        """Read all *.json in customer_json_dir, call RAG chat-completions,
        write the resulting Markdown to output_dir. Returns the written path.
        """
        if not customer_json_dir.is_dir():
            raise FileNotFoundError(f"Customer JSON directory not found: {customer_json_dir}")

        customer_content = self._read_customer_content(customer_json_dir)
        if not customer_content:
            raise TiaGenerationError(
                f"No .json files in {customer_json_dir}; nothing to report on"
            )
        # Every top-level key across the customer files must appear as a Subject
        # in the report; the missing-subject guardrail checks this after assembly.
        self._customer_keys = [
            k for v in customer_content.values() if isinstance(v, dict) for k in v
        ]

        logger.info(
            "TIA generate start: source_dir=%s, files=%d, model=%s, tags=%s, sections=%d",
            customer_json_dir, len(customer_content), self.llm_model,
            self.reference_tags, len(self.REPORT_SECTIONS),
        )

        data_block = self._build_user_message(customer_content)

        # Deterministic grounding: the extracted scoring rubric is injected into
        # the two calls that author and audit every criticality — RAG top_k
        # retrieval cannot reliably surface the rubric entry for all ~35
        # questions, and any entry the model doesn't see it substitutes with
        # general knowledge (which once inverted the Unicode-logging guidance).
        # The 12 section calls copy the ledger verbatim, so they don't need it.
        guidance = self._load_reference_guidance()
        guidance_block = (
            "\n\n=== REFERENCE SCORING GUIDANCE (authoritative criticality "
            "rubric — see system prompt rules) ===\n"
            f"{guidance}\n"
            "=== END REFERENCE SCORING GUIDANCE ===\n"
        ) if guidance else ""

        # Phase 1 — canonical analysis: fix the environment facts and the severity
        # of every finding ONCE. The report's narrative sections are generated as
        # independent RAG calls, and the Detailed Assessment is rendered in code
        # from this analysis's ledger; anchoring everything to this single
        # analysis keeps counts and criticalities consistent across the report.
        logger.info("TIA analysis pass: %s", self.ANALYSIS_LABEL)
        draft_analysis = self._strip_code_fence(
            self._call_rag_chat(
                data_block + guidance_block + self._analysis_directive(),
                section=self.ANALYSIS_LABEL,
                read_timeout=self.ANALYSIS_READ_TIMEOUT,
            )
        ).strip()

        # Phase 1b — verification: audit the draft so an ungrounded finding (one
        # inferred from a reference/sizing table rather than a stated answer)
        # doesn't get propagated confidently into every section. If verification
        # itself fails (gateway), fall back to the unverified draft — still
        # usable — rather than losing the whole report.
        logger.info("TIA analysis verification pass: %s", self.VERIFICATION_LABEL)
        try:
            analysis = self._strip_code_fence(
                self._call_rag_chat(
                    data_block + guidance_block
                    + self._verification_directive(draft_analysis),
                    section=self.VERIFICATION_LABEL,
                    read_timeout=self.ANALYSIS_READ_TIMEOUT,
                )
            ).strip()
        except (TiaGenerationError, *TRANSIENT_ERRORS) as exc:
            logger.warning(
                "TIA verification pass failed (%s); falling back to the "
                "unverified draft analysis", exc,
            )
            analysis = draft_analysis
        # Audit trail: the verified analysis every section is anchored to.
        # DEBUG-level — enable LOG_LEVEL=DEBUG to diagnose section/ledger drift.
        logger.debug("TIA verified analysis:\n%s", analysis)
        canonical = self._canonical_block(analysis)

        # Phase 2 — render each section from the shared analysis. If a section
        # still fails after the HTTP layer's retries, stop and salvage: write a
        # clearly-flagged PARTIAL report from the sections completed so far, then
        # raise so run.py defers the file for a full regeneration next run.
        # The Detailed Assessment blocks are rendered in code from this ledger
        # (one block per row), so coverage is guaranteed; the narrative sections
        # (Summary, Key Findings, Outstanding Questions) are still LLM calls.
        ledger_rows = self._parse_ledger(analysis)
        known = {c.lower() for c in self._DETAILED_CATEGORIES}
        unknown = sorted({
            r["category"] for r in ledger_rows
            if r["category"].strip().lower() not in known
        })
        if unknown:
            logger.warning(
                "ledger has row(s) in %d unknown categ/ies (not rendered): %s",
                len(unknown), ", ".join(unknown),
            )
        logger.info("ledger parsed: %d row(s) across the assessment", len(ledger_rows))

        section_texts: list[str] = []
        failed: tuple[str, Exception] | None = None
        detailed_opened = False
        for i, (title, hint) in enumerate(self.REPORT_SECTIONS, start=1):
            logger.info(
                "TIA section %d/%d: %s", i, len(self.REPORT_SECTIONS), title,
            )
            if title in self._DETAILED_CATEGORIES:
                section_texts.append(
                    self._render_category(title, ledger_rows, first=not detailed_opened)
                )
                detailed_opened = True
                continue
            message = data_block + canonical + self._section_directive(title, hint)
            try:
                section_md = self._call_rag_chat(message, section=title)
            except (TiaGenerationError, *TRANSIENT_ERRORS) as exc:
                logger.error("TIA section '%s' failed after retries: %s", title, exc)
                failed = (title, exc)
                break
            cleaned = self._strip_restarted_draft(
                self._strip_code_fence(section_md).strip(), section=title
            )
            section_texts.append(cleaned)

        if failed is not None:
            title, exc = failed
            banner = (
                f"> ⚠️ **INCOMPLETE REPORT** — generation failed at section "
                f"\"{title}\" ({exc}). {len(section_texts)} of "
                f"{len(self.REPORT_SECTIONS)} sections were produced before the "
                f"failure. Re-run the pipeline to regenerate the full report."
            )
            body = [banner] + section_texts
            out_path = self._write_outputs(body, filename_prefix, partial=True)
            raise TiaGenerationError(
                f"TIA incomplete: section '{title}' failed after retries; "
                f"wrote partial report to {out_path.name}"
            )

        # Deterministic count reconciliation: rewrite the Summary count table
        # from the criticalities actually rendered in the Detailed Assessment
        # blocks, so the summary can never disagree with the detail. (LLM
        # copying/counting of the tally drifts ±1 even with the verification
        # pass — this is the one number the code owns end-to-end.)
        self._reconcile_summary_counts(section_texts)
        return self._write_outputs(section_texts, filename_prefix)

    def _reconcile_summary_counts(self, section_texts: list[str]) -> None:
        """Recount criticalities from the rendered Detailed Assessment blocks and
        overwrite the Summary section's count table with the result. Mutates
        `section_texts` in place. No-op if the Summary section or its count table
        can't be located (logs a warning)."""
        titles = [t for t, _ in self.REPORT_SECTIONS]
        if "Summary" not in titles or len(section_texts) != len(titles):
            return
        summary_idx = titles.index("Summary")
        category_texts = [
            section_texts[i] for i, t in enumerate(titles)
            if t in self._DETAILED_CATEGORIES
        ]
        counts = self._count_criticalities(category_texts)

        canonical = (
            "| Criticality | Number of instances |\n|---|---|\n"
            + "\n".join(f"| {lv} | {counts[lv]} |" for lv in self.CRITICALITY_LEVELS)
            + "\n"
        )
        summary = section_texts[summary_idx]
        new_summary, n = self._COUNT_TABLE_RE.subn(canonical, summary, count=1)
        if n == 0:
            logger.warning(
                "count reconciliation: Summary count table not found; leaving "
                "the model's table (counts=%s)", counts,
            )
            return
        section_texts[summary_idx] = new_summary
        logger.info(
            "count reconciliation: Summary table set from rendered blocks: %s",
            ", ".join(f"{lv}={counts[lv]}" for lv in self.CRITICALITY_LEVELS),
        )

    @classmethod
    def _parse_ledger(cls, analysis: str) -> list[dict]:
        """Parse the `## Assessment Ledger` markdown table out of the canonical
        analysis into row dicts (category, subject, question, answer,
        criticality, detail). This is the single source the Detailed Assessment
        blocks are rendered from, so every question renders exactly once."""
        m = re.search(
            r"##\s*Assessment Ledger\s*\n(.*?)(?=\n##\s|\Z)", analysis, re.S | re.I
        )
        if not m:
            logger.warning("ledger parse: '## Assessment Ledger' section not found")
            return []
        rows: list[dict] = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 6 or not re.fullmatch(r"(?i)R\d+", cells[0]):
                continue  # header, separator, or non-row line
            _id, category, subject, question, answer, criticality = cells[:6]
            # Detail is the last column; any extra pipes belong to it — rejoin
            # them so a Detail containing '|' survives round-trip.
            detail = " | ".join(cells[6:]).strip() if len(cells) > 6 else ""
            crit = criticality if criticality in cls.CRITICALITY_LEVELS else None
            # Dash placeholders ("—"/"-") mean "no value" — normalise them to
            # blank so a heading falls back Question → Subject correctly (some
            # drafts put "—" in Question for administrative fields).
            question = cls._blank_if_dash(question)
            subject = cls._blank_if_dash(subject)
            rows.append({
                "category": category,
                "subject": subject,
                "question": question,
                "answer": answer or "not provided",
                "criticality": crit,
                "detail": detail,
            })
        return rows

    @staticmethod
    def _blank_if_dash(value: str) -> str:
        """Normalise a dash/empty placeholder to '' (so heading fall-through
        works); leaves real text untouched."""
        return "" if value.strip() in ("", "—", "–", "-") else value.strip()

    @classmethod
    def _render_category(cls, category: str, rows: list[dict], *, first: bool) -> str:
        """Render one Detailed Assessment category section in code from the
        ledger rows assigned to it — one numbered Q&A block per row (full
        question as the heading, criticality suffix when flagged, Answer line,
        Recommendation line only when flagged). The first category also opens
        the parent `## Detailed Assessment` heading."""
        cat_rows = [
            r for r in rows if r["category"].strip().lower() == category.lower()
        ]
        out: list[str] = []
        if first:
            out += ["## Detailed Assessment", "", _DETAILED_ASSESSMENT_LEAD, ""]
        out += [f"### {category}", ""]
        if not cat_rows:
            out.append("No questions in this category.")
            return "\n".join(out)
        for n, r in enumerate(cat_rows, start=1):
            heading = r["question"] or r["subject"]
            crit = f" — {r['criticality']}" if r["criticality"] else ""
            out.append(f"**{n}. {heading}{crit}**")
            out.append(f"Answer: {r['answer']}")
            if r["criticality"] and r["detail"]:
                out.append(f"Recommendation: {r['detail']}")
            out.append("")
        return "\n".join(out).rstrip()

    @classmethod
    def _count_criticalities(cls, category_texts: list[str]) -> dict[str, int]:
        """Tally criticalities from the numbered Q&A block headings across the
        Detailed Assessment category sections. A heading ends with
        ` — <Criticality>` when flagged; unflagged blocks are not counted."""
        counts = {lv: 0 for lv in cls.CRITICALITY_LEVELS}
        for text in category_texts:
            for heading in cls._BLOCK_LEAD_RE.findall(text):
                h = heading.strip()
                for lv in cls.CRITICALITY_LEVELS:  # Strong Recommendation first
                    if h.endswith(f"— {lv}") or h.endswith(f"– {lv}"):
                        counts[lv] += 1
                        break
        return counts

    def _write_outputs(
        self, section_texts: list[str], filename_prefix: str, *, partial: bool = False
    ) -> Path:
        """Write the assembled `.md` (authoritative) then the independent,
        best-effort `.docx`. Returns the `.md` path. A missing python-docx or
        any render error degrades to a warning so it can never affect the `.md`
        output or the pipeline's success."""
        markdown = self._assemble_report(section_texts)
        self._warn_version_leaks(markdown)
        self._warn_ledger_id_leaks(markdown)
        self._warn_guidance_leaks(markdown)
        self._warn_coverage(markdown, len(self._customer_keys))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._build_output_path(filename_prefix)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(markdown)
        tag = "PARTIAL " if partial else ""
        logger.info(
            "TIA %swritten: %s (%d bytes, %d blocks)",
            tag, out_path, out_path.stat().st_size, len(section_texts),
        )

        docx_path = out_path.with_suffix(".docx")
        try:
            from docx_writer import write_docx

            write_docx(self.REPORT_TITLE, section_texts, docx_path)
            logger.info(
                "TIA %sWord written: %s (%d bytes)",
                tag, docx_path, docx_path.stat().st_size,
            )
        except Exception as exc:
            logger.warning(
                "TIA Word generation skipped for %s: %s", docx_path.name, exc
            )
        return out_path

    # ---- internals ----

    def _load_reference_guidance(self) -> str | None:
        """Concatenate the extracted reference JSONs (the per-answer criticality
        rubric) for deterministic injection into the analysis/verification
        calls. RAG top_k retrieval cannot reliably surface the rubric entry for
        every question, and any entry the model doesn't see it substitutes with
        general knowledge — injecting the rubric wholesale closes that gap.
        Returns None (with a WARNING) when the dir is unset, missing, or empty;
        generation then degrades to RAG-retrieval-only grounding."""
        if self.reference_guidance_dir is None:
            return None
        files = list(self.reference_guidance_dir.glob("extracted_*.json"))
        if not files:
            logger.warning(
                "No extracted_*.json reference guidance in %s; the analysis "
                "will rely on RAG retrieval alone",
                self.reference_guidance_dir,
            )
            return None

        def rank(p: Path) -> tuple[int, str]:
            for i, marker in enumerate(self._GUIDANCE_PRIORITY):
                if p.name.startswith(f"extracted_{marker}_"):
                    return (i, p.name)
            return (len(self._GUIDANCE_PRIORITY), p.name)

        parts: list[str] = []
        total = 0
        for p in sorted(files, key=rank):
            text = p.read_text(encoding="utf-8")
            if total + len(text) > self.GUIDANCE_MAX_CHARS:
                logger.warning(
                    "reference guidance capped at %d chars; %s and later "
                    "files not injected", self.GUIDANCE_MAX_CHARS, p.name,
                )
                break
            parts.append(f"--- {p.name} ---\n{text}")
            total += len(text)
        if not parts:
            return None
        logger.info(
            "reference scoring guidance: injecting %d file(s), %d chars",
            len(parts), total,
        )
        return "\n\n".join(parts)

    @staticmethod
    def _read_customer_content(customer_json_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {}
        skipped: list[str] = []
        for jf in sorted(customer_json_dir.glob("*.json")):
            sheet = ExcelToJsonConverter.sheet_name_from_path(jf)
            if sheet.lower() in TiaReportGenerator.EXCLUDED_SHEET_NAMES:
                skipped.append(jf.name)
                continue
            try:
                with jf.open("r", encoding="utf-8") as f:
                    result[jf.name] = json.load(f)
            except Exception as exc:
                logger.warning("skip (read error) %s: %s", jf.name, exc)
        if skipped:
            logger.info(
                "excluded %d reference-scaffolding sheet(s) from customer payload: %s",
                len(skipped), ", ".join(skipped),
            )
        return result

    @staticmethod
    def _build_user_message(customer_content: dict[str, Any]) -> str:
        body = json.dumps(customer_content, ensure_ascii=False, indent=2)
        return (
            "Generate the Technical Infrastructure Assessment for the following customer.\n\n"
            "Customer data (each key is a source filename; each value is the parsed JSON):\n\n"
            f"```json\n{body}\n```\n"
        )

    @staticmethod
    def _analysis_directive() -> str:
        """Phase-1 instruction. Produces the canonical facts + criticality-rated
        assessment ledger that every section then renders from, so the
        independently-generated sections share one set of rows and ratings."""
        return (
            "\nThis is the CANONICAL ANALYSIS phase — do NOT write report prose or a "
            "document section. Produce one compact reference block, exactly these "
            "three parts:\n\n"
            "## Environment Facts\n"
            "A short table of the headline facts (Blue Prism version, Runtime "
            "Resource count, Application Server count, Interactive Client count, "
            "process/object count, hosting platforms, database size). Where the data "
            "gives more than one value for a figure, choose ONE operative value, "
            "show it, and note the discrepancy once here so later sections do not "
            "have to decide.\n\n"
            "## Assessment Ledger\n"
            "A table with columns: ID | Category | Subject | Question | Answer | "
            "Criticality | Detail. ID is R1, R2, R3, ... assigned in order. One "
            "row for EVERY question in the customer data — each appears exactly "
            "once, grouped by Category, flagged rows first within each category "
            "(Red Flag first). Produce EXACTLY one row per customer data key: "
            "never merge two keys into one row, never split a key, never invent a "
            "row with no customer key behind it. "
            "This ledger is EXHAUSTIVE and FINAL: everything the report mentions "
            "must be a row here, and later sections may not add rows beyond it. "
            "Category must be exactly one of: General Information, SQL Server, "
            "Application Server(s), Interactive Clients, Runtime Resources "
            "(Robots), Disaster Recovery, Security. Assign each question to the "
            "topically closest category — never dump leftovers into Runtime "
            "Resources. Anchors: administrative fields (submission time, booking "
            "ID, contact name, email, organisation — never flagged), Environment, "
            "Blue Prism version, and free-text catch-alls such as 'Anything else' "
            "go under General Information; database configuration (hosting, "
            "dedication, size, statistics, index maintenance, connection "
            "encryption, latency) under SQL Server; backup and recovery questions "
            "under Disaster Recovery; dev/prod environment parity under "
            "Interactive Clients; authentication and antivirus/endpoint-protection "
            "questions under Security. Criticality is exactly one of the four "
            "assessment categories from the system prompt for rows needing action, "
            "or the single character — for rows needing none. Subject is the "
            "customer's data key copied CHARACTER-FOR-CHARACTER — never reworded, "
            "reformatted, merged, or repeated in another category. Question is the "
            "FULL question text from the matched REFERENCE SCORING GUIDANCE item's "
            "'question' field (the complete wording the customer was asked); leave "
            "Question blank when the item has no reference question (e.g. "
            "administrative fields). Answer is their answer (verbatim, abbreviated "
            "if long); Detail is the recommendation in at most 2 short sentences, "
            "filled ONLY for flagged rows and phrased per the tone rules — the "
            "observed gap and its consequence, then the advisory suggestion. "
            "Where the REFERENCE SCORING GUIDANCE states a reason for a rating, "
            "the Detail must use that reason.\n"
            "Answer-handling rules: a BLANK or missing answer → Answer is 'not "
            "provided', Criticality —, no Detail; it belongs in Outstanding "
            "Questions, never as a finding. An explicitly uncertain answer "
            "('Don't know', 'maybe') MAY be flagged — the finding is the "
            "uncertainty itself and the Detail says what to confirm and why it "
            "matters; a row flagged this way is NOT repeated in Outstanding "
            "Questions.\n\n"
            "## Positive Confirmations\n"
            "At most 5 one-line bullets of explicitly-stated customer configurations "
            "that follow best practice and are worth acknowledging.\n\n"
            "## Criticality Tally\n"
            "Four lines, one per criticality, in this exact form:\n"
            "Red Flag: N (R_, R_, ...)\n"
            "Strong Recommendation: N (R_, R_, ...)\n"
            "Recommendation: N (R_, R_, ...)\n"
            "Suggestion: N (R_, R_, ...)\n"
            "List EVERY ledger row ID with that criticality inside the "
            "parentheses (rows marked — are excluded), then set N to the number "
            "of IDs you just listed — N must equal the length of its own ID "
            "list.\n\n"
            "Grounding rule (critical): a ledger row may assert a customer "
            "configuration ONLY when it is an EXPLICIT answer the customer gave. "
            "Never infer the customer's hardware specs, counts, versions, or "
            "settings from a reference, lookup, sizing, or scoring table. If the "
            "customer did not state a value (e.g. actual CPU/RAM), record it under "
            "Environment Facts as 'not provided' — do NOT raise an "
            "under-/over-provisioning row from a sizing-table value, and leave it "
            "for the Outstanding Questions section.\n"
        )

    @staticmethod
    def _verification_directive(draft_analysis: str) -> str:
        """Phase-1b instruction. Audits the draft analysis for grounding before it
        becomes the authoritative source of truth, removing findings that assert
        configurations the customer never explicitly stated (the highest-assurance
        guard against the analysis confidently propagating a misread to every
        section)."""
        return (
            "\nThis is the ANALYSIS VERIFICATION phase. Below is a DRAFT canonical "
            "analysis. Audit it and re-output a CORRECTED version in the SAME "
            "four-part format (## Environment Facts, ## Assessment Ledger, "
            "## Positive Confirmations, ## Criticality Tally).\n\n"
            "Rules:\n"
            "- Keep every well-grounded row unchanged — same Category, Criticality, "
            "and wording. If you remove rows, renumber the remaining IDs "
            "sequentially (R1..Rn).\n"
            "- A row may assert a customer configuration ONLY if it is an EXPLICIT "
            "customer answer. If a row asserts a spec/count/setting the customer did "
            "not state — e.g. CPU/RAM inferred from a sizing/lookup table — REMOVE "
            "it from the ledger and instead record the missing value under "
            "Environment Facts as 'not provided' (it will surface in Outstanding "
            "Questions).\n"
            "- Audit every row against the REFERENCE SCORING GUIDANCE (when "
            "supplied): correct any Criticality that differs from the "
            "guidance's rating for that answer, REMOVE the flag from any answer "
            "the guidance rates as good, and align the Detail's direction with "
            "the guidance.\n"
            "- Verify each Subject is a customer data key copied verbatim (no "
            "merged/reworded/invented Subjects) and each Question is the full "
            "wording from the matched guidance item (blank if none).\n"
            "- Do NOT invent new rows or change the criticality of grounded rows "
            "that match the guidance.\n"
            "- Re-check every figure in Environment Facts against the customer "
            "answers.\n"
            "- Re-count the Criticality Tally from the corrected ledger.\n\n"
            "DRAFT ANALYSIS:\n"
            f"{draft_analysis}\n"
        )

    @staticmethod
    def _canonical_block(analysis: str) -> str:
        """Wrap the phase-1 analysis as the authoritative context injected into
        every section call."""
        return (
            "\n\n=== AUTHORITATIVE ANALYSIS (single source of truth — reuse its "
            "figures and severities verbatim; do NOT re-derive counts or re-rate "
            "findings) ===\n"
            f"{analysis}\n"
            "=== END AUTHORITATIVE ANALYSIS ===\n"
        )

    @staticmethod
    def _section_directive(title: str, hint: str) -> str:
        """The per-section instruction appended to the shared data block. Tells
        the model to produce ONLY the named section; the hint carries the exact
        heading line(s) plus the section's content and size rules."""
        return (
            f"\nWrite ONLY the \"{title}\" section of the assessment. {hint}\n"
            "Reuse the rows, figures, and criticality ratings from the "
            "authoritative analysis above exactly; do not re-derive counts or "
            "re-rate findings.\n"
        )

    @classmethod
    def _warn_version_leaks(cls, markdown: str) -> None:
        """Log-only guardrail: reference documents must never be cited with a
        version number (prompt rule); surface any slip so an operator notices.
        The customer's own bare version (e.g. '7.4.1' with no 'guide' nearby)
        does not trigger it."""
        m = cls._VERSION_NEAR_GUIDE_RE.search(markdown)
        if m:
            logger.warning(
                "TIA report may name a reference document version: %r", m.group(0)
            )

    @classmethod
    def _warn_ledger_id_leaks(cls, markdown: str) -> None:
        """Log-only guardrail: internal ledger row IDs (R1, R2, ...) must never
        appear in the report (prompt rule); surface any that slip through."""
        ids = cls._LEDGER_ID_RE.findall(markdown)
        if ids:
            logger.warning(
                "TIA report leaked %d internal ledger ID(s): %s",
                len(ids), ", ".join(sorted(set(ids))),
            )

    @classmethod
    def _warn_guidance_leaks(cls, markdown: str) -> None:
        """Log-only guardrail: the internal scoring rubric must never be named
        in the report (prompt rule); surface any slip (e.g. 'the guidance rates
        this as')."""
        m = cls._GUIDANCE_LEAK_RE.search(markdown)
        if m:
            logger.warning(
                "TIA report may reference the internal scoring rubric: %r",
                m.group(0),
            )

    # A numbered Q&A block lead line in the Detailed Assessment sections:
    # `**<n>. <heading text>[ — <Criticality>]**`.
    _BLOCK_LEAD_RE = re.compile(r"^\*\*\d+\.\s+(.+?)\*\*\s*$", re.MULTILINE)

    @classmethod
    def _warn_coverage(cls, markdown: str, expected_count: int) -> None:
        """Log-only guardrail replacing the old form-key grep (headings are now
        full questions, so keys no longer appear verbatim). Every customer
        question must render as exactly one Detailed Assessment block: N distinct
        blocks for N form fields. A count mismatch means a question was dropped
        or merged; a duplicate heading means one was rendered twice."""
        section = markdown.split("## Detailed Assessment", 1)
        body = section[1] if len(section) == 2 else ""
        # Stop at Outstanding Questions so its bullets aren't counted.
        body = body.split("## Outstanding Questions", 1)[0]
        headings = [m.strip() for m in cls._BLOCK_LEAD_RE.findall(body)]
        if expected_count and len(headings) != expected_count:
            logger.warning(
                "TIA coverage: %d assessment block(s) rendered but %d customer "
                "question(s) expected — a question may be dropped or merged.",
                len(headings), expected_count,
            )
        dupes = sorted({h for h in headings if headings.count(h) > 1})
        if dupes:
            logger.warning(
                "TIA coverage: %d duplicate assessment heading(s): %s",
                len(dupes), "; ".join(dupes),
            )

    @classmethod
    def _assemble_report(cls, section_texts: list[str]) -> str:
        """Join the per-section Markdown into one document with a title header
        and horizontal rules between sections."""
        return cls.REPORT_TITLE + "\n---\n\n" + "\n\n---\n\n".join(section_texts) + "\n"

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Defensive: if the model wrapped a whole section in a ```markdown / ```
        fence despite instructions, unwrap it. Leaves inner fenced code blocks
        (which don't start at the very first line) untouched."""
        s = text.strip()
        if not s.startswith("```"):
            return text
        lines = s.splitlines()
        # Drop the opening fence line (``` or ```markdown).
        lines = lines[1:]
        # Drop a trailing closing fence if present.
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)

    @classmethod
    def _strip_restarted_draft(cls, text: str, section: str | None = None) -> str:
        """Deterministic repair for a section the model self-corrected mid-output
        (e.g. wrote a draft, then 'Wait — let me rewrite', then a second
        rendering). A section is generated by ONE call and legitimately opens
        with its heading exactly once, so if that first heading line reappears
        later, everything before the LAST occurrence is a discarded draft (and
        the reasoning between them may leak internal ledger IDs). Keep only the
        final rendering. No-op when the section doesn't restart."""
        lines = text.splitlines()
        heading_idx = next(
            (i for i, ln in enumerate(lines) if ln.lstrip().startswith("#")), None
        )
        if heading_idx is None:
            return text
        heading = lines[heading_idx].strip()
        last = max(
            i for i, ln in enumerate(lines) if ln.strip() == heading
        )
        if last == heading_idx:
            return text
        logger.warning(
            "TIA section '%s' restarted mid-output; discarded the draft before "
            "the final rendering (%d lines dropped)",
            section or heading, last - heading_idx,
        )
        return "\n".join(lines[last:]).strip()

    def _call_rag_chat(
        self, user_message: str, section: str | None = None,
        read_timeout: float | None = None,
    ) -> str:
        rt = read_timeout or self.timeout_seconds
        url = f"{self.base_url}/rag/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }
        body = {
            "llm_name": self.llm_model,
            "embedding_model_name": self.DEFAULT_EMBEDDING_MODEL,
            "n_vector_candidates": self.DEFAULT_N_VECTOR_CANDIDATES,
            "n_fulltext_candidates": self.DEFAULT_N_FULLTEXT_CANDIDATES,
            "top_k": self.DEFAULT_TOP_K,
            "num_query_rewrites": self.DEFAULT_NUM_QUERY_REWRITES,
            "condense_chat_history": False,
            "tags": self.reference_tags,
            "rag_system_prompt": TIA_SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": user_message},
            ],
        }

        label = f" [{section}]" if section else ""
        logger.info("TIA LLM call start%s: input_chars=%d", label, len(user_message))

        try:
            response = call_resilient(
                lambda: requests.post(
                    url, headers=headers, json=body,
                    timeout=(CONNECT_TIMEOUT, rt),
                ),
                label=f"rag/chat{label}",
                read_timeout=rt,
            )
        except TRANSIENT_ERRORS as exc:
            logger.error("TIA LLM call FAILED (gateway): %s", exc)
            raise

        raise_for_status(response, label="rag/chat", error_cls=TiaGenerationError)
        payload = parse_json(response, label="rag/chat", error_cls=TiaGenerationError)

        # A non-object body (e.g. a JSON array) must fail as TiaGenerationError:
        # an AttributeError from payload.get would escape run.py's per-file
        # handling and abort the whole run before finalize.
        if not isinstance(payload, dict):
            logger.error(
                "TIA LLM call FAILED: non-object JSON payload (%s); body=%s",
                type(payload).__name__, response.text[:500],
            )
            raise TiaGenerationError(
                f"rag/chat non-object JSON response: {response.text[:500]}"
            )

        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.error(
                "TIA LLM call FAILED: missing/empty 'content'; payload keys=%s",
                list(payload.keys()),
            )
            raise TiaGenerationError(
                "rag/chat response missing non-empty 'content' field"
            )

        citations = payload.get("rag_citations") or []
        completion_tokens = (payload.get("llm_usage") or {}).get("completion_tokens")
        finish_reason = self._extract_finish_reason(payload)
        logger.info(
            "TIA LLM call OK%s: output_chars=%d, completion_tokens=%s, "
            "finish_reason=%s, citations=%d",
            label, len(content), completion_tokens, finish_reason,
            len(citations) if isinstance(citations, list) else 0,
        )
        # The RAG response shape was never documented; dump its keys once per
        # call at DEBUG so the finish_reason location can be confirmed/adjusted.
        logger.debug("TIA rag/chat payload keys%s: %s", label, sorted(payload))

        # Truncation guardrail: warn only on an actual truncation SIGNAL from the
        # gateway (finish_reason), not a token-count guess. If the endpoint omits
        # finish_reason, a truncated section instead surfaces downstream via the
        # coverage guardrail (short category) and the empty/short-content raise.
        if isinstance(finish_reason, str) and finish_reason in self.TRUNCATION_FINISH_REASONS:
            logger.warning(
                "TIA output TRUNCATED%s: finish_reason=%s (completion_tokens=%s); "
                "content cut off mid-section.",
                label, finish_reason, completion_tokens,
            )
        if isinstance(citations, list):
            # One line per citation (which reference informed the section).
            for c in citations:
                if isinstance(c, dict):
                    logger.info(
                        "  cite: %s#p%s score=%s",
                        c.get("file_name"), c.get("page_number"), c.get("score"),
                    )

        return content

    @staticmethod
    def _extract_finish_reason(payload: dict) -> str | None:
        """Pull the completion's finish_reason from the gateway response,
        checking the shapes it might use (top-level, OpenAI-style choices, or
        the Anthropic-style stop_reason). Returns None when absent."""
        fr = payload.get("finish_reason") or payload.get("stop_reason")
        if fr is None:
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                fr = choices[0].get("finish_reason")
        return fr if isinstance(fr, str) else None

    def _build_output_path(self, prefix: str) -> Path:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"{prefix}_{ts}.md"
