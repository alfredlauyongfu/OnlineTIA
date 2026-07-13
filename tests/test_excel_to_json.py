"""Tests for excel_to_json.ExcelToJsonConverter.

Fixtures use openpyxl to build small workbooks in memory (per the project's
no-pre-committed-test-fixture convention), then exercise the class against
pytest's tmp_path filesystem.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pytest
from openpyxl import Workbook

import excel_to_json
from excel_to_json import ExcelToJsonConverter, read_json_object


# ---------- safe_name ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("simple", "simple"),
        ("with space", "with_space"),
        ("Orders (Q1)", "Orders_Q1"),
        ("a/b\\c", "a_b_c"),
        ("UPPER123", "UPPER123"),
        ("trailing___", "trailing"),
        ("___leading", "leading"),
        ("", "sheet"),                  # empty falls back to "sheet"
        ("!!!", "sheet"),               # all-bad chars fall back to "sheet"
        ("a.b-c_d", "a.b-c_d"),         # dot/dash/underscore preserved
    ],
)
def test_safe_name(raw: str, expected: str) -> None:
    assert ExcelToJsonConverter.safe_name(raw) == expected


# ---------- _json_value (date serialization) ----------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("hello", "hello"),
        (42, 42),
        (3.14, 3.14),
        (None, None),
        (True, True),
        (dt.date(2026, 5, 24), "2026-05-24"),
        (dt.datetime(2026, 5, 24, 12, 30, 45), "2026-05-24T12:30:45"),
        (dt.time(9, 0, 0), "09:00:00"),
    ],
)
def test_json_value_serialization(value, expected) -> None:
    assert ExcelToJsonConverter._json_value(value) == expected


# ---------- in-memory workbook helper ----------

def _make_workbook(tmp_path: Path, name: str, sheets: dict[str, list[list]]) -> Path:
    """Build a small .xlsx at tmp_path/name with the given sheets.
    `sheets` maps sheet_name -> list of rows; each row is a list of cell values.
    """
    wb = Workbook()
    # Remove the default sheet so we get exactly what's in `sheets`.
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


# ---------- convert_workbook + _sheet_to_records ----------

def test_convert_workbook_basic_shape(tmp_path: Path) -> None:
    xlsx = _make_workbook(tmp_path, "wb.xlsx", {
        "People": [
            ["name", "age"],
            ["Alice", 30],
            ["Bob", 25],
        ],
    })
    conv = ExcelToJsonConverter(input_dir=tmp_path, output_dir=tmp_path / "out")
    data = conv.convert_workbook(xlsx)
    assert list(data.keys()) == ["People"]
    assert data["People"] == [
        {"name": "Alice", "age": 30},
        {"name": "Bob", "age": 25},
    ]


def test_sheet_skips_blank_rows_and_blank_columns(tmp_path: Path) -> None:
    xlsx = _make_workbook(tmp_path, "wb.xlsx", {
        "S": [
            [None, None, None],       # leading blank row
            ["col_a", None, "col_c"], # header with a None column → becomes "column_2"
            ["", None, ""],           # all-blank → skipped
            ["x", None, "y"],         # col_a=x, column_2=None (skipped), col_c=y
            ["", None, "z"],          # col_a="" (skipped), col_c=z
        ],
    })
    conv = ExcelToJsonConverter(input_dir=tmp_path, output_dir=tmp_path / "out")
    data = conv.convert_workbook(xlsx)
    records = data["S"]
    # 2 non-blank rows after the header; blank values pruned per-cell.
    assert records == [
        {"col_a": "x", "col_c": "y"},
        {"col_c": "z"},
    ]


def test_convert_workbook_isoformats_dates(tmp_path: Path) -> None:
    xlsx = _make_workbook(tmp_path, "wb.xlsx", {
        "Dates": [
            ["k", "when"],
            ["a", dt.datetime(2026, 5, 24, 9, 0, 0)],
        ],
    })
    conv = ExcelToJsonConverter(input_dir=tmp_path, output_dir=tmp_path / "out")
    data = conv.convert_workbook(xlsx)
    assert data["Dates"] == [{"k": "a", "when": "2026-05-24T09:00:00"}]


# ---------- wipe_output ----------

def test_wipe_output_removes_top_level_files_only(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.json").write_text("{}")
    (out / "b.json").write_text("{}")
    (out / "subdir").mkdir()
    (out / "subdir" / "kept.json").write_text("{}")
    conv = ExcelToJsonConverter(input_dir=tmp_path, output_dir=out)
    wiped = conv.wipe_output()
    assert wiped == 2
    assert not (out / "a.json").exists()
    assert not (out / "b.json").exists()
    assert (out / "subdir" / "kept.json").exists()  # subdirs left alone


def test_wipe_output_creates_missing_dir(tmp_path: Path) -> None:
    out = tmp_path / "does_not_exist_yet"
    conv = ExcelToJsonConverter(input_dir=tmp_path, output_dir=out)
    wiped = conv.wipe_output()
    assert wiped == 0
    assert out.is_dir()


# ---------- process_one + finalize_to_processed_dir lifecycle ----------

def test_process_one_full_lifecycle(tmp_path: Path) -> None:
    """End-to-end per-file path: claim → convert → write JSONs → track →
    finalize moves to processed."""
    inbox = tmp_path / "inbox"
    proc = tmp_path / "processing"
    done = tmp_path / "processed"
    out = tmp_path / "out"
    inbox.mkdir()

    xlsx = _make_workbook(inbox, "ws.xlsx", {
        "People": [["name"], ["Alice"]],
        "Orders (Q1)": [["id"], [1]],
    })

    conv = ExcelToJsonConverter(
        input_dir=inbox,
        output_dir=out,
        processing_dir=proc,
        processed_dir=done,
    )
    ok = conv.process_one(xlsx)
    assert ok is True
    # Source moved to processing.
    assert not (inbox / "ws.xlsx").exists()
    assert (proc / "ws.xlsx").exists()
    # Per-sheet JSONs written with safe_name applied to sheet name.
    assert (out / "ws__People.json").exists()
    assert (out / "ws__Orders_Q1.json").exists()
    # Not yet graduated to processed.
    assert not (done / "ws.xlsx").exists()
    assert conv.successfully_processed_paths == [proc / "ws.xlsx"]

    moved = conv.finalize_to_processed_dir()
    assert moved == 1
    assert not (proc / "ws.xlsx").exists()
    assert (done / "ws.xlsx").exists()
    # Tracking list cleared after finalize.
    assert conv.successfully_processed_paths == []


def test_process_one_failure_does_not_track(tmp_path: Path) -> None:
    """A file that fails to convert is NOT added to
    successfully_processed_paths, so finalize won't graduate it."""
    inbox = tmp_path / "inbox"
    proc = tmp_path / "processing"
    done = tmp_path / "processed"
    out = tmp_path / "out"
    inbox.mkdir()
    # Not a real xlsx — openpyxl will reject it.
    bad = inbox / "not_an_xlsx.xlsx"
    bad.write_text("this is not a workbook")

    conv = ExcelToJsonConverter(
        input_dir=inbox, output_dir=out,
        processing_dir=proc, processed_dir=done,
    )
    ok = conv.process_one(bad)
    assert ok is False
    assert conv.successfully_processed_paths == []
    moved = conv.finalize_to_processed_dir()
    assert moved == 0


