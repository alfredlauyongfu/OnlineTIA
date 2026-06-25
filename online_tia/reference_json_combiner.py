"""Per-sheet TIA-relevant extraction from REFERENCE_JSON_DIR.

For each `<workbook_stem>__<sheet>.json` file in reference_json_dir, ask the
LLM to extract only the facts relevant to a Technical Infrastructure
Assessment (TIA), dropping instructions/changelogs/templates/blanks/
intra-sheet duplicates. Each non-empty extraction is written as
`extracted_{sheet}_{YYYYMMDD_HHMMSS}.json` back into the same
reference_json_dir. Source files and extraction files coexist in that dir
and are distinguished by the `extracted_` prefix; only `extracted_*.json`
files are wiped at the start of each `combine()` call (the source per-sheet
JSONs the converter just wrote are preserved).

(The previous pass-2 LLM merge into a single consolidated JSON has been
removed — downstream consumers now use the RAG service to query the
extracted artifacts. See `rag_ingester.py`.)

The HTTP call POSTs an OpenAI-compatible chat-completions body to
{SSC_CLOUD_AIGATEWAY_BASE_URL}/v1/chat/completions. user_id and use_case_id are
passed as custom headers.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import requests


logger = logging.getLogger(__name__)


class GatewayUnreachable(RuntimeError):
    """Raised when the SSC Cloud AIGateway cannot be contacted at all
    (DNS failure, connection refused, TLS handshake failure, timeout).
    Distinct from per-request failures so the caller can abort early."""


EXTRACT_SYSTEM_PROMPT = """You are an analyst preparing input for a Technical Infrastructure Assessment (TIA).
You will receive the JSON contents of ONE sheet from a TIA reference workbook. The sheet name is given.

Extract ONLY facts that are genuinely relevant to a TIA. Discard:
- Instructions, boilerplate, help text, change logs, version history.
- Template / placeholder rows with no real answer.
- Header or label text that appears as data.
- Rows whose answers are empty, blank, "N/A", or unanswered.
- Duplicate or near-duplicate entries within this sheet.

Output a single JSON object whose top-level keys are snake_case TIA topic names
appropriate to this sheet's content (examples: "sql_servers", "app_servers",
"interactive_clients", "runtime_resources", "dr", "security", "general",
"summary", "notes", "report"). Choose the most natural structure under each
topic (object, list of objects, etc.). Omit any key whose value would be empty.

If the sheet contributes nothing useful, output exactly: {}

Keep the JSON COMPACT — large sheets otherwise overflow the response limit:
- Do NOT copy long guidance / help / explanatory paragraphs verbatim. Where a
  scoring table gives per-option guidance, condense it to at most one short
  phrase, or omit it and keep just the option label and its score/rating.
- Do not repeat a question's full wording as both a key and a value; use a
  short snake_case key and keep the value to the essential fact.
- For scoring/rating tables, prefer capturing the recommended/best value and
  the key thresholds compactly rather than enumerating every option in full.
- Avoid duplicated boilerplate across entries; state shared context once.

