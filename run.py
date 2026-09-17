"""CLI entry point: run the reference pipeline first (so any new reference
xlsx gets vectorized into RAG-ready extractions before customer data is
processed), sync RAG with the latest local extractions, then process every
customer input file in INPUT_DIR — Excel workbooks (.xlsx/.xlsm) AND JSON
form exports (.json) — **independently and in sequence**: each customer
file is staged as JSON (workbooks converted per-sheet, JSON responses
validated and staged as-is), then a Technical Infrastructure Assessment
(TIA) report is generated for that ONE file's data only, then the loop
moves on to the next customer file. INTERMEDIATE_JSON_DIR is
wiped between files so it never holds content from more than one input
file at a time, and each TIA report is based on exactly one input file.

Paths owned by this file (read from .env, must be absolute):
  INPUT_DIR              - primary input; files move through PROCESSING_DIR to PROCESSED_DIR
  INTERMEDIATE_JSON_DIR  - where primary JSON output is written
  PROCESSING_DIR         - staging folder for in-flight primary files
  PROCESSED_DIR          - where successfully converted primary files land
  OUTPUT_REPORT_DIR      - where the generated TIA Markdown report is written

REQUIRED_VARS below is the union of every env var any stage dereferences
(reference dirs, AIGateway auth, working dirs), so a missing var fails at
bootstrap instead of surfacing as a KeyError mid-run.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path

# Add the sibling source dir to sys.path so the project's modules can be
# imported directly (e.g. `from excel_to_json import ...`). Keeps the module
# files cleanly tucked into src/ while run.py stays at the project
# root as the single entry point.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from excel_to_json import ExcelToJsonConverter
from http_resilient import TRANSIENT_ERRORS
from logging_setup import bootstrap
from rag_ingester import RagIngester, RagGatewayError
from reference_info_extractor import extract as extract_reference_info_stage
from reference_passthrough_ingester import (
    PASSTHROUGH_PATTERNS,
    ingest as ingest_reference_passthrough_stage,
)
from tia_generator import TiaReportGenerator, TiaGenerationError


# The union of every env var any stage dereferences. run.py is the single
# entry point, so a var missing from .env must fail here (bootstrap's clean
# exit-2 message) — not as a KeyError traceback halfway through the run.
REQUIRED_VARS = (
    "INPUT_DIR",
    "INTERMEDIATE_JSON_DIR",
    "PROCESSING_DIR",
    "PROCESSED_DIR",
    "REFERENCE_TO_BE_LOADED_DIR",
    "REFERENCE_LOADED_DIR",
    "REFERENCE_JSON_DIR",
    "OUTPUT_REPORT_DIR",
    "SSC_CLOUD_AIGATEWAY_BASE_URL",
    "SSC_CLOUD_AIGATEWAY_API_KEY",
    "SSC_CLOUD_AIGATEWAY_USER_ID",
    "SSC_CLOUD_AIGATEWAY_MODEL",
    "USE_CASE_ID",
    "LOG_DIR",
)


logger = logging.getLogger(__name__)


# Report-name sizing: enough of the organisation to recognise it, and enough
# Booking ID to tell two submissions apart without pasting a whole GUID.
ORGANISATION_MAX_CHARS = 40
BOOKING_ID_CHARS = 8


def _answer(payload: dict, key: str) -> str:
    """Read one field's answer from a form export, accepting the nested
    `{"question", "answer"}` shape as well as a plain metadata value."""
    value = payload.get(key)
    if isinstance(value, dict):
        value = value.get("answer")
    return value.strip() if isinstance(value, str) else ""


def _slug(text: str, limit: int) -> str:
    """Filename-safe slug, trimmed to `limit` on a word boundary.

    Accents are transliterated FIRST: `safe_name` alone would turn
    "Acmé Soluções" into "Acm__Solu__es", mangling a customer's own name.
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = ExcelToJsonConverter.safe_name(ascii_text) if ascii_text.strip() else ""
    if slug == "sheet" and not ascii_text.strip():
        return ""                      # safe_name's placeholder for empty input
    if len(slug) <= limit:
        return slug
    cut = slug[:limit]
    return (cut.rsplit("_", 1)[0] if "_" in cut else cut).strip("_-")


