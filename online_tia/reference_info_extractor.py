"""Stage: full reference-info extraction pipeline.

  0. **Inbox guard**: if REFERENCE_EXCEL_TO_BE_LOADED_DIR is empty, skip
     stages 1 and 2 entirely (REFERENCE_JSON_DIR and
     REFERENCE_JSON_EXTRACTED_DIR are left untouched). Otherwise:
  1. Convert .xlsx/.xlsm files in REFERENCE_EXCEL_TO_BE_LOADED_DIR to
     per-sheet JSON in REFERENCE_JSON_DIR (wiped first). Successfully
     converted xlsx files are then moved to REFERENCE_EXCEL_LOADED_DIR.
  2. Run a per-sheet LLM extraction over REFERENCE_JSON_DIR, writing one
     `extracted_{sheet}_{YYYYMMDD_HHMMSS}.json` per non-empty result to
     REFERENCE_JSON_EXTRACTED_DIR (wiped first).

Downstream consumers push the extracted files into the RAG service via
`rag_ingester.RagIngester` (out of scope for this module).

Can be run directly (python online_tia\reference_info_extractor.py) for
isolated testing, or imported by run.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from excel_to_json import ExcelToJsonConverter
from logging_setup import bootstrap
from reference_json_combiner import ReferenceJsonCombiner


REQUIRED_ENV_VARS = (
    "SSC_CLOUD_AIGATEWAY_BASE_URL",
    "SSC_CLOUD_AIGATEWAY_API_KEY",
    "SSC_CLOUD_AIGATEWAY_USER_ID",
    "SSC_CLOUD_AIGATEWAY_MODEL",
    "USE_CASE_ID",
    "REFERENCE_EXCEL_TO_BE_LOADED_DIR",
    "REFERENCE_EXCEL_LOADED_DIR",
    "REFERENCE_JSON_DIR",
    "REFERENCE_JSON_EXTRACTED_DIR",
    "LOG_DIR",
)


logger = logging.getLogger(__name__)


def extract() -> int:
    rc = bootstrap(REQUIRED_ENV_VARS)
    if rc is not None:
        return rc

    reference_json_dir = Path(os.environ["REFERENCE_JSON_DIR"])
    to_be_loaded_dir = Path(os.environ["REFERENCE_EXCEL_TO_BE_LOADED_DIR"])
    loaded_dir = Path(os.environ["REFERENCE_EXCEL_LOADED_DIR"])

    # Inbox guard: if nothing waiting, skip both stages — leave the JSON and
    # extracted dirs untouched so the downstream RAG sync gate sees no
    # changes and stays idempotent.
    incoming = sorted(
        p for p in to_be_loaded_dir.glob("*.xls[xm]")
        if not p.name.startswith("~$")
    )
    if not incoming:
        logger.info(
            "No xlsx in %s; skipping convert and extract stages "
            "(REFERENCE_JSON_DIR and REFERENCE_JSON_EXTRACTED_DIR unchanged)",
            to_be_loaded_dir,
        )
        return 0

    # Stage 1: Excel -> per-sheet JSON. Configure the converter with
    # processing_dir = input dir (claim-rename becomes a no-op via the
    # converter's same-path guard), processed_dir = loaded_dir. After both
    # stages run, finalize_to_processed_dir() moves successfully-converted
    # xlsx from inbox to loaded; failed conversions remain in the inbox.
    converter = ExcelToJsonConverter(
        input_dir=to_be_loaded_dir,
        output_dir=reference_json_dir,
        processing_dir=to_be_loaded_dir,
        processed_dir=loaded_dir,
        clean_output_first=True,
    )
    logger.info(
        "-- stage: convert reference excel (%d xlsx in inbox) --", len(incoming)
    )
    rc_convert = converter.convert_folder()

    # Stage 2: LLM extraction.
    combiner = ReferenceJsonCombiner(
        api_url=os.environ["SSC_CLOUD_AIGATEWAY_BASE_URL"],
        api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
        user_id=os.environ["SSC_CLOUD_AIGATEWAY_USER_ID"],
        use_case_id=os.environ["USE_CASE_ID"],
        model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
        reference_json_dir=reference_json_dir,
        extracted_dir=Path(os.environ["REFERENCE_JSON_EXTRACTED_DIR"]),
    )

    logger.info("-- stage: extract reference json --")
    # Config summary stays as plain prints — it's per-run operator context,
    # not part of the persistent operational trail.
    print(f"  source dir   : {combiner.reference_json_dir}")
    print(f"  extracted dir: {combiner.extracted_dir}")
    print(f"  model        : {combiner.model}")
    print(f"  endpoint     : {combiner.api_url}/chat/completions")

    rc_combine = combiner.combine()
    logger.info("extract stage finished (rc=%d)", rc_combine)

    # Finalize: move successfully-converted xlsx from inbox to loaded.
    moved = converter.finalize_to_processed_dir()
    logger.info("moved %d xlsx to %s", moved, loaded_dir)

    return rc_convert or rc_combine


if __name__ == "__main__":
    print("=== reference_info_extractor (standalone) ===")
    raise SystemExit(extract())