Output requirements:
- Output ONLY valid JSON. No markdown fences, no commentary, no preamble.
- Use snake_case for all keys.
- Omit empty / null / blank fields.
"""


class ReferenceJsonCombiner:
    # Per-call output cap. The gateway's default max_tokens truncates response
    # JSON around ~4K tokens; 16384 was insufficient for the largest sheet
    # (SQL_Server), whose verbose scoring JSON overflowed and produced an
    # invalid-JSON parse error. Raised to 32768 and paired with the
    # "keep the JSON COMPACT" directives in EXTRACT_SYSTEM_PROMPT.
    EXTRACT_MAX_TOKENS = 32768

    def __init__(
        self,
        api_url: str,
        api_key: str,
        user_id: str,
        use_case_id: str,
        model: str,
        reference_json_dir: Path,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.use_case_id = use_case_id
        self.model = model
        self.reference_json_dir = reference_json_dir
        self.timeout_seconds = timeout_seconds

    # ---- public ----

    def combine(self) -> int:
        if not self.reference_json_dir.is_dir():
            logger.error("Reference JSON directory not found: %s", self.reference_json_dir)
            return 1

        # Defensive: ignore anything matching our own per-sheet output naming
        # if it ever ends up in the source dir (legacy state, manual copy, etc.).
        json_files = sorted(
            p for p in self.reference_json_dir.glob("*.json")
            if not p.name.startswith("extracted_")
        )
        if not json_files:
            logger.error("No .json files found in %s", self.reference_json_dir)
            return 1

        # Selective wipe: only delete previous-run extractions
        # (`extracted_*.json`). The source per-sheet JSONs the converter
        # just wrote share this dir and must be preserved.
        wiped = 0
        for p in self.reference_json_dir.glob("extracted_*.json"):
            if p.is_file():
                p.unlink()
                wiped += 1
        logger.info(
            "wiped %d previous extracted_*.json file(s) from %s",
            wiped, self.reference_json_dir,
        )
        logger.info("combine() starting: %d source file(s)", len(json_files))

        non_empty = 0
        for jf in json_files:
            sheet_name = self._sheet_name_from_filename(jf)
            try:
                with jf.open("r", encoding="utf-8") as f:
                    sheet_data = json.load(f)
            except Exception as exc:
                logger.error("skip (read error) %s: %s", jf.name, exc)
                continue

            try:
                extracted = self._extract_sheet(sheet_name, sheet_data)
            except GatewayUnreachable as exc:
                logger.error(
                    "Aborting: %s (no further sheets will be attempted)", exc
                )
                return 1
            except Exception as exc:
                # LLM call failure was already logged inside _call_llm; this
                # records the higher-level "skipping this sheet" decision.
                logger.warning("skip (extract error) %s: %s", jf.name, exc)
                continue

            if extracted:
                non_empty += 1
                ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                per_sheet_path = self.reference_json_dir / f"extracted_{sheet_name}_{ts}.json"
                with per_sheet_path.open("w", encoding="utf-8") as f:
                    json.dump(extracted, f, ensure_ascii=False, indent=2)
                logger.info(
                    "extracted %s -> %d topic(s) (wrote %s)",
                    jf.name, len(extracted), per_sheet_path.name,
                )
            else:
                logger.info("extracted %s -> (no relevant content)", jf.name)

        logger.info(
            "combine() finished: %d non-empty extraction(s) written to %s",
            non_empty, self.reference_json_dir,
        )
        return 0

    # ---- internals ----

    @staticmethod
    def _sheet_name_from_filename(path: Path) -> str:
        """Filenames produced by ExcelToJsonConverter look like
        '{workbook_stem}__{sheet_name}.json'. Take the part after '__'.
        Fall back to the full stem if the separator isn't present.
        """
        stem = path.stem
        return stem.split("__", 1)[1] if "__" in stem else stem

    def _extract_sheet(self, sheet_name: str, sheet_data: Any) -> dict[str, Any]:
        user_content = json.dumps(
            {"sheet_name": sheet_name, "data": sheet_data},
            ensure_ascii=False,
        )
        return self._call_llm(
            EXTRACT_SYSTEM_PROMPT,
            user_content,
            self.EXTRACT_MAX_TOKENS,
            label=f"extract:{sheet_name}",
        )

    def _call_llm(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int,
        label: str,
    ) -> dict[str, Any]:
        # api_url is the gateway root (e.g. https://api-ai.ssnc.cloud). The
        # OpenAI-compatible chat-completions endpoint lives at /v1/chat/completions;
        # the RAG endpoints live at /rag/... under the same root.
        url = f"{self.api_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-User-Id": self.user_id,
            "X-Use-Case-Id": self.use_case_id,
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        # Initiation: logged unconditionally before the POST.
        logger.info(
            "LLM call start: %s (model=%s, max_tokens=%d, input_chars=%d)",
            label, self.model, max_tokens, len(user_content),
        )

        try:
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=self.timeout_seconds,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                raise GatewayUnreachable(f"Cannot reach gateway at {url}: {exc}") from exc

            if not response.ok:
                # On error the body is a short HTML/JSON page, safe to read fully.
                raise RuntimeError(
                    f"LLM HTTP {response.status_code}: {response.text[:500]}"
                )

            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(
                    f"Unexpected LLM response shape: {exc}; body={response.text[:500]}"
                )

            if not content:
                raise RuntimeError("LLM returned no content")

            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"LLM returned non-JSON content: {exc}; content={content[:500]}"
                )

            if not isinstance(parsed, dict):
                raise RuntimeError(f"LLM returned non-object JSON: {type(parsed).__name__}")
        except GatewayUnreachable as exc:
            logger.error("LLM call FAILED (gateway): %s: %s", label, exc)
            raise
        except Exception as exc:
            logger.error("LLM call FAILED: %s: %s", label, exc)
            raise

        # Completion (success): logged with a matching label and size metrics.
        logger.info(
            "LLM call OK: %s (output_chars=%d, topics=%d)",
            label, len(content), len(parsed),
        )
        return parsed
