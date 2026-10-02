"""Tests for reference_info_extractor's superseded-workbook retirement.

The RAG sync gate matches on basename, so bumping a reference workbook's version
renames the file and leaves the previous one in REFERENCE_LOADED_DIR with nothing
to retire it. These cover the cleanup that fixes that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reference_info_extractor import (
    SUPERSEDED_DIR_NAME,
    _family,
    retire_superseded,
    wipe_stale_sheet_sources,
)


def test_stale_wipe_keeps_the_rubric(tmp_path: Path) -> None:
    """Clearing the previous workbook's sheets must NOT take the rubric with it.

    The converter's own clean_output_first wipe is all-or-nothing; using it here
    deleted `extracted_*.json` before the extraction that replaces them had run,
    so a mid-extraction failure left no rubric at all.
    """
    (tmp_path / "WB V1__SQL_Server.json").write_text("[]", encoding="utf-8")
    (tmp_path / "WB V1__Security.json").write_text("[]", encoding="utf-8")
    (tmp_path / "extracted_SQL_Server_20260101_000000.json").write_text(
        '{"keep": true}', encoding="utf-8")

    assert wipe_stale_sheet_sources(tmp_path) == 2
    survivors = sorted(p.name for p in tmp_path.glob("*.json"))
    assert survivors == ["extracted_SQL_Server_20260101_000000.json"]
    assert (tmp_path / survivors[0]).read_text(encoding="utf-8") == '{"keep": true}'


def test_stale_wipe_on_an_empty_or_missing_dir_is_safe(tmp_path: Path) -> None:
    assert wipe_stale_sheet_sources(tmp_path / "does_not_exist") == 0
    assert wipe_stale_sheet_sources(tmp_path) == 0


def _loaded(tmp_path: Path, *names: str) -> Path:
    d = tmp_path / "loaded"
    d.mkdir(exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"x")
    return d


@pytest.mark.parametrize("stem, expected", [
    ("Technical Infrastructure Assessment V2.6.5", "technical infrastructure assessment"),
    ("Technical Infrastructure Assessment V2.6.4", "technical infrastructure assessment"),
    ("Technical Infrastructure Assessment v2.6", "technical infrastructure assessment"),
    ("Technical Infrastructure Assessment 2.6.5", "technical infrastructure assessment"),
    ("Some Workbook", "some workbook"),          # no version token
    ("Report V2 Final", "report v2 final"),      # version not at the end -> untouched
])
def test_family_strips_a_trailing_version_token(stem: str, expected: str) -> None:
    assert _family(stem) == expected


def test_retires_the_previous_version(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path,
                     "Technical Infrastructure Assessment V2.6.4.xlsm",
                     "Technical Infrastructure Assessment V2.6.5.xlsm")
    retired = retire_superseded(
        loaded, [loaded / "Technical Infrastructure Assessment V2.6.5.xlsm"])

    assert retired == 1
    assert (loaded / "Technical Infrastructure Assessment V2.6.5.xlsm").exists()
    assert not (loaded / "Technical Infrastructure Assessment V2.6.4.xlsm").exists()
    # Moved, not deleted — the retention policy is to keep everything.
    assert (loaded / SUPERSEDED_DIR_NAME /
            "Technical Infrastructure Assessment V2.6.4.xlsm").exists()


def test_leaves_unrelated_workbooks_alone(tmp_path: Path) -> None:
    loaded = _loaded(tmp_path, "Sizing Guide V1.0.xlsm",
                     "Technical Infrastructure Assessment V2.6.5.xlsm")
    assert retire_superseded(
        loaded, [loaded / "Technical Infrastructure Assessment V2.6.5.xlsm"]) == 0
    assert (loaded / "Sizing Guide V1.0.xlsm").exists()


def test_reingesting_the_same_version_retires_nothing(tmp_path: Path) -> None:
    """Re-dropping the identical file must not retire the copy it just replaced."""
    loaded = _loaded(tmp_path, "Technical Infrastructure Assessment V2.6.5.xlsm")
    assert retire_superseded(
        loaded, [loaded / "Technical Infrastructure Assessment V2.6.5.xlsm"]) == 0
    assert (loaded / "Technical Infrastructure Assessment V2.6.5.xlsm").exists()
    assert not (loaded / SUPERSEDED_DIR_NAME).exists()


def test_retired_files_are_hidden_from_the_passthrough_scan(tmp_path: Path) -> None:
    """run.py globs REFERENCE_LOADED_DIR non-recursively for passthrough files,
    so a retired workbook in the subfolder can never reach RAG."""
    loaded = _loaded(tmp_path, "Ref V1.0.xlsm", "Ref V2.0.xlsm")
    retire_superseded(loaded, [loaded / "Ref V2.0.xlsm"])
    assert sorted(p.name for p in loaded.glob("*.xls[xm]")) == ["Ref V2.0.xlsm"]
