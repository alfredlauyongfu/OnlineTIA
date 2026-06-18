"""Centralized logging configuration and entry-point bootstrap.

`configure_logging` installs a root logger with two handlers:
  - FileHandler writing to {log_dir}/logs.txt in append mode (the persistent
    trail required by the spec).
  - StreamHandler to stderr so the operator's live view is preserved.

Both handlers use the same timestamped format. Safe to call more than once;
the second call clears prior handlers before re-installing.

`bootstrap` is a one-call convenience for the standard entry-point preamble
(.env load, required-vars check, logging setup). See its docstring.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


LOG_FILENAME = "logs.txt"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Rotate logs.txt at 10 MB, keeping up to 5 historical backups
# (logs.txt.1 ... logs.txt.5). Effective retention: ~60 MB of logs.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def _resolve_level(log_level: int | str | None) -> int:
    """Accept None, a logging-level int, or a level name string
    (e.g. 'DEBUG', 'info'). Unknown / missing values fall back to INFO.
    """
    if log_level is None:
        return logging.INFO
    if isinstance(log_level, int):
        return log_level
    name = log_level.strip().upper()
    resolved = getattr(logging, name, None)
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def configure_logging(log_dir: Path, log_level: int | str = logging.INFO) -> None:
    level = _resolve_level(log_level)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILENAME

    root = logging.getLogger()
    root.setLevel(level)

    # Drop any existing handlers so repeat calls don't duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        mode="a",
        encoding="utf-8",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)


def bootstrap(required_vars: tuple[str, ...]) -> int | None:
    """Standard entry-point preamble: load .env, check every var in
    `required_vars` is set, then call `configure_logging` using LOG_DIR /
    LOG_LEVEL from the env.

    `required_vars` must include "LOG_DIR" (otherwise the logging-config
    step will raise KeyError).

    Returns:
        None on success — caller can proceed with main work.
        int (2) on any missing required var — caller should `return` this rc
            immediately. The missing names are printed to stderr.

    Standard usage at the top of every entry-point main():
        rc = bootstrap(REQUIRED_VARS)
        if rc is not None:
            return rc
    """
    load_dotenv()
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        print(
            f"Missing required env var(s) in .env: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2
    configure_logging(
        Path(os.environ["LOG_DIR"]),
        os.environ.get("LOG_LEVEL", "INFO"),
    )
    return None
