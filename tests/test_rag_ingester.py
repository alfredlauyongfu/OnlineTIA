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

class _FakeResponse:
    def __init__(self, *, ok: bool = True, status_code: int = 200,
                 text: str = "", json_body=None):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


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


def test_ingest_directory_out_of_sync_deletes_then_uploads(tmp_path: Path) -> None:
    """When local set != RAG set, every existing RAG entry is deleted then
    each local file is registered+uploaded."""
    extracted = _make_extracted_dir(tmp_path, ["a.json"])  # 1 local
    rag_listing = [
        # 2 stale in RAG — should be deleted
        {"file_id": "stale-1", "file_name": "old1.json"},
        {"file_id": "stale-2", "file_name": "old2.json"},
    ]
    ing = _make_ingester()

    post_calls: list[tuple] = []

    def fake_get(url, **_):
        return _FakeResponse(ok=True, status_code=200, json_body=rag_listing)

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        # Different endpoints return different shapes.
        if url.endswith("/rag/ingest/register"):
            return _FakeResponse(ok=True, json_body={"file_id": "new-fid-a"})
        if url.endswith("/rag/ingest/upload"):
            return _FakeResponse(ok=True)
        if url.endswith("/rag/ingest/delete"):
            return _FakeResponse(ok=True)
        return _FakeResponse(ok=False, status_code=500, text="unexpected url")

    with patch("rag_ingester.requests.get", side_effect=fake_get), \
         patch("rag_ingester.requests.post", side_effect=fake_post):
        result = ing.ingest_directory(extracted)

    assert result == {"a.json": "new-fid-a"}
    # 2 deletes + 1 register + 1 upload = 4 POSTs
    posted_urls = [u for u, _ in post_calls]
    assert posted_urls.count("https://example.invalid/rag/ingest/delete") == 2
    assert posted_urls.count("https://example.invalid/rag/ingest/register") == 1
    assert posted_urls.count("https://example.invalid/rag/ingest/upload") == 1


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