def _organisation_slug(raw: str) -> str:
    """Organisations commonly answer "Company - Department - Team"; the company
    alone is what makes a filename recognisable, so keep the leading segment."""
    head = re.split(r"\s+-\s+", raw, maxsplit=1)[0] if raw else ""
    return _slug(head or raw, ORGANISATION_MAX_CHARS)


def _submission_date(raw: str) -> str:
    """The submission date as YYYY-MM-DD. Falls back to today when the field is
    absent or unparseable, so naming never fails."""
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return dt.datetime.now().strftime("%Y-%m-%d")


def report_prefix(source: Path) -> str:
    """Filename stem for the TIA report generated from `source`.

    JSON form exports are named
    `TIA_<Organisation>_<Environment>_<submission date>_<Booking ID>` — readable,
    sorts by customer, and keeps the Booking ID that links a report back to its
    support booking. Empty segments are dropped rather than left as dangling
    separators. Anything else — an Excel input, a response with no Organisation,
    an unreadable file — falls back to the sanitized file stem.

    No timestamp is appended, so re-running a submission replaces its previous
    report instead of accumulating near-duplicates.
    Never raises: report naming must not be able to fail the run.
    """
    if source.suffix.lower() == ".json":
        try:
            with source.open("r", encoding="utf-8-sig") as f:
                payload = json.load(f)
            organisation = _organisation_slug(_answer(payload, "Organisation"))
            if organisation:
                parts = [
                    organisation,
                    _slug(_answer(payload, "Environment"), 20),
                    _submission_date(_answer(payload, "Submission time")),
                    _slug(_answer(payload, "Booking ID")[:BOOKING_ID_CHARS],
                          BOOKING_ID_CHARS),
                ]
                return "TIA_" + "_".join(p for p in parts if p)
        except Exception:
            pass  # fall through to the stem rule
    # The flow names submissions "TIA_<booking id>- <timestamp>.json", so a blank
    # Booking ID sends us down the stem rule with a stem that already carries the
    # prefix and an empty booking-id separator ("TIA_-_<timestamp>"). Drop both so
    # the report is named "TIA_<timestamp>" rather than "TIA_TIA_-_<timestamp>".
    stem = ExcelToJsonConverter.safe_name(source.stem)
    core = (stem[4:] if stem.startswith("TIA_") else stem).strip("_-")
    return f"TIA_{core}" if core else stem