def test_unmark_processed_keeps_file_in_processing(tmp_path: Path) -> None:
    """After a downstream-stage failure (e.g. TIA failed for this file), the
    caller calls `unmark_processed(name)` to drop it from the finalize list.
    finalize then doesn't graduate it to processed, and the file stays in
    processing for the next-run retry."""
    inbox = tmp_path / "inbox"
    proc = tmp_path / "processing"
    done = tmp_path / "processed"
    out = tmp_path / "out"
    inbox.mkdir()
    xlsx = _make_workbook(inbox, "ws.xlsx", {"S": [["k"], ["v"]]})

    conv = ExcelToJsonConverter(
        input_dir=inbox, output_dir=out,
        processing_dir=proc, processed_dir=done,
    )
    assert conv.process_one(xlsx) is True
    assert conv.successfully_processed_paths == [proc / "ws.xlsx"]

    removed = conv.unmark_processed("ws.xlsx")
    assert removed == 1
    assert conv.successfully_processed_paths == []

    moved = conv.finalize_to_processed_dir()
    assert moved == 0
    # File stayed in processing for retry, did not graduate.
    assert (proc / "ws.xlsx").exists()
    assert not (done / "ws.xlsx").exists()


def test_unmark_processed_unknown_name_is_noop(tmp_path: Path) -> None:
    """Asking to unmark a file that isn't tracked returns 0 and leaves the
    rest alone."""
    inbox = tmp_path / "inbox"
    proc = tmp_path / "processing"
    done = tmp_path / "processed"
    out = tmp_path / "out"
    inbox.mkdir()
    xlsx = _make_workbook(inbox, "kept.xlsx", {"S": [["k"], ["v"]]})

    conv = ExcelToJsonConverter(
        input_dir=inbox, output_dir=out,
        processing_dir=proc, processed_dir=done,
    )
    conv.process_one(xlsx)
    assert conv.successfully_processed_paths == [proc / "kept.xlsx"]

    removed = conv.unmark_processed("does_not_exist.xlsx")
    assert removed == 0
    assert conv.successfully_processed_paths == [proc / "kept.xlsx"]


