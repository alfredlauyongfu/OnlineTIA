"""Smoke test: every project module imports without side-effects.

Catches missing imports, syntax errors, undeclared references at the
cheapest possible level — no network, no env, no filesystem.
"""

from __future__ import annotations


def test_import_excel_to_json() -> None:
    import excel_to_json  # noqa: F401


def test_import_logging_setup() -> None:
    import logging_setup  # noqa: F401


def test_import_reference_sheet_extractor() -> None:
    import reference_sheet_extractor  # noqa: F401


def test_import_reference_info_extractor() -> None:
    import reference_info_extractor  # noqa: F401


def test_import_reference_passthrough_ingester() -> None:
    import reference_passthrough_ingester  # noqa: F401


def test_import_rag_ingester() -> None:
    import rag_ingester  # noqa: F401


def test_import_tia_generator() -> None:
    import tia_generator  # noqa: F401


def test_import_run() -> None:
    import run  # noqa: F401


def test_run_required_vars_cover_every_stage_var() -> None:
    """run.py is the single entry point and the only bootstrap: every env var
    any stage dereferences must be in its REQUIRED_VARS, else a missing var
    surfaces as a KeyError traceback mid-run instead of bootstrap's clean
    exit-2 (and LOG_DIR must be present for logging-config itself)."""
    import run

    stage_vars = {
        # bootstrap / logging
        "LOG_DIR",
        # gateway auth (extraction + RAG + TIA stages)
        "SSC_CLOUD_AIGATEWAY_BASE_URL",
        "SSC_CLOUD_AIGATEWAY_API_KEY",
        "SSC_CLOUD_AIGATEWAY_USER_ID",
        "SSC_CLOUD_AIGATEWAY_MODEL",
        "USE_CASE_ID",
        # reference pipeline dirs
        "REFERENCE_TO_BE_LOADED_DIR",
        "REFERENCE_LOADED_DIR",
        "REFERENCE_JSON_DIR",
        # primary + TIA pipeline dirs
        "INPUT_DIR",
        "INTERMEDIATE_JSON_DIR",
        "PROCESSING_DIR",
        "PROCESSED_DIR",
        "OUTPUT_REPORT_DIR",
    }
    assert stage_vars <= set(run.REQUIRED_VARS)
