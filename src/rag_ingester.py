"""Push files into the SS&C Cloud RAG-as-a-Service (Soma / Ganglia).

Each file goes through two HTTP POSTs against the AI Gateway RAG API:
  1. /rag/ingest/register  - assigns a file_id given filename + sha256 +
                             rag_config_metadata (chunking, embedding, tags...).
  2. /rag/ingest/upload    - uploads the file blob (multipart) under that file_id;
                             ganglia chunks, vectorizes, and stores in Postgres.

Auth is via the `X-API-Key` header (reuses SSC_CLOUD_AIGATEWAY_API_KEY).

This module is standalone — instantiating callers wire `RagIngester` in
themselves. A bottom-of-file `main()` test harness lets you ingest every
`extracted_*.json` file in REFERENCE_JSON_DIR with
`python src\rag_ingester.py`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests

from logging_setup import bootstrap


logger = logging.getLogger(__name__)


class RagGatewayError(RuntimeError):
    """Raised when the RAG gateway returns a non-2xx or is unreachable."""


class RagIngester:
    DEFAULT_RAG_CONFIG: dict[str, Any] = {
        "chunk_separator": " ",
        "chunk_size": 1024,
        "chunk_overlap": 512,
        "use_advanced_extractors": False,
        "embedding_model_name": "all-mpnet-base-v2",
        "use_tesseract_ocr": False,
        "append_tags": False,
    }

    def __init__(
        self,
        base_url: str,
        api_key: str,
        llm_model: str,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.llm_model = llm_model
        self.timeout_seconds = timeout_seconds

    # ---- public ----

    def list_files(self) -> Any:
        """Call /rag/ingest/listfiles. Logs the full response and returns the
        parsed JSON payload. Useful as a pre-flight check to confirm the gateway
        is reachable and to see what's already ingested.
        """
        url = f"{self.base_url}/rag/ingest/listfiles"
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key}

        logger.info("RAG listfiles start: GET %s", url)
        try:
            response = requests.get(url, headers=headers, timeout=self.timeout_seconds)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            logger.error("RAG listfiles FAILED (gateway): %s", exc)
            raise
        self._raise_for_status(response, "listfiles")

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(
                "RAG listfiles FAILED: bad JSON: %s; body=%s",
                exc, response.text[:500],
            )
            raise RagGatewayError(
                f"listfiles non-JSON response: {response.text[:500]}"
            )

        # Count first (always visible). At INFO level the log gets only the
        # list of file names. At DEBUG level it also gets the full structured
        # response (file_id, status, tags, created_at, num_chunks, ...).
        count = len(payload) if hasattr(payload, "__len__") else "unknown"
        logger.info("RAG listfiles OK: %s file(s) currently ingested", count)
        if isinstance(payload, list):
            names = [
                e.get("file_name")
                for e in payload
                if isinstance(e, dict) and e.get("file_name")
            ]
            logger.info(
                "RAG listfiles file names:\n%s",
                "\n".join(names) if names else "(none)",
            )
        logger.debug(
            "RAG listfiles response (full):\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return payload

    def ingest_file(
        self,
        file_path: Path,
        tags: list[str] | None = None,
        rag_config_overrides: dict[str, Any] | None = None,
        force_update: bool = True,
    ) -> str:
        """Register + upload one file. Returns the file_id assigned by ganglia."""
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        sha256 = self._sha256(file_path)
        rag_config = self._build_rag_config(tags, rag_config_overrides, force_update)
        file_id = self._register(file_path, sha256, rag_config)
        self._upload_one_shot(file_id, file_path)
        return file_id

    def ingest_directory(
        self,
        dir_path: Path,
        glob_pattern: str = "*.json",
        tags: list[str] | None = None,
        rag_config_overrides: dict[str, Any] | None = None,
        force_update: bool = True,
        extra_files: list[Path] | None = None,
    ) -> dict[str, str]:
        """Ingest every matching file in dir_path. Returns {filename: file_id}.

        Per-file failures are logged and skipped — the run continues for the
        remaining files. A connection-level failure (gateway unreachable) is
        raised so the caller can abort instead of looping uselessly.

        `extra_files` (if given) are merged into the local file set for the
        basename-equality sync gate AND uploaded alongside the glob matches
        when a mismatch triggers re-upload. Use this when files outside
        `dir_path` should be considered part of the same RAG-managed set
        (e.g. PDFs already moved to REFERENCE_LOADED_DIR).

        Sync gate behaviour (set-difference):
          - The RAG inventory is filtered to entries carrying ALL of the
            requested `tags` (if any) so this pipeline only touches the
            files it owns. Other use-cases sharing the same gateway are
            invisible to the gate.
          - Stale entries (in RAG, not local) are deleted.
          - New files (local, not in RAG) are uploaded.
          - Files in both sides keep their existing `file_id`.
        """
        if not dir_path.is_dir():
            raise FileNotFoundError(f"Directory not found: {dir_path}")

        files = sorted(set(dir_path.glob(glob_pattern)) | set(extra_files or []))
        if not files:
            logger.warning("No files match %s in %s", glob_pattern, dir_path)
            return {}

        # Sync gate: compare local basenames against what's already in RAG
        # under our tag set. Set-difference yields the minimal delete/upload
        # work; intersection keeps its existing file_id.
        rag_entries = self.list_files()
        if not isinstance(rag_entries, list):
            rag_entries = []

        # Tag isolation: only consider RAG entries that carry all of our
        # requested tags. With `tags=None`, no filtering happens (matches
        # the legacy behaviour for callers that genuinely want the full
        # inventory).
        owned_entries: list[dict] = [
            e for e in rag_entries
            if isinstance(e, dict)
            and e.get("file_name")
            and e.get("file_id")
            and self._entry_has_tags(e, tags)
        ]

        # basename -> file_id for our owned RAG entries. If duplicates exist
        # (shouldn't, but defend against it) the last one wins; the others
        # become stale during the set-difference pass below.
        rag_by_name: dict[str, str] = {
            Path(e["file_name"]).name: e["file_id"] for e in owned_entries
        }
        local_by_name: dict[str, Path] = {p.name: p for p in files}

        local_names = set(local_by_name)
        rag_names = set(rag_by_name)

        to_delete = rag_names - local_names
        to_upload = local_names - rag_names
        unchanged = local_names & rag_names

        if not to_delete and not to_upload:
            logger.info(
                "RAG sync OK: %d local file(s) match %d uploaded file(s); skipping upload",
                len(local_names), len(rag_names),
            )
            return {name: rag_by_name[name] for name in unchanged}

        logger.info(
            "RAG sync: delete %d stale, upload %d new, keep %d unchanged",
            len(to_delete), len(to_upload), len(unchanged),
        )

        # Delete only stale entries — and clean up any extra owned-by-tag
        # entries that share a basename we're keeping (defensive dedup).
        stale_file_ids: list[str] = [
            e["file_id"] for e in owned_entries
            if Path(e["file_name"]).name in to_delete
        ]
        for file_id in stale_file_ids:
            try:
                self._delete_one(file_id)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # Gateway dropped mid-delete: stop trying to delete; the
                # subsequent ingest will also fail fast.
                logger.error("Gateway unreachable mid-delete: %s", exc)
                raise RagGatewayError(f"Gateway unreachable mid-delete: {exc}") from exc
            except Exception as exc:
                # Per-file delete failure: log and keep deleting others.
                logger.error("delete failed for %s: %s", file_id, exc)

        # Start with the unchanged entries' existing file_ids, then upload
        # only the new files.
        results: dict[str, str] = {name: rag_by_name[name] for name in unchanged}
        for name in sorted(to_upload):
            f = local_by_name[name]
            try:
                file_id = self.ingest_file(
                    f,
                    tags=tags,
                    rag_config_overrides=rag_config_overrides,
                    force_update=force_update,
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # Gateway-level failure: don't keep hammering it.
                logger.error("Gateway unreachable, aborting directory ingest: %s", exc)
                raise RagGatewayError(f"Gateway unreachable: {exc}") from exc
            except Exception as exc:
                logger.error("ingest skipped %s: %s", f.name, exc)
                continue
            results[name] = file_id

        return results

    @staticmethod
    def _entry_has_tags(entry: dict, required_tags: list[str] | None) -> bool:
        """Does `entry` carry every tag in `required_tags`?

        `required_tags=None` means "no filter" → always True (legacy
        callers and the tag-agnostic `list_files()` consumers keep
        seeing every entry).
        """
        if not required_tags:
            return True
        entry_tags = entry.get("tags") or []
        if not isinstance(entry_tags, list):
            return False
        return set(required_tags).issubset(entry_tags)

    # ---- internals ----

    @staticmethod
    def _sha256(file_path: Path) -> str:
        h = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _build_rag_config(
        self,
        tags: list[str] | None,
        overrides: dict[str, Any] | None,
        force_update: bool,
    ) -> dict[str, Any]:
        config: dict[str, Any] = dict(self.DEFAULT_RAG_CONFIG)
        config["llm_name"] = self.llm_model
        config["tags"] = list(tags) if tags else []
        config["force_update"] = force_update
        if overrides:
            config.update(overrides)
        return config

    @staticmethod
    def _raise_for_status(response: requests.Response, label: str) -> None:
        """If the HTTP response isn't 2xx, log + raise `RagGatewayError`.
        `label` is embedded in both the log line and the exception message.
        """
        if response.ok:
            return
        logger.error(
            "RAG %s FAILED: HTTP %d: %s",
            label, response.status_code, response.text[:500],
        )
        raise RagGatewayError(
            f"{label} HTTP {response.status_code}: {response.text[:500]}"
        )

    def _post_json(
        self, endpoint: str, body: dict[str, Any], label: str
    ) -> requests.Response:
        """POST application/json + X-API-Key to {base_url}{endpoint}. Logs
        '(gateway)' and re-raises the raw requests exception on
        connection failure (so callers can distinguish a gateway-down
        condition from a per-request HTTP error). Calls
        `_raise_for_status` on the response before returning it.
        """
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json", "X-API-Key": self.api_key}
        try:
            response = requests.post(
                url, headers=headers, json=body, timeout=self.timeout_seconds
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            logger.error("RAG %s FAILED (gateway): %s", label, exc)
            raise
        self._raise_for_status(response, label)
        return response

    def _register(self, file_path: Path, sha256: str, rag_config: dict[str, Any]) -> str:
        label = f"register {file_path.name}"
        logger.info(
            "RAG register start: %s (sha256=%s, size=%d)",
            file_path.name, sha256[:12], file_path.stat().st_size,
        )
        response = self._post_json(
            "/rag/ingest/register",
            {
                "file_name": str(file_path),
                "checksum": sha256,
                "rag_config_metadata": rag_config,
            },
            label,
        )
        try:
            file_id = response.json()["file_id"]
        except (ValueError, KeyError, TypeError) as exc:
            logger.error(
                "RAG %s FAILED: bad response shape: %s; body=%s",
                label, exc, response.text[:500],
            )
            raise RagGatewayError(
                f"Unexpected register response: {exc}; body={response.text[:500]}"
            )
        logger.info("RAG register OK: %s -> file_id=%s", file_path.name, file_id)
        return file_id

    def _upload_one_shot(self, file_id: str, file_path: Path) -> None:
        """One-shot upload: the entire file goes in a SINGLE multipart POST.

        The gateway's `/rag/ingest/upload` endpoint supports chunked uploads
        (caller sends the file as a stream of parts, `file_end=false` on every
        part except the last). This implementation intentionally does NOT do
        that: we always send the complete file in one request with
        `file_end=true`. Simpler, atomic per file, and avoids partial-state
        cleanup on retry.
        """
        url = f"{self.base_url}/rag/ingest/upload"
        headers = {"X-API-Key": self.api_key}
        label = f"upload {file_path.name}"

        logger.info("RAG upload start (one-shot): %s (file_id=%s)", file_path.name, file_id)
        try:
            with file_path.open("rb") as fh:
                # file_end="true" marks this as the complete file (not part of
                # a chunked stream). Sent as a string because multipart form
                # fields are strings; the gateway parses "true"/"false" itself.
                response = requests.post(
                    url,
                    headers=headers,
                    data={"file_id": file_id, "file_end": "true"},
                    files={"file": (file_path.name, fh)},
                    timeout=self.timeout_seconds,
                )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            logger.error("RAG %s FAILED (gateway): %s", label, exc)
            raise
        self._raise_for_status(response, label)
        # The gateway chunks + vectorizes during upload processing, so the
        # chunk count (if reported) appears on THIS response, not on register.
        num_chunks = self._extract_chunk_count(response)
        if num_chunks is not None:
            logger.info(
                "RAG upload OK: %s (file_id=%s, num_chunks=%s)",
                file_path.name, file_id, num_chunks,
            )
        else:
            logger.info("RAG upload OK: %s (file_id=%s)", file_path.name, file_id)

    @staticmethod
    def _extract_chunk_count(response: Any) -> int | None:
        """Best-effort: pull a chunk count out of a gateway response. The
        gateway reports it under one of a few possible field names (and only
        once vectorization has run), so this returns None when absent or when
        the body isn't JSON."""
        try:
            payload = response.json()
        except (ValueError, AttributeError):
            return None
        if not isinstance(payload, dict):
            return None
        for key in ("num_chunks", "chunk_count", "n_chunks", "chunks"):
            v = payload.get(key)
            if isinstance(v, bool):  # guard: bools are ints in Python
                continue
            if isinstance(v, int):
                return v
            if isinstance(v, list):
                return len(v)
        return None

    def _delete_one(self, file_id: str) -> None:
        """POST /rag/ingest/delete with {'file_id': file_id}.

        Per the doc, deletion is one-file-per-call. Caller loops for many.
        Logs start / OK / FAILED. Raises RagGatewayError on HTTP non-2xx;
        raw requests.exceptions on connection failure (so the caller can
        distinguish a per-file rejection from a gateway-down condition).
        """
        logger.info("RAG delete start: file_id=%s", file_id)
        self._post_json(
            "/rag/ingest/delete", {"file_id": file_id}, f"delete file_id={file_id}"
        )
        logger.info("RAG delete OK: file_id=%s", file_id)

    def delete_file_id(self, file_id: str) -> None:
        """Public alias for `_delete_one`. Use this from outside the class."""
        self._delete_one(file_id)