def test_process_one_in_place_mode_no_move(tmp_path: Path) -> None:
    """When processing_dir/processed_dir are omitted, source stays put."""
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    xlsx = _make_workbook(inbox, "ws.xlsx", {"S": [["k"], ["v"]]})

    conv = ExcelToJsonConverter(input_dir=inbox, output_dir=out)
    ok = conv.process_one(xlsx)
    assert ok is True
    assert xlsx.exists()                         # source not moved
    assert (out / "ws__S.json").exists()
    assert conv.successfully_processed_paths == []  # only populated in move mode


# ---------- convert_folder (batch) ----------

def test_convert_folder_processes_all_xlsx_and_skips_lockfiles(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    _make_workbook(inbox, "a.xlsx", {"S": [["k"], ["a"]]})
    _make_workbook(inbox, "b.xlsm", {"S": [["k"], ["b"]]})
    # Excel-style temp lockfile — must be skipped.
    (inbox / "~$tempfile.xlsx").write_text("lock")
    # Non-xlsx file — not matched by the glob.
    (inbox / "readme.txt").write_text("hi")

    conv = ExcelToJsonConverter(input_dir=inbox, output_dir=out)
    rc = conv.convert_folder()
    assert rc == 0
    written = sorted(p.name for p in out.iterdir() if p.is_file())
    assert written == ["a__S.json", "b__S.json"]


def test_convert_folder_empty_input_returns_zero(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    out = tmp_path / "out"
    conv = ExcelToJsonConverter(input_dir=inbox, output_dir=out)
    assert conv.convert_folder() == 0


def test_convert_folder_missing_input_returns_one(tmp_path: Path) -> None:
    inbox = tmp_path / "does_not_exist"
    out = tmp_path / "out"
    conv = ExcelToJsonConverter(input_dir=inbox, output_dir=out)
    assert conv.convert_folder() == 1


# ---------- ctor validation ----------

def test_processing_processed_must_be_both_or_neither(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ExcelToJsonConverter(
            input_dir=tmp_path,
            output_dir=tmp_path / "out",
            processing_dir=tmp_path / "proc",
            # processed_dir intentionally omitted
        )


def test_json_output_is_valid_json(tmp_path: Path) -> None:
    """The written sheet files round-trip through json.load."""
    inbox = tmp_path / "inbox"
    out = tmp_path / "out"
    inbox.mkdir()
    xlsx = _make_workbook(inbox, "ws.xlsx", {
        "S": [["name", "qty"], ["widget", 5], ["gadget", 12]],
    })
    conv = ExcelToJsonConverter(input_dir=inbox, output_dir=out)
    assert conv.process_one(xlsx)
    payload = json.loads((out / "ws__S.json").read_text(encoding="utf-8"))
    assert payload == [
        {"name": "widget", "qty": 5},
        {"name": "gadget", "qty": 12},
    ]


# ---------- JSON form-export inputs ----------

def _make_lifecycle_converter(tmp_path: Path) -> tuple[ExcelToJsonConverter, Path, Path, Path]:
    """Converter in move-mode with fresh inbox/processing/processed/out dirs.
    Returns (converter, inbox, processing, out)."""
    inbox = tmp_path / "inbox"
    proc = tmp_path / "processing"
    out = tmp_path / "out"
    inbox.mkdir()
    conv = ExcelToJsonConverter(
        input_dir=inbox, output_dir=out,
        processing_dir=proc, processed_dir=tmp_path / "processed",
    )
    return conv, inbox, proc, out


def test_list_customer_inputs_covers_both_formats(tmp_path: Path) -> None:
    """Excel AND JSON inputs are listed (sorted); Office lock files are not."""
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.xlsx").write_bytes(b"x")
    (tmp_path / "c.xlsm").write_bytes(b"x")
    (tmp_path / "~$a.xlsx").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignored")
    names = [p.name for p in ExcelToJsonConverter.list_customer_inputs(tmp_path)]
    assert names == ["a.xlsx", "b.json", "c.xlsm"]


def test_process_one_stages_valid_json(tmp_path: Path) -> None:
    """A JSON form export is claimed, validated, re-serialized into
    output_dir, and recorded for finalize — same lifecycle as a workbook."""
    conv, inbox, proc, out = _make_lifecycle_converter(tmp_path)
    src = inbox / "TIA_id1 20260704.json"
    src.write_text(json.dumps({"Booking ID": "id1", "Q": "A"}), encoding="utf-8")

    assert conv.process_one(src) is True
    assert not src.exists()                                  # claimed
    assert (proc / src.name).exists()
    assert conv.successfully_processed_paths == [proc / src.name]
    staged = json.loads((out / src.name).read_text(encoding="utf-8"))
    assert staged == {"Booking ID": "id1", "Q": "A"}


def test_process_one_stages_bom_json_as_plain_utf8(tmp_path: Path) -> None:
    """A UTF-8-BOM form export (common from web exports) stages successfully,
    and the staged copy parses with PLAIN utf-8 — the contract the TIA
    generator's reader relies on."""
    conv, inbox, _, out = _make_lifecycle_converter(tmp_path)
    src = inbox / "resp.json"
    src.write_bytes(b"\xef\xbb\xbf" + json.dumps({"Q": "Ä"}).encode("utf-8"))

    assert conv.process_one(src) is True
    with (out / "resp.json").open("r", encoding="utf-8") as f:  # no -sig
        assert json.load(f) == {"Q": "Ä"}


def test_process_one_rejects_unparseable_json(tmp_path: Path) -> None:
    """A file that doesn't parse stays in PROCESSING (like a bad workbook):
    False returned, not tracked, nothing staged."""
    conv, inbox, proc, out = _make_lifecycle_converter(tmp_path)
    src = inbox / "bad.json"
    src.write_text("not json {", encoding="utf-8")

    assert conv.process_one(src) is False
    assert (proc / "bad.json").exists()                      # left for inspection
    assert conv.successfully_processed_paths == []
    assert not (out / "bad.json").exists()


def test_process_one_rejects_non_object_json(tmp_path: Path) -> None:
    """Parses, but isn't a JSON object → rejected the same way."""
    conv, inbox, proc, out = _make_lifecycle_converter(tmp_path)
    src = inbox / "list.json"
    src.write_text("[1, 2, 3]", encoding="utf-8")

    assert conv.process_one(src) is False
    assert (proc / "list.json").exists()
    assert not (out / "list.json").exists()


# ---------- read_json_object: OneDrive placeholder / retry robustness ----------

class _FakePath:
    """Minimal Path stand-in for read_json_object: it only calls .open(),
    .stat() and .name. `open_results` is consumed one entry per open() call —
    an Exception instance is raised, anything else is returned as the handle.
    `stat_attrs` (or None) is exposed as st_file_attributes on stat()."""

    def __init__(self, name="resp.json", open_results=(), stat_attrs=None):
        self.name = name
        self._open_results = list(open_results)
        self._stat_attrs = stat_attrs

    def open(self, *args, **kwargs):
        result = self._open_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stat(self):
        st = type("_St", (), {})()
        if self._stat_attrs is not None:
            st.st_file_attributes = self._stat_attrs
        return st


def test_read_json_object_reads_valid_object(tmp_path: Path) -> None:
    src = tmp_path / "ok.json"
    src.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert read_json_object(src) == {"a": 1}


def test_read_json_object_rejects_non_object() -> None:
    p = _FakePath(open_results=[io.StringIO("[1, 2, 3]")])
    with pytest.raises(ValueError):
        read_json_object(p)  # type: ignore[arg-type]


def test_read_json_object_placeholder_raises_actionable_hint() -> None:
    """A read that fails on an online-only placeholder is turned into an
    actionable OSError instead of the opaque [Errno 22], with no retry."""
    err = OSError(22, "Invalid argument")
    p = _FakePath(open_results=[err], stat_attrs=0x1000)  # FILE_ATTRIBUTE_OFFLINE
    with pytest.raises(OSError) as ei:
        read_json_object(p)  # type: ignore[arg-type]
    msg = str(ei.value)
    assert "online-only placeholder" in msg
    assert "Always keep on this device" in msg


def test_read_json_object_retries_transient_then_succeeds(monkeypatch) -> None:
    """A transient (non-placeholder) OSError is retried and then succeeds."""
    monkeypatch.setattr(excel_to_json, "_READ_RETRY_DELAY", 0)
    p = _FakePath(
        open_results=[OSError(22, "Invalid argument"), io.StringIO('{"ok": true}')],
        stat_attrs=None,  # not a placeholder → eligible for retry
    )
    assert read_json_object(p) == {"ok": True}  # type: ignore[arg-type]


def test_read_json_object_reraises_after_exhausting_retries(monkeypatch) -> None:
    monkeypatch.setattr(excel_to_json, "_READ_RETRY_DELAY", 0)
    p = _FakePath(
        open_results=[OSError(22, "x")] * excel_to_json._READ_RETRIES,
        stat_attrs=None,
    )
    with pytest.raises(OSError):
        read_json_object(p)  # type: ignore[arg-type]
