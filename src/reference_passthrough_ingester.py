"""Stage: upload non-Excel reference files directly to RAG, then move
them to REFERENCE_LOADED_DIR.

Currently handles PDFs (extensible via `PASSTHROUGH_PATTERNS`). The
flow per file:

  1. List the current RAG inventory ONCE at stage start (basename ->
     file_id mapping).
  2. For each file matching any pattern in `PASSTHROUGH_PATTERNS` in
     REFERENCE_TO_BE_LOADED_DIR:
       a. If the basename is already in RAG, delete those entries first
          (overwrite semantics — sync-gate set-equality cannot detect
          duplicates after the fact, so we must clean up here).
       b. Upload to RAG via `RagIngester.ingest_file` with the same
          `tia_reference` tag the xlsx extractions use.
       c. On success, move the file to REFERENCE_LOADED_DIR
          (replacing any stale copy with the same basename).
       d. On a per-file RAG rejection, leave the file in the inbox for
          next-run retry; stage rc becomes 1 but the loop continues.
          If the gateway is unreachable (connection error / timeout),
          abort the stage instead — every remaining file would fail the
          same way, each paying the retry/backoff cost for nothing.

`ingest()` is imported and driven by run.py.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from excel_to_json import move_replacing
from http_resilient import TRANSIENT_ERRORS
from rag_ingester import RagGatewayError, RagIngester


PASSTHROUGH_PATTERNS: tuple[str, ...] = ("*.pdf",)


logger = logging.getLogger(__name__)


def ingest(rag: RagIngester) -> int:
    """Run the passthrough stage. Returns 0 on full success, 1 if any
    file failed (other files still proceed).

    Caller is expected to have already called `bootstrap()` and ensured
    REFERENCE_TO_BE_LOADED_DIR and REFERENCE_LOADED_DIR env vars are set.
    """
    to_be_loaded_dir = Path(os.environ["REFERENCE_TO_BE_LOADED_DIR"])
    loaded_dir = Path(os.environ["REFERENCE_LOADED_DIR"])

    inbox: list[Path] = []
    for pattern in PASSTHROUGH_PATTERNS:
        inbox.extend(
            p for p in to_be_loaded_dir.glob(pattern)
            if not p.name.startswith("~$") and not p.name.startswith(".")
        )
    inbox = sorted(set(inbox))

    if not inbox:
        logger.info(
            "No passthrough files in %s (patterns=%s); stage is a no-op",
            to_be_loaded_dir, list(PASSTHROUGH_PATTERNS),
        )
        return 0

    logger.info(
        "passthrough stage: %d file(s) to ingest (patterns=%s)",
        len(inbox), list(PASSTHROUGH_PATTERNS),
    )

    # List current RAG inventory once; build basename -> [file_id, ...]
    # so an overwrite can clean up multiple prior duplicates if they exist.
    rag_inventory: dict[str, list[str]] = {}
    try:
        entries = rag.list_files()
    except (RagGatewayError, *TRANSIENT_ERRORS) as exc:
        logger.error("Cannot list RAG files; aborting passthrough stage: %s", exc)
        return 1
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = e.get("file_name")
            file_id = e.get("file_id")
            if not name or not file_id:
                continue
            rag_inventory.setdefault(Path(name).name, []).append(file_id)

    loaded_dir.mkdir(parents=True, exist_ok=True)

    n_uploaded = 0
    n_overwritten = 0
    n_failed = 0
    for src in inbox:
        basename = src.name

        # Overwrite: delete any prior RAG entries for this basename first.
        prior_ids = rag_inventory.get(basename, [])
        prior_delete_ok = True
        for prior_id in prior_ids:
            try:
                logger.info(
                    "overwrite: deleting prior RAG entry file_id=%s for %s",
                    prior_id, basename,
                )
                rag.delete_file_id(prior_id)
            except TRANSIENT_ERRORS as exc:
                logger.error(
                    "Gateway unreachable deleting prior entry for %s; aborting "
                    "passthrough stage (remaining files stay in inbox): %s",
                    basename, exc,
                )
                return 1
            except RagGatewayError as exc:
                logger.error(
                    "Cannot delete prior RAG entry %s for %s: %s",
                    prior_id, basename, exc,
                )
                n_failed += 1
                prior_delete_ok = False
                break

        if not prior_delete_ok:
            continue

        try:
            rag.ingest_file(src, tags=["tia_reference"])
        except TRANSIENT_ERRORS as exc:
            logger.error(
                "Gateway unreachable uploading %s; aborting passthrough stage "
                "(remaining files stay in inbox): %s", basename, exc,
            )
            return 1
        except RagGatewayError as exc:
            logger.error("passthrough upload FAILED for %s: %s", basename, exc)
            n_failed += 1
            continue

        target = loaded_dir / basename
        try:
            move_replacing(src, target)
        except OSError as exc:
            logger.error(
                "passthrough move FAILED for %s (uploaded to RAG, "
                "still in inbox): %s",
                basename, exc,
            )
            n_failed += 1
            continue

        if prior_ids:
            n_overwritten += 1
            logger.info("passthrough overwrite OK: %s", basename)
        else:
            n_uploaded += 1
            logger.info("passthrough upload OK: %s -> %s", basename, loaded_dir)

    logger.info(
        "passthrough stage finished: %d uploaded, %d overwritten, %d failed",
        n_uploaded, n_overwritten, n_failed,
    )
    return 1 if n_failed else 0
