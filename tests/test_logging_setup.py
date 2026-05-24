"""Tests for logging_setup helpers."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from logging_setup import _resolve_level, bootstrap, configure_logging


# ---------- _resolve_level ----------

@pytest.mark.parametrize(
    "value, expected",
    [
        (None, logging.INFO),
        (logging.DEBUG, logging.DEBUG),
        (logging.WARNING, logging.WARNING),
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),               # case-insensitive
        ("  WARNING  ", logging.WARNING),     # stripped
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
        ("nope", logging.INFO),               # unknown → fallback
        ("", logging.INFO),                   # empty → fallback
        ("0", logging.INFO),                  # ambiguous string → fallback
    ],
)
def test_resolve_level(value, expected) -> None:
    assert _resolve_level(value) == expected


# ---------- configure_logging ----------

def test_configure_logging_writes_to_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(log_dir, log_level=logging.INFO)
    logger = logging.getLogger("test_configure_logging_writes_to_file")
    logger.info("hello world")
    logging.shutdown()

    log_file = log_dir / "logs.txt"
    assert log_file.is_file()
    contents = log_file.read_text(encoding="utf-8")
    assert "hello world" in contents
    assert "INFO" in contents


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """Repeat calls don't pile up handlers."""
    log_dir = tmp_path / "logs"
    configure_logging(log_dir)
    configure_logging(log_dir)
    configure_logging(log_dir)
    root = logging.getLogger()
    # We expect exactly 2 handlers (FileHandler + StreamHandler) regardless
    # of how many times configure_logging is called.
    assert len(root.handlers) == 2


# ---------- bootstrap ----------

def test_bootstrap_returns_2_on_missing_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("THIS_VAR_DOES_NOT_EXIST", raising=False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    rc = bootstrap(("LOG_DIR", "THIS_VAR_DOES_NOT_EXIST"))
    assert rc == 2


def test_bootstrap_returns_none_on_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SOME_REQUIRED_VAR", "value")
    rc = bootstrap(("LOG_DIR", "SOME_REQUIRED_VAR"))
    assert rc is None
    # Logging side-effect: handlers attached, log dir created.
    assert (tmp_path / "logs").is_dir()
