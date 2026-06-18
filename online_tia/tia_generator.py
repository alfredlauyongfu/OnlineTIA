"""Generate a Technical Infrastructure Assessment (TIA) for a customer.

Reads every *.json file from a caller-supplied directory (the customer's
intermediate JSON data) and POSTs it as the user message to the SS&C Cloud
RAG chat-completions endpoint. The RAG retrieval is scoped by tags to the
TIA reference documents already ingested via `rag_ingester.py`. The LLM's
Markdown response is written to OUTPUT_REPORT_DIR with a timestamped name.

The class itself is directory-agnostic — `INTERMEDIATE_JSON_DIR` is referenced
only in the standalone `main()` test harness at the bottom of this file.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests

from logging_setup import bootstrap


logger = logging.getLogger(__name__)


class TiaGenerationError(RuntimeError):
    """Raised on HTTP non-2xx, malformed response shape, or missing content
    from the RAG chat-completions endpoint."""


TIA_SYSTEM_PROMPT = """You are a senior infrastructure consultant authoring a Technical
Infrastructure Assessment (TIA) for a customer. Use the customer-provided JSON data
(their TIA workbook contents, supplied in the user message) together with the
reference chunks retrieved from the RAG store (TIA standards, scoring guidance,
recommended configurations). Incorporate the reference material into your
assessment and flag any customer answers that violate or fall short of the
reference recommendations.

The full report is produced ONE SECTION AT A TIME. Each request names the single
section to write — produce ONLY that section's content.

Output requirements:
- Markdown ONLY. No preamble, no sign-off, no document title. Do not wrap the
  output in a code fence.
