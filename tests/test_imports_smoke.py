"""Smoke test: every project module imports without side-effects.

Catches missing imports, syntax errors, undeclared references at the
cheapest possible level — no network, no env, no filesystem.
"""

from __future__ import annotations


def test_import_excel_to_json() -> None:
    import excel_to_json  # noqa: F401


def test_import_logging_setup() -> None:
    import logging_setup  # noqa: F401


def test_import_reference_json_combiner() -> None:
    import reference_json_combiner  # noqa: F401


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


def test_required_vars_all_include_log_dir() -> None:
    """Bootstrap requires LOG_DIR — every entry point's REQUIRED_VARS
    tuple must include it, else logging-config will KeyError at runtime."""
    import run
    import reference_info_extractor
    import reference_passthrough_ingester
    import rag_ingester
    import tia_generator

    assert "LOG_DIR" in run.REQUIRED_VARS
    assert "LOG_DIR" in reference_info_extractor.REQUIRED_ENV_VARS
    assert "LOG_DIR" in reference_passthrough_ingester.REQUIRED_ENV_VARS
    assert "LOG_DIR" in rag_ingester.REQUIRED_ENV_VARS
    assert "LOG_DIR" in tia_generator.REQUIRED_ENV_VARS
