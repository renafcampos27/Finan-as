#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central logging for the Liquid Glass app."""

from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

from utils.config import logs_dir

LOG_DIR = logs_dir()
LOG_FILE = LOG_DIR / "app.log"
LOGGER_NAME = "financas_liquid_glass"

_configured = False
_old_threading_excepthook = getattr(threading, "excepthook", None)

_LOG_REDACTIONS = (
    (
        re.compile(r"(?i)([?&](?:key|api_key|x-goog-api-key)=)[^&\s]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(x-goog-api-key|authorization|gemini_api_key|google_api_key|openai_api_key|password|backup_password|passphrase|token|secret|pin)"
            r"(\s*[:=]\s*)(?:Bearer\s+)?\S+"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_SECRET_KEY]"),
    (
        re.compile(r"(?i)\bATCUD:[A-Za-z0-9_-]+-[0-9]+\b"),
        "[ATCUD]",
    ),
    (
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
        "[EMAIL]",
    ),
    (
        re.compile(r"(?i)\b(?:PT\d{2}|[A-Z]{2}\d{2})(?:[\s-]?[A-Z0-9]){11,30}\b"),
        "[IBAN]",
    ),
    (
        re.compile(r"\b\d{4}[\s.-]?\d{4}[\s.-]?\d{11}[\s.-]?\d{2}\b"),
        "[NIB]",
    ),
    (
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        "[CARD_NUMBER]",
    ),
    (
        re.compile(r"(?i)\b(?:NIF[:\s]*)?(?:PT)?([1235689]\d{8})\b"),
        "[NIF]",
    ),
    (
        re.compile(
            r"(?i)\b(?:\d{1,3}(?:[.,\s]\d{3})*|\d+)[.,]\d{2}\s*(?:€|EUR|USD|\$)\b"
        ),
        "[AMOUNT]",
    ),
    (
        re.compile(
            r"(?i)\b(?:€|EUR|USD|\$)\s*(?:\d{1,3}(?:[.,\s]\d{3})*|\d+)[.,]\d{2}\b"
        ),
        "[AMOUNT]",
    ),
    (
        re.compile(re.escape(str(Path.home())), re.IGNORECASE),
        "~",
    ),
)


def redact_log_text(value: object) -> str:
    text = str(value if value is not None else "")
    for pattern, replacement in _LOG_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Redact secrets and direct identifiers from messages and tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or LOGGER_NAME)


def get_log_path() -> Path:
    return LOG_FILE


def configure_logging() -> Path:
    """Configure file logging once and capture unhandled Python exceptions."""
    global _configured
    if _configured:
        return LOG_FILE

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = RedactingFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if getattr(sys, "stderr", None) is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.WARNING)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    from utils.error_handler import install_exception_handler

    install_exception_handler()
    if hasattr(threading, "excepthook"):
        threading.excepthook = _handle_thread_exception  # type: ignore[assignment]

    _configured = True
    logger.info("Logging initialized at %s", LOG_FILE)
    return LOG_FILE


def shutdown_logging() -> None:
    """Close application handlers so tests/tools can remove isolated profiles."""

    global _configured
    logger = get_logger()
    for handler in tuple(logger.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            logger.removeHandler(handler)
    from utils.error_handler import uninstall_exception_handler

    uninstall_exception_handler()
    if hasattr(threading, "excepthook") and _old_threading_excepthook is not None:
        threading.excepthook = _old_threading_excepthook
    _configured = False


def _handle_unhandled_exception(
    exc_type: Type[BaseException],
    exc_value: BaseException,
    exc_tb: Optional[TracebackType],
) -> None:
    # Compatibility entry point; this function is not installed as another hook.
    from utils.error_handler import handle_unhandled_exception

    handle_unhandled_exception(exc_type, exc_value, exc_tb)


def _handle_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not None and issubclass(args.exc_type, KeyboardInterrupt):
        if _old_threading_excepthook is not None:
            _old_threading_excepthook(args)
        return
    get_logger().critical(
        "Unhandled thread exception in %s",
        getattr(args.thread, "name", "<unknown>"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
    if _old_threading_excepthook is not None:
        _old_threading_excepthook(args)


def install_qt_message_handler() -> None:
    """Route Qt warnings/errors into the same log file."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return

    def _qt_handler(mode, context, message: str) -> None:
        logger = get_logger("financas_liquid_glass.qt")
        location = ""
        try:
            file_name = getattr(context, "file", "") or ""
            line = int(getattr(context, "line", 0) or 0)
            function = getattr(context, "function", "") or ""
            if file_name or line or function:
                location = f" ({file_name}:{line} {function})"
        except Exception:
            location = ""

        text = f"{message}{location}"
        if mode == QtMsgType.QtDebugMsg:
            logger.debug(text)
        elif mode == QtMsgType.QtInfoMsg:
            logger.info(text)
        elif mode == QtMsgType.QtWarningMsg:
            logger.warning(text)
        elif mode == QtMsgType.QtCriticalMsg:
            logger.error(text)
        elif mode == QtMsgType.QtFatalMsg:
            logger.critical(text)
        else:
            logger.warning(text)

    qInstallMessageHandler(_qt_handler)
    get_logger().info("Qt message handler installed")


def log_exception(context: str, exc: BaseException | None = None) -> None:
    """Log an exception with stack trace from an except block."""
    logger = get_logger()
    if exc is None:
        logger.exception(context)
    else:
        logger.exception("%s: %s", context, exc)


def log_warning(message: str, *args) -> None:
    get_logger().warning(message, *args)


def log_info(message: str, *args) -> None:
    get_logger().info(message, *args)