- Begin with the exact level-2 heading given for the requested section, then its
  content. Use level-3 (###) headings for any subsections.
- Be concrete: name specific systems, versions, counts, hostnames where given.
- Use bullet lists, short paragraphs, and tables where they aid readability.
- Do NOT invent values. Where the customer data is missing or ambiguous, say so
  explicitly within the relevant section.
- Do not produce content belonging to other sections.
"""


class TiaReportGenerator:
    DEFAULT_TAGS = ["tia_reference"]
    DEFAULT_EMBEDDING_MODEL = "all-mpnet-base-v2"
    DEFAULT_TOP_K = 5
    DEFAULT_N_VECTOR_CANDIDATES = 20
    DEFAULT_N_FULLTEXT_CANDIDATES = 20
    DEFAULT_NUM_QUERY_REWRITES = 3

    # The /rag/chat/completions endpoint hard-caps output near ~4096 completion
    # tokens and ignores every max-token request parameter, so a single-call
    # full report gets silently truncated mid-content. We therefore generate the
    # report one section at a time (each section stays well under the cap) and
    # concatenate. Each section makes its own RAG call, so retrieval is naturally
    # scoped to that section's topic.
    REPORT_SECTIONS: tuple[tuple[str, str], ...] = (
        ("Executive Summary",
         "Give a concise overview of the environment's scale (Blue Prism version, "
         "Runtime Resource / App Server / Interactive Client counts, scale tier, "
         "virtualisation) and the most significant findings as a short bulleted list."),
        ("Servers (SQL / App / Runtime)",
         "Assess the Database/SQL Server, Application Servers, and Runtime Resources: "
         "platform, scale-tier specs, dedication, connection security, backups, "
         "statistics, index maintenance, encryption-key location, authentication."),
        ("Interactive Clients",
         "Assess the Interactive Clients: counts, platform, and any mirroring/build "
         "differences between development clients and production Runtime Resources."),
        ("Disaster Recovery",
         "Assess DR readiness. If no DR data was provided, state that clearly and list "
         "the DR areas that must be documented for an environment of this scale."),
        ("Security",
         "Summarise the security posture as a table (Area, Status, Severity) covering "
         "SQL connection encryption, encryption-key location, Runtime Resource "
         "authentication, SQL dedication, and any unanswered security questions."),
        ("General Environment",
         "Assess session logging (level, archiving, Data Gateways, Unicode) and the "
         "process/object estate, relating volume to database-load risk."),
        ("Findings & Recommendations",
         "Produce a single prioritised findings table with columns: #, Area, Finding, "
         "Severity, Recommendation. Order by severity (High first)."),
        ("Outstanding Questions",
         "List every customer question that was unanswered, blank, or ambiguous in the "
         "data, grouped by area. Do NOT invent answers — only list what needs clarifying."),
    )

    # If a section's completion_tokens lands at/above this, the gateway very
    # likely truncated it at its ~4096-token ceiling — warn the operator.
    TRUNCATION_TOKEN_THRESHOLD = 4000

    def __init__(
        self,
        base_url: str,
        api_key: str,
        llm_model: str,
        output_dir: Path,
        reference_tags: list[str] | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_model = llm_model
        self.output_dir = output_dir
        self.reference_tags = list(reference_tags) if reference_tags else list(self.DEFAULT_TAGS)
        self.timeout_seconds = timeout_seconds

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

        logger.info(
            "TIA generate start: source_dir=%s, files=%d, model=%s, tags=%s, sections=%d",
            customer_json_dir, len(customer_content), self.llm_model,
            self.reference_tags, len(self.REPORT_SECTIONS),
        )

        data_block = self._build_user_message(customer_content)

        # Generate each section in its own RAG call (the endpoint caps output
        # near ~4096 tokens, so a single all-in-one call truncates).
        section_texts: list[str] = []
        for i, (title, hint) in enumerate(self.REPORT_SECTIONS, start=1):
            logger.info(
                "TIA section %d/%d: %s", i, len(self.REPORT_SECTIONS), title,
            )
            message = data_block + self._section_directive(title, hint)
            section_md = self._call_rag_chat(message, section=title)
            section_texts.append(self._strip_code_fence(section_md).strip())

        markdown = self._assemble_report(section_texts)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._build_output_path(filename_prefix)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(markdown)
        logger.info(
            "TIA written: %s (%d bytes, %d sections)",
            out_path, out_path.stat().st_size, len(section_texts),
        )
        return out_path

    # ---- internals ----

    @staticmethod
    def _read_customer_content(customer_json_dir: Path) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for jf in sorted(customer_json_dir.glob("*.json")):
            try:
                with jf.open("r", encoding="utf-8") as f:
                    result[jf.name] = json.load(f)
            except Exception as exc:
                logger.warning("skip (read error) %s: %s", jf.name, exc)
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
    def _section_directive(title: str, hint: str) -> str:
        """The per-section instruction appended to the shared data block. Tells
        the model to produce ONLY the named section, starting with its heading."""
        return (
            f"\nWrite ONLY the \"{title}\" section of the assessment. "
            f"Begin with this exact heading line:\n\n## {title}\n\n{hint}\n"
        )

    @staticmethod
    def _assemble_report(section_texts: list[str]) -> str:
        """Join the per-section Markdown into one document with a title header
        and horizontal rules between sections."""
        title = (
            "# Technical Infrastructure Assessment\n"
            "### Blue Prism Enterprise — Customer Environment\n"
        )
        return title + "\n---\n\n" + "\n\n---\n\n".join(section_texts) + "\n"

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

    def _call_rag_chat(self, user_message: str, section: str | None = None) -> str:
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
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout_seconds
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            logger.error("TIA LLM call FAILED (gateway): %s", exc)
            raise

        if not response.ok:
            logger.error(
                "TIA LLM call FAILED: HTTP %d: %s",
                response.status_code, response.text[:500],
            )
            raise TiaGenerationError(
                f"rag/chat HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("TIA LLM call FAILED: bad JSON: %s; body=%s", exc, response.text[:500])
            raise TiaGenerationError(
                f"rag/chat non-JSON response: {response.text[:500]}"
            )

        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            logger.error(
                "TIA LLM call FAILED: missing/empty 'content'; payload keys=%s",
                list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
            )
            raise TiaGenerationError(
                "rag/chat response missing non-empty 'content' field"
            )

        citations = payload.get("rag_citations") or []
        completion_tokens = (payload.get("llm_usage") or {}).get("completion_tokens")
        logger.info(
            "TIA LLM call OK%s: output_chars=%d, completion_tokens=%s, citations=%d",
            label, len(content), completion_tokens,
            len(citations) if isinstance(citations, list) else 0,
        )

        # Truncation guardrail: the /rag/chat/completions endpoint silently caps
        # output near ~4096 completion tokens and ignores max-token request
        # params. If we land at/above the threshold, the section was very likely
        # cut off — surface it so an operator notices an incomplete report.
        if (
            isinstance(completion_tokens, int)
            and completion_tokens >= self.TRUNCATION_TOKEN_THRESHOLD
        ):
            logger.warning(
                "TIA output may be TRUNCATED%s: completion_tokens=%d is at/near the "
                "gateway's ~4096-token cap; content likely cut off mid-section.",
                label, completion_tokens,
            )
        if isinstance(citations, list):
            # Per-citation summary line (quick scan).
            for c in citations:
                if isinstance(c, dict):
                    logger.info(
                        "  cite: %s#p%s score=%s",
                        c.get("file_name"), c.get("page_number"), c.get("score"),
                    )
            # Full structured dump so the log file captures every field the
            # gateway returned for the citations (file_name, page_number, score,
            # plus any extras the API may add later).
            logger.info(
                "TIA rag_citations (full):\n%s",
                json.dumps(citations, ensure_ascii=False, indent=2),
            )

        return content

    def _build_output_path(self, prefix: str) -> Path:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.output_dir / f"{prefix}_{ts}.md"


# ---- standalone test harness ----
#
# Run directly to generate a TIA from whatever is currently in
# INTERMEDIATE_JSON_DIR:
#   & C:\blueprism\OnlineTIA\.venv\Scripts\python.exe C:\blueprism\OnlineTIA\online_tia\tia_generator.py
#
# Requires INTERMEDIATE_JSON_DIR to be populated (run run.py first) and the
# reference files to already be ingested in the RAG store (run rag_ingester.py
# first, or as part of the overall pipeline).
#
# NOTE: INTERMEDIATE_JSON_DIR is referenced ONLY here in the harness — the
# TiaReportGenerator class itself stays directory-agnostic.

REQUIRED_ENV_VARS = (
    "SSC_CLOUD_AIGATEWAY_BASE_URL",
    "SSC_CLOUD_AIGATEWAY_API_KEY",
    "SSC_CLOUD_AIGATEWAY_MODEL",
    "OUTPUT_REPORT_DIR",
    "INTERMEDIATE_JSON_DIR",
    "LOG_DIR",
)


def main() -> int:
    rc = bootstrap(REQUIRED_ENV_VARS)
    if rc is not None:
        return rc

    customer_dir = Path(os.environ["INTERMEDIATE_JSON_DIR"])
    if not any(customer_dir.glob("*.json")):
        print(
            f"No .json files in {customer_dir}\n"
            f"Run run.py first to populate it from INPUT_DIR.",
            file=sys.stderr,
        )
        return 1

    gen = TiaReportGenerator(
        base_url=os.environ["SSC_CLOUD_AIGATEWAY_BASE_URL"],
        api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
        llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
        output_dir=Path(os.environ["OUTPUT_REPORT_DIR"]),
    )

    print("=== tia_generator (standalone test) ===")
    print(f"  customer dir: {customer_dir}")
    print(f"  output dir  : {gen.output_dir}")
    print(f"  endpoint    : {gen.base_url}/rag/chat/completions")
    print(f"  model       : {gen.llm_model}")
    print(f"  tags        : {gen.reference_tags}")

    try:
        out_path = gen.generate(customer_dir)
    except (
        TiaGenerationError,
        FileNotFoundError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as exc:
        print(f"  FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"  OK -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
