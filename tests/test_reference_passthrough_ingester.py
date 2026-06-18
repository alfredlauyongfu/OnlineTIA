"""Tests for reference_passthrough_ingester. Uses a stub RagIngester
to avoid hitting the network."""

from __future__ import annotations

from pathlib import Path

import pytest

import reference_passthrough_ingester as rpi
from rag_ingester import RagGatewayError


class _StubRag:
    """Minimal RagIngester stand-in. Records the calls."""

    def __init__(self, listing=None, ingest_error: Exception | None = None,
                 delete_error: Exception | None = None) -> None:
        self._listing = listing or []
        self._ingest_error = ingest_error
        self._delete_error = delete_error
        self.list_calls = 0
        self.ingested: list[Path] = []
        self.deleted: list[str] = []

    def list_files(self):
        self.list_calls += 1
        return list(self._listing)

    def ingest_file(self, path: Path, tags=None, **_):
        if self._ingest_error is not None:
            raise self._ingest_error
        self.ingested.append(Path(path))
        return f"fid-{Path(path).name}"

    def delete_file_id(self, file_id: str) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(file_id)


@pytest.fixture
def env_dirs(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    inbox = tmp_path / "tobeloaded"
    loaded = tmp_path / "loaded"
    inbox.mkdir()
    monkeypatch.setenv("REFERENCE_TO_BE_LOADED_DIR", str(inbox))
    monkeypatch.setenv("REFERENCE_LOADED_DIR", str(loaded))
    return inbox, loaded


def test_no_files_in_inbox_is_noop(env_dirs, caplog) -> None:
    inbox, loaded = env_dirs
    rag = _StubRag()
    rc = rpi.ingest(rag)
    assert rc == 0
    assert rag.list_calls == 0
    assert rag.ingested == []
    assert not loaded.exists() or not any(loaded.iterdir())


def test_fresh_pdf_uploads_and_moves(env_dirs) -> None:
    inbox, loaded = env_dirs
    pdf = inbox / "guide.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    rag = _StubRag(listing=[])
    rc = rpi.ingest(rag)

    assert rc == 0
    assert [p.name for p in rag.ingested] == ["guide.pdf"]
    assert rag.deleted == []
    # Source moved out of inbox, into loaded.
    assert not pdf.exists()
    assert (loaded / "guide.pdf").exists()


def test_overwrite_deletes_prior_rag_entry_then_uploads(env_dirs) -> None:
    inbox, loaded = env_dirs
    pdf = inbox / "guide.pdf"
    pdf.write_bytes(b"new content")
    # A stale copy already sits in LOADED — `replace()` must overwrite it.
    loaded.mkdir()
    (loaded / "guide.pdf").write_bytes(b"old content")

    listing = [
        {"file_id": "old-fid-1", "file_name": r"X:\anywhere\guide.pdf"},
    ]
    rag = _StubRag(listing=listing)
    rc = rpi.ingest(rag)

    assert rc == 0
    assert rag.deleted == ["old-fid-1"]
    assert [p.name for p in rag.ingested] == ["guide.pdf"]
    assert (loaded / "guide.pdf").read_bytes() == b"new content"
    assert not pdf.exists()


def test_overwrite_handles_multiple_prior_duplicates(env_dirs) -> None:
    inbox, loaded = env_dirs
    (inbox / "guide.pdf").write_bytes(b"x")
    listing = [
        {"file_id": "dup-1", "file_name": "guide.pdf"},
        {"file_id": "dup-2", "file_name": r"C:\elsewhere\guide.pdf"},
    ]
    rag = _StubRag(listing=listing)
    rc = rpi.ingest(rag)

    assert rc == 0
    assert sorted(rag.deleted) == ["dup-1", "dup-2"]
    assert [p.name for p in rag.ingested] == ["guide.pdf"]


def test_upload_failure_leaves_file_in_inbox_and_sets_rc_1(env_dirs) -> None:
    inbox, loaded = env_dirs
    pdf = inbox / "guide.pdf"
    pdf.write_bytes(b"x")

    rag = _StubRag(ingest_error=RagGatewayError("nope"))
    rc = rpi.ingest(rag)

    assert rc == 1
    assert pdf.exists()                         # still in inbox
    assert not (loaded / "guide.pdf").exists()  # NOT moved


def test_list_files_failure_aborts_stage(env_dirs) -> None:
    inbox, _ = env_dirs
    (inbox / "guide.pdf").write_bytes(b"x")

    class _BadList(_StubRag):
        def list_files(self):
            raise RagGatewayError("listfiles failed")

    rag = _BadList()
    rc = rpi.ingest(rag)
    assert rc == 1
    assert rag.ingested == []


def test_partial_failure_sets_rc_1_other_files_proceed(env_dirs) -> None:
    inbox, loaded = env_dirs
    (inbox / "a.pdf").write_bytes(b"x")
    (inbox / "b.pdf").write_bytes(b"y")

    fail_on = {"b.pdf"}

    class _OneBad(_StubRag):
        def ingest_file(self, path: Path, tags=None, **_):
            if Path(path).name in fail_on:
                raise RagGatewayError("upload broke")
            self.ingested.append(Path(path))
            return f"fid-{Path(path).name}"

    rag = _OneBad(listing=[])
    rc = rpi.ingest(rag)

    assert rc == 1
    assert [p.name for p in rag.ingested] == ["a.pdf"]
    assert not (inbox / "a.pdf").exists()           # moved
    assert (loaded / "a.pdf").exists()
    assert (inbox / "b.pdf").exists()               # still in inbox
    assert not (loaded / "b.pdf").exists()


def test_non_pattern_files_are_ignored(env_dirs) -> None:
    inbox, loaded = env_dirs
    (inbox / "ignored.xlsx").write_bytes(b"x")  # handled by Excel stage, not us
    (inbox / "ignored.txt").write_text("nope")
    rag = _StubRag()
    rc = rpi.ingest(rag)
    assert rc == 0
    assert rag.ingested == []
