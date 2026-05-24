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
recommended configurations). Incorporate the reference material implicitly into
your assessment and flag any customer answers that violate or fall short of the
reference recommendations.

Output requirements:
- Markdown ONLY. No preamble. Do not wrap the entire document in a code fence.
- Suggested top-level sections (omit any that don't apply to the customer's data):
    # Executive Summary
    ## Servers (SQL / App / Runtime)
    ## Interactive Clients
    ## Disaster Recovery
    ## Security
    ## General Environment
    ## Findings & Recommendations
    ## Outstanding Questions
- Be concrete: name specific systems, versions, counts, hostnames where given.
- Use bullet lists, short paragraphs, and tables where they aid readability.
- Where the customer data is missing or ambiguous, list it under
  "## Outstanding Questions" — do NOT invent values.
"""


class TiaReportGenerator:
    DEFAULT_TAGS = ["tia_reference"]
    DEFAULT_EMBEDDING_MODEL = "all-mpnet-base-v2"
    DEFAULT_TOP_K = 5
    DEFAULT_N_VECTOR_CANDIDATES = 20
    DEFAULT_N_FULLTEXT_CANDIDATES = 20
    DEFAULT_NUM_QUERY_REWRITES = 3

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
            "TIA generate start: source_dir=%s, files=%d, model=%s, tags=%s",
            customer_json_dir, len(customer_content), self.llm_model, self.reference_tags,
        )

        user_message = self._build_user_message(customer_content)
        markdown = self._call_rag_chat(user_message)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._build_output_path(filename_prefix)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(markdown)
        logger.info(
            "TIA written: %s (%d bytes)", out_path, out_path.stat().st_size
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

    def _call_rag_chat(self, user_message: str) -> str:
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

        logger.info("TIA LLM call start: input_chars=%d", len(user_message))

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
        logger.info(
            "TIA LLM call OK: output_chars=%d, citations=%d",
            len(content), len(citations) if isinstance(citations, list) else 0,
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
    "SSC_CLOUD_RAG_BASE_URL",
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
        base_url=os.environ["SSC_CLOUD_RAG_BASE_URL"],
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
