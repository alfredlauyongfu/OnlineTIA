"""CLI entry point: run the reference pipeline first (so any new reference
xlsx gets vectorized into RAG-ready extractions before customer data is
processed), sync RAG with the latest local extractions, then process every
.xlsx/.xlsm file in INPUT_DIR **independently and in sequence** — each
customer file is converted to JSON, then a Technical Infrastructure
Assessment (TIA) report is generated for that ONE file's data only, then
the loop moves on to the next customer file. INTERMEDIATE_JSON_DIR is
wiped between files so it never holds content from more than one input
file at a time, and each TIA report is based on exactly one input file.

Paths owned by this file (read from .env, must be absolute):
  INPUT_DIR              - primary input; files move through PROCESSING_DIR to PROCESSED_DIR
  INTERMEDIATE_JSON_DIR  - where primary JSON output is written
  PROCESSING_DIR         - staging folder for in-flight primary files
  PROCESSED_DIR          - where successfully converted primary files land
  OUTPUT_REPORT_DIR      - where the generated TIA Markdown report is written

The reference stage env vars (REFERENCE_TO_BE_LOADED_DIR / ...) plus
the AIGateway chat-completions auth (SSC_CLOUD_AIGATEWAY_USER_ID, USE_CASE_ID,
SSC_CLOUD_AIGATEWAY_BASE_URL) are validated inside reference_info_extractor.py.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Add the sibling source dir to sys.path so the project's modules can be
# imported directly (e.g. `from excel_to_json import ...`). Keeps the module
# files cleanly tucked into online_tia/ while run.py stays at the project
# root as the single entry point.
sys.path.insert(0, str(Path(__file__).resolve().parent / "online_tia"))

import requests

from excel_to_json import ExcelToJsonConverter
from logging_setup import bootstrap
from rag_ingester import RagIngester, RagGatewayError
from reference_info_extractor import extract as extract_reference_info_stage
from tia_generator import TiaReportGenerator, TiaGenerationError


REQUIRED_VARS = (
    "INPUT_DIR",
    "INTERMEDIATE_JSON_DIR",
    "PROCESSING_DIR",
    "PROCESSED_DIR",
    "REFERENCE_JSON_EXTRACTED_DIR",
    "OUTPUT_REPORT_DIR",
    "SSC_CLOUD_RAG_BASE_URL",
    "SSC_CLOUD_AIGATEWAY_API_KEY",
    "SSC_CLOUD_AIGATEWAY_MODEL",
    "LOG_DIR",
)


logger = logging.getLogger(__name__)


def main() -> int:
    rc = bootstrap(REQUIRED_VARS)
    if rc is not None:
        return rc
    logger.info("=== run.py start ===")

    # Snapshot the INPUT_DIR contents BEFORE the primary loop wipes anything
    # or moves any files. The list also drives whether the per-file TIA
    # stages run.
    input_dir = Path(os.environ["INPUT_DIR"])
    input_files: list[Path] = sorted(
        p for p in input_dir.glob("*.xls[xm]")
        if not p.name.startswith("~$")
    ) if input_dir.is_dir() else []

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

    # RAG sync stage: ensure the RAG store mirrors REFERENCE_JSON_EXTRACTED_DIR.
    # The ingester's internal sync gate makes this a near-no-op when local
    # and RAG already match (one listfiles call, no register/upload/delete).
    # When they differ (e.g., the reference stage just produced new
    # timestamped files), it deletes the stale RAG entries and re-uploads.
    logger.info("--- stage: sync RAG with reference extractions ---")
    rc_rag = 0
    try:
        rag = RagIngester(
            base_url=os.environ["SSC_CLOUD_RAG_BASE_URL"],
            api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
            llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
        )
        rag.ingest_directory(
            Path(os.environ["REFERENCE_JSON_EXTRACTED_DIR"]),
            tags=["tia_reference"],
        )
    except (
        RagGatewayError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ) as exc:
        logger.error("RAG sync FAILED: %s", exc)
        rc_rag = 1
    except FileNotFoundError as exc:
        # REFERENCE_JSON_EXTRACTED_DIR doesn't exist yet (no reference has
        # been processed). Not fatal — the TIA stage will fail visibly if it
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
            base_url=os.environ["SSC_CLOUD_RAG_BASE_URL"],
            api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
            llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
            output_dir=Path(os.environ["OUTPUT_REPORT_DIR"]),
        )
        logger.info(
            "--- stage: primary + TIA per file (%d input file(s)) ---",
            len(input_files),
        )
        for i, xlsx in enumerate(input_files, start=1):
            logger.info("--- file %d/%d: %s ---", i, len(input_files), xlsx.name)

            # Wipe intermediate before this file's conversion so it only holds
            # this file's sheet JSONs.
            primary.wipe_output()
            if not primary.process_one(xlsx):
                rc_primary = 1
                continue

            # TIA report for this single file. Include the source stem in the
            # output filename so per-file reports don't collide.
            safe_stem = ExcelToJsonConverter.safe_name(xlsx.stem)
            try:
                out_path = tia.generate(
                    intermediate_dir,
                    filename_prefix=f"TIA_{safe_stem}",
                )
                logger.info("TIA report for %s: %s", xlsx.name, out_path)
            except (
                TiaGenerationError,
                FileNotFoundError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                logger.error("TIA failed for %s: %s", xlsx.name, exc)
                rc_tia = 1

    # End-of-run finalize: move successfully-converted primary source files
    # from PROCESSING_DIR to PROCESSED_DIR. Files whose conversion failed
    # stay in PROCESSING_DIR for inspection.
    moved = primary.finalize_to_processed_dir()
    logger.info(
        "finalize: %d source file(s) moved to %s",
        moved, primary.processed_dir,
    )

    exit_code = rc_ref or rc_rag or rc_primary or rc_tia
    logger.info("=== run.py end (exit=%d) ===", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
