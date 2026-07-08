"""Tests for rag_ingester. Pure logic is tested directly; HTTP methods
(list_files / ingest_directory) are tested with `requests.get/post`
monkey-patched so the suite runs offline.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from rag_ingester import RagGatewayError, RagIngester
from tests.helpers import FakeResponse as _FakeResponse


def _make_ingester() -> RagIngester:
    return RagIngester(
        base_url="https://example.invalid",
        api_key="fake-key",
        llm_model="fake-model",
    )


# ---------- _sha256 ----------

def test_sha256_matches_stdlib(tmp_path: Path) -> None:
    body = b"the quick brown fox jumps over the lazy dog"
    f = tmp_path / "blob.bin"
    f.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    assert RagIngester._sha256(f) == expected


def test_sha256_handles_large_file(tmp_path: Path) -> None:
    """Streams in 8KB chunks — make sure a >8KB file hashes correctly."""
    body = b"x" * (8192 * 3 + 17)
    f = tmp_path / "blob.bin"
    f.write_bytes(body)
    assert RagIngester._sha256(f) == hashlib.sha256(body).hexdigest()


# ---------- _build_rag_config ----------

def test_build_rag_config_defaults() -> None:
    ing = _make_ingester()
    cfg = ing._build_rag_config(tags=None, overrides=None, force_update=True)
    assert cfg["llm_name"] == "fake-model"
    assert cfg["tags"] == []
    assert cfg["force_update"] is True
    # Defaults from DEFAULT_RAG_CONFIG are present:
    assert cfg["chunk_size"] == 1024
    assert cfg["embedding_model_name"] == "all-mpnet-base-v2"


def test_build_rag_config_overrides_apply_last() -> None:
    ing = _make_ingester()
    cfg = ing._build_rag_config(
        tags=["t1", "t2"],
        overrides={"chunk_size": 256, "extra_field": "x"},
        force_update=False,
    )
    assert cfg["tags"] == ["t1", "t2"]
    assert cfg["force_update"] is False
    assert cfg["chunk_size"] == 256        # overridden
    assert cfg["extra_field"] == "x"       # added via overrides


# ---------- _raise_for_status ----------

def test_raise_for_status_passthrough_on_ok() -> None:
    # No exception, no return value.
    RagIngester._raise_for_status(_FakeResponse(ok=True, status_code=200), "label")


def test_raise_for_status_raises_on_4xx() -> None:
    bad = _FakeResponse(ok=False, status_code=400, text='{"error": "bad"}')
    with pytest.raises(RagGatewayError) as exc_info:
        RagIngester._raise_for_status(bad, "register foo.json")
    assert "register foo.json HTTP 400" in str(exc_info.value)
    assert '"error": "bad"' in str(exc_info.value)


# ---------- ingest_directory: sync-gate behaviour (HTTP mocked) ----------

def _make_extracted_dir(tmp_path: Path, names: list[str]) -> Path:
    """Create tmp_path/extracted/ with the given filenames as empty JSON files."""
    d = tmp_path / "extracted"
    d.mkdir()
    for n in names:
        (d / n).write_text("{}", encoding="utf-8")
    return d


def test_ingest_directory_sync_ok_when_basenames_match(tmp_path: Path) -> None:
    """When local basenames == RAG basenames, no register/upload/delete."""
    extracted = _make_extracted_dir(tmp_path, ["a.json", "b.json"])
    rag_listing = [
        {"file_id": "fid-a", "file_name": r"C:\some\other\dir\a.json"},
        {"file_id": "fid-b", "file_name": r"C:\some\other\dir\b.json"},
    ]
    ing = _make_ingester()

    with patch("rag_ingester.requests.get") as mock_get, \
         patch("rag_ingester.requests.post") as mock_post:
        mock_get.return_value = _FakeResponse(ok=True, status_code=200, json_body=rag_listing)
        result = ing.ingest_directory(extracted)

    assert result == {"a.json": "fid-a", "b.json": "fid-b"}
    mock_get.assert_called_once()         # one listfiles call
    mock_post.assert_not_called()         # NO delete / register / upload


def _make_sync_post(register_fid: str = "new-fid"):
    """Returns a (post_calls list, fake_post) pair recording every POST.

    `register` responses always include `register_fid` as the file_id —
    set a unique value if the test inspects individual results.
    """
    post_calls: list[tuple] = []

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        if url.endswith("/rag/ingest/register"):
            return _FakeResponse(ok=True, json_body={"file_id": register_fid})
        if url.endswith("/rag/ingest/upload"):
            return _FakeResponse(ok=True)
        if url.endswith("/rag/ingest/delete"):
            return _FakeResponse(ok=True)
        return _FakeResponse(ok=False, status_code=500, text="unexpected url")

    return post_calls, fake_post


def test_ingest_directory_out_of_sync_set_difference(tmp_path: Path) -> None:
    """When local set != RAG set, ONLY the diff is acted on: stale entries
    deleted, new entries uploaded, intersection's file_ids preserved.
    """
    extracted = _make_extracted_dir(tmp_path, ["keep.json", "new.json"])
    rag_listing = [
        {"file_id": "fid-keep", "file_name": "keep.json"},  # in both → KEEP
        {"file_id": "stale-1", "file_name": "old1.json"},   # stale → DELETE
        {"file_id": "stale-2", "file_name": "old2.json"},   # stale → DELETE
    ]
    ing = _make_ingester()
    post_calls, fake_post = _make_sync_post(register_fid="fid-new")

    with patch("rag_ingester.requests.get",
               return_value=_FakeResponse(ok=True, status_code=200, json_body=rag_listing)), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted)

    assert result == {"keep.json": "fid-keep", "new.json": "fid-new"}
    posted_urls = [u for u, _ in post_calls]
    # 2 deletes (stale-1, stale-2) + 1 register + 1 upload (new.json)
    assert posted_urls.count("https://example.invalid/rag/ingest/delete") == 2
    assert posted_urls.count("https://example.invalid/rag/ingest/register") == 1
    assert posted_urls.count("https://example.invalid/rag/ingest/upload") == 1


def test_ingest_directory_only_new_uploaded_no_deletes(tmp_path: Path) -> None:
    """Local strictly extends RAG set → no deletes; only new file uploaded."""
    extracted = _make_extracted_dir(tmp_path, ["a.json", "b.json"])
    rag_listing = [
        {"file_id": "fid-a", "file_name": "a.json"},  # already in RAG → KEEP
    ]
    ing = _make_ingester()
    post_calls, fake_post = _make_sync_post(register_fid="fid-b")

    with patch("rag_ingester.requests.get",
               return_value=_FakeResponse(ok=True, status_code=200, json_body=rag_listing)), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted)

    assert result == {"a.json": "fid-a", "b.json": "fid-b"}
    posted_urls = [u for u, _ in post_calls]
    assert posted_urls.count("https://example.invalid/rag/ingest/delete") == 0


def test_ingest_directory_only_stale_deleted_no_uploads(tmp_path: Path) -> None:
    """Local is a strict subset of RAG → no uploads; stale entries deleted."""
    extracted = _make_extracted_dir(tmp_path, ["keep.json"])
    rag_listing = [
        {"file_id": "fid-keep", "file_name": "keep.json"},
        {"file_id": "stale", "file_name": "stale.json"},
    ]
    ing = _make_ingester()
    post_calls, fake_post = _make_sync_post()

    with patch("rag_ingester.requests.get",
               return_value=_FakeResponse(ok=True, status_code=200, json_body=rag_listing)), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted)

    assert result == {"keep.json": "fid-keep"}
    posted_urls = [u for u, _ in post_calls]
    assert posted_urls.count("https://example.invalid/rag/ingest/delete") == 1
    assert posted_urls.count("https://example.invalid/rag/ingest/upload") == 0


def test_ingest_directory_tag_isolation_ignores_other_use_cases(tmp_path: Path) -> None:
    """When `tags` is given, RAG entries lacking those tags must be ignored
    — they should NOT be deleted, and they should NOT count toward the
    sync-gate basename set even if their basename collides with a local file.
    """
    extracted = _make_extracted_dir(tmp_path, ["shared.json"])
    rag_listing = [
        # Owned by us (has our tag) — already in RAG → KEEP
        {"file_id": "fid-shared",  "file_name": "shared.json",
         "tags": ["tia_reference"]},
        # Different use case — must be ignored entirely
        {"file_id": "fid-foreign", "file_name": "foreign.json",
         "tags": ["other_use_case"]},
        # No tags at all — also outside our scope
        {"file_id": "fid-untagged", "file_name": "untagged.json"},
    ]
    ing = _make_ingester()
    post_calls, fake_post = _make_sync_post()

    with patch("rag_ingester.requests.get",
               return_value=_FakeResponse(ok=True, status_code=200, json_body=rag_listing)), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted, tags=["tia_reference"])

    # Result includes only the entry we own.
    assert result == {"shared.json": "fid-shared"}
    # CRITICAL: no deletes against the other use case's entries.
    posted_urls = [u for u, _ in post_calls]
    assert posted_urls.count("https://example.invalid/rag/ingest/delete") == 0


def test_ingest_directory_deletes_duplicate_basename_entries(tmp_path: Path) -> None:
    """Two owned RAG entries share one local basename (e.g. a hard-capped
    upload attempt that still completed server-side, plus its retry): the kept
    mapping wins, every other file_id for that basename is deleted, and the
    file is NOT re-uploaded."""
    extracted = _make_extracted_dir(tmp_path, ["a.json"])
    rag_listing = [
        {"file_id": "fid-old", "file_name": "a.json"},
        {"file_id": "fid-new", "file_name": r"C:\elsewhere\a.json"},
    ]
    ing = _make_ingester()
    post_calls, fake_post = _make_sync_post()

    with patch("rag_ingester.requests.get",
               return_value=_FakeResponse(ok=True, status_code=200, json_body=rag_listing)), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted)

    # Last listing entry wins the basename mapping; the duplicate is deleted.
    assert result == {"a.json": "fid-new"}
    deleted = [kw["json"]["file_id"] for u, kw in post_calls
               if u.endswith("/rag/ingest/delete")]
    assert deleted == ["fid-old"]
    posted_urls = [u for u, _ in post_calls]
    assert posted_urls.count("https://example.invalid/rag/ingest/upload") == 0


def test_entry_has_tags_helper() -> None:
    has = RagIngester._entry_has_tags
    # No filter → always True
    assert has({"tags": ["x"]}, None) is True
    assert has({}, None) is True
    assert has({}, []) is True
    # Subset semantics
    assert has({"tags": ["a", "b"]}, ["a"]) is True
    assert has({"tags": ["a", "b"]}, ["a", "b"]) is True
    assert has({"tags": ["a"]}, ["a", "b"]) is False
    # Missing or wrong type
    assert has({}, ["a"]) is False
    assert has({"tags": None}, ["a"]) is False
    assert has({"tags": "a"}, ["a"]) is False  # not a list


def test_upload_logs_ok(tmp_path: Path, caplog) -> None:
    """_upload_one_shot logs an OK line with the file_id after a 2xx upload."""
    import logging
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    ing = _make_ingester()
    resp = _FakeResponse(ok=True, status_code=200, json_body={"file_id": "fid-1"})
    with patch("rag_ingester.requests.post", return_value=resp):
        with caplog.at_level(logging.INFO):
            ing._upload_one_shot("fid-1", f)
    assert any("RAG upload OK" in r.message for r in caplog.records)


def test_ingest_directory_empty_returns_empty_dict(tmp_path: Path) -> None:
    """Empty source dir → returns {} without ever hitting the gateway."""
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    ing = _make_ingester()

    with patch("rag_ingester.requests.get") as mock_get, \
         patch("rag_ingester.requests.post") as mock_post:
        result = ing.ingest_directory(extracted)

    assert result == {}
    mock_get.assert_not_called()
    mock_post.assert_not_called()


def test_ingest_directory_missing_dir_raises(tmp_path: Path) -> None:
    ing = _make_ingester()
    with pytest.raises(FileNotFoundError):
        ing.ingest_directory(tmp_path / "does_not_exist")


def test_ingest_directory_extras_fold_into_basename_set(tmp_path: Path) -> None:
    """`extra_files` should be merged with glob_pattern matches when the
    sync gate compares against the RAG inventory."""
    extracted = _make_extracted_dir(tmp_path, ["a.json"])
    # The "extra" file lives in a separate dir but should be considered
    # part of the local set.
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    pdf = pdf_dir / "guide.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    rag_listing = [
        {"file_id": "fid-a", "file_name": r"X:\old\a.json"},
        {"file_id": "fid-p", "file_name": r"X:\old\guide.pdf"},
    ]
    ing = _make_ingester()

    with patch("rag_ingester.requests.get") as mock_get, \
         patch("rag_ingester.requests.post") as mock_post:
        mock_get.return_value = _FakeResponse(ok=True, status_code=200, json_body=rag_listing)
        result = ing.ingest_directory(extracted, extra_files=[pdf])

    # Set equality holds across both sources → skip upload.
    assert set(result.keys()) == {"a.json", "guide.pdf"}
    mock_post.assert_not_called()


def test_list_files_raises_on_non_2xx(tmp_path: Path) -> None:
    ing = _make_ingester()
    bad = _FakeResponse(ok=False, status_code=500, text="server error")
    with patch("rag_ingester.requests.get", return_value=bad):
        with pytest.raises(RagGatewayError) as exc_info:
            ing.list_files()
    assert "listfiles HTTP 500" in str(exc_info.value)


def test_list_files_propagates_connection_error() -> None:
    ing = _make_ingester()
    with patch(
        "rag_ingester.requests.get",
        side_effect=requests.exceptions.ConnectionError("boom"),
    ):
        with pytest.raises(requests.exceptions.ConnectionError):
            ing.list_files()


# ---------- ctor ----------

def test_base_url_trailing_slash_stripped() -> None:
    ing = RagIngester(
        base_url="https://example.invalid///",
        api_key="k",
        llm_model="m",
    )
    assert ing.base_url == "https://example.invalid"