# ---- standalone test harness ----
#
# Run directly to ingest every extracted_*.json file in REFERENCE_JSON_DIR:
#   & C:\blueprism\OnlineTIA\.venv\Scripts\python.exe C:\blueprism\OnlineTIA\src\rag_ingester.py
#
# Requires the per-sheet extracted files to already exist (run
# reference_info_extractor.py first).

REQUIRED_ENV_VARS = (
    "SSC_CLOUD_AIGATEWAY_BASE_URL",
    "SSC_CLOUD_AIGATEWAY_API_KEY",
    "SSC_CLOUD_AIGATEWAY_MODEL",
    "REFERENCE_JSON_DIR",
    "LOG_DIR",
)


def main() -> int:
    rc = bootstrap(REQUIRED_ENV_VARS)
    if rc is not None:
        return rc

    reference_json_dir = Path(os.environ["REFERENCE_JSON_DIR"])
    if not any(reference_json_dir.glob("extracted_*.json")):
        print(
            f"No extracted_*.json files in {reference_json_dir}\n"
            f"Run reference_info_extractor.py first to populate it.",
            file=sys.stderr,
        )
        return 1

    ingester = RagIngester(
        base_url=os.environ["SSC_CLOUD_AIGATEWAY_BASE_URL"],
        api_key=os.environ["SSC_CLOUD_AIGATEWAY_API_KEY"],
        llm_model=os.environ["SSC_CLOUD_AIGATEWAY_MODEL"],
    )

    print("=== rag_ingester (standalone test) ===")
    print(f"  source dir : {reference_json_dir} (extracted_*.json only)")
    print(f"  endpoint   : {ingester.base_url}/rag/ingest/(listfiles|register|upload)")
    print(f"  model      : {ingester.llm_model}")

    # Pre-flight: list what's already in the RAG store. Logged in full. Treated
    # as fatal so we don't proceed to ingest if the gateway is unreachable.
    try:
        ingester.list_files()
    except (RagGatewayError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        print(f"  ABORTED (listfiles): {exc}", file=sys.stderr)
        return 1

    try:
        results = ingester.ingest_directory(
            reference_json_dir,
            tags=["tia_reference"],
            glob_pattern="extracted_*.json",
        )
    except RagGatewayError as exc:
        print(f"  ABORTED: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("  No files ingested.", file=sys.stderr)
        return 1

    print(f"  OK ({len(results)} file(s) ingested)")
    for name, fid in sorted(results.items()):
        print(f"    {name} -> {fid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
