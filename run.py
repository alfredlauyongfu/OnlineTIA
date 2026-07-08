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

import json
import logging
import os
import sys
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


def report_prefix(source: Path) -> str:
    """Filename prefix for the TIA report generated from `source`.

    JSON form exports are named from their "Booking ID" field
    (`TIA_<bookingid>`); anything else — Excel inputs, a missing/non-string
    field, an unreadable file — falls back to the sanitized file stem.
    Never raises: report naming must not be able to fail the run.
    """
    if source.suffix.lower() == ".json":
        try:
            with source.open("r", encoding="utf-8-sig") as f:
                booking_id = json.load(f).get("Booking ID")
            if isinstance(booking_id, str) and booking_id.strip():
                return f"TIA_{ExcelToJsonConverter.safe_name(booking_id.strip())}"
        except Exception:
            pass  # fall through to the stem rule
    return f"TIA_{ExcelToJsonConverter.safe_name(source.stem)}"


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
