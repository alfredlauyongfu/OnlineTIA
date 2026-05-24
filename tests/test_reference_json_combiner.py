"""Tests for reference_json_combiner pure-logic helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from reference_json_combiner import ReferenceJsonCombiner


@pytest.mark.parametrize(
    "filename, expected",
    [
        # Files produced by ExcelToJsonConverter use `__` between workbook
        # stem and sheet name; the combiner should pull out the sheet part.
        ("Technical Infrastructure Assessment V2.6.4__SQL_Server.json", "SQL_Server"),
        ("foo__bar.json", "bar"),
        ("plain_no_separator.json", "plain_no_separator"),
        ("a__b__c.json", "b__c"),   # only split on the FIRST `__`
        ("__leading_sep.json", "leading_sep"),
    ],
)
def test_sheet_name_from_filename(filename: str, expected: str) -> None:
    assert ReferenceJsonCombiner._sheet_name_from_filename(Path(filename)) == expected