def main() -> int:
    rc = bootstrap(REQUIRED_VARS)
    if rc is not None:
        return rc
    logger.info("=== run.py start ===")

    # Snapshot the INPUT_DIR contents BEFORE the primary loop wipes anything
    # or moves any files. The list also drives whether the per-file TIA
    # stages run.
    input_dir = Path(os.environ["INPUT_DIR"])
    input_files: list[Path] = (
        ExcelToJsonConverter.list_customer_inputs(input_dir)
        if input_dir.is_dir() else []
    )

    primary = ExcelToJsonConverter(
        input_dir=input_dir,
        output_dir=Path(os.environ["INTERMEDIATE_JSON_DIR"]),
        processing_dir=Path(os.environ["PROCESSING_DIR"]),
        processed_dir=Path(os.environ["PROCESSED_DIR"]),
        # We wipe per-file via primary.wipe_output() inside the loop below,
        # not via convert_folder's batch wipe.
        clean_output_first=False,
    )

    logger.info("--- stage: extract reference info ---")
    rc_ref = extract_reference_info_stage()

    # Construct the RAG ingester once and share it across the passthrough
    # and sync stages — both hit /rag/* with the same auth.
    rag = RagIngester(
        base_url=os.environ["SSC_CLOUD_AIGATEWAY_BASE_URL"],
        api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
        llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
    )

    # Passthrough stage: non-Excel reference files (PDFs etc.) get
    # uploaded directly to RAG and moved to REFERENCE_LOADED_DIR. They
    # don't go through the Excel-only extraction pipeline.
    logger.info("--- stage: ingest reference passthrough ---")
    rc_passthrough = ingest_reference_passthrough_stage(rag)

    # RAG sync stage: ensure the RAG store mirrors the union of
    # `extracted_*.json` in REFERENCE_JSON_DIR and passthrough files
    # already in REFERENCE_LOADED_DIR (e.g. *.pdf). The ingester's
    # internal sync gate makes this a near-no-op when local and RAG
    # already match. When they differ, it deletes the stale RAG entries
    # and re-uploads from scratch.
    logger.info("--- stage: sync RAG with reference extractions ---")
    rc_rag = 0
    try:
        loaded_dir = Path(os.environ["REFERENCE_LOADED_DIR"])
        loaded_passthrough = sorted(
            p for pat in PASSTHROUGH_PATTERNS for p in loaded_dir.glob(pat)
        ) if loaded_dir.is_dir() else []
        rag.ingest_directory(
            Path(os.environ["REFERENCE_JSON_DIR"]),
            tags=["tia_reference"],
            glob_pattern="extracted_*.json",
            extra_files=loaded_passthrough,
        )
    except (RagGatewayError, *TRANSIENT_ERRORS) as exc:
        logger.error("RAG sync FAILED: %s", exc)
        rc_rag = 1
    except FileNotFoundError as exc:
        # REFERENCE_JSON_DIR doesn't exist yet (no reference has been
        # processed). Not fatal — the TIA stage will fail visibly if it
        # tries to use empty RAG state.
        logger.warning("RAG sync skipped: %s", exc)

    # Per-file primary + TIA stage. Each customer xlsx is processed end-to-end
    # in isolation: wipe INTERMEDIATE_JSON_DIR, convert ONLY this file, run
    # TIA on the freshly-isolated intermediate. This guarantees both that
    # INTERMEDIATE_JSON_DIR never holds data from more than one input file at
    # a time and that each TIA report is based on exactly one input file.
    rc_primary = 0
    rc_tia = 0
    intermediate_dir = Path(os.environ["INTERMEDIATE_JSON_DIR"])

    if not input_files:
        logger.info("--- stage: primary + TIA (skipped: no files in INPUT_DIR) ---")
    else:
        tia = TiaReportGenerator(
            base_url=os.environ["SSC_CLOUD_AIGATEWAY_BASE_URL"],
            api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
            llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
            output_dir=Path(os.environ["OUTPUT_REPORT_DIR"]),
            # The extracted per-answer criticality rubric — injected into the
            # analysis/verification calls so ratings are grounded in the
            # reference material rather than RAG retrieval luck.
            reference_guidance_dir=Path(os.environ["REFERENCE_JSON_DIR"]),
        )
        logger.info(
            "--- stage: primary + TIA per file (%d input file(s)) ---",
            len(input_files),
        )
        for i, source in enumerate(input_files, start=1):
            logger.info("--- file %d/%d: %s ---", i, len(input_files), source.name)

            # Report prefix (Booking ID for JSON inputs, stem otherwise) must
            # be computed BEFORE process_one — the claim step moves the file
            # out of INPUT_DIR, after which this path can no longer be read.
            prefix = report_prefix(source)

            # Wipe intermediate before this file's staging so it only holds
            # this file's artifacts.
            primary.wipe_output()
            if not primary.process_one(source):
                rc_primary = 1
                continue

            try:
                out_path = tia.generate(intermediate_dir, filename_prefix=prefix)
                logger.info("TIA report for %s: %s", source.name, out_path)
            except (TiaGenerationError, FileNotFoundError, *TRANSIENT_ERRORS) as exc:
                logger.error("TIA failed for %s: %s", source.name, exc)
                rc_tia = 1
                # Keep the source file in PROCESSING_DIR so the operator can
                # retry on the next scheduled run, instead of graduating it
                # to PROCESSED_DIR with no corresponding TIA report.
                primary.unmark_processed(source.name)
                logger.info(
                    "left %s in PROCESSING_DIR for retry (TIA failed)",
                    source.name,
                )

    # End-of-run finalize: move successfully-converted primary source files
    # from PROCESSING_DIR to PROCESSED_DIR. Files whose conversion failed
    # stay in PROCESSING_DIR for inspection.
    moved = primary.finalize_to_processed_dir()
    logger.info(
        "finalize: %d source file(s) moved to %s",
        moved, primary.processed_dir,
    )

    exit_code = rc_ref or rc_passthrough or rc_rag or rc_primary or rc_tia
    logger.info("=== run.py end (exit=%d) ===", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
