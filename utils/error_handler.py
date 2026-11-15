"""One non-recursive owner for top-level exception logging and presentation."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from types import TracebackType

ExceptionNotifier = Callable[
    [type[BaseException], BaseException, TracebackType | None], bool
]
_notifier: ExceptionNotifier | None = None
_previous_hook = None
_handling = threading.local()


def install_exception_handler(notifier: ExceptionNotifier | None = None) -> None:
    """Register presentation without building a chain of logging/UI hooks."""
    global _notifier, _previous_hook
    if notifier is not None:
        _notifier = notifier
    if sys.excepthook is not handle_unhandled_exception:
        _previous_hook = sys.excepthook
        sys.excepthook = handle_unhandled_exception


def uninstall_exception_handler() -> None:
    """Restore the displaced owner only while this module still owns the hook."""
    global _previous_hook
    if sys.excepthook is handle_unhandled_exception:
        sys.excepthook = _previous_hook or sys.__excepthook__
    _previous_hook = None


def handle_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: TracebackType | None,
) -> None:
    """Log once, present once; nested errors use Python's primitive fallback."""
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) or getattr(
        _handling, "active", False
    ):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    _handling.active = True
    try:
        try:
            from utils.app_logging import get_logger

            get_logger().critical(
                "Unhandled top-level application exception",
                exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            # A failed logging sink must not suppress the user's error dialog.
            sys.__excepthook__(*sys.exc_info())
        presented = False
        if _notifier is not None:
            try:
                presented = bool(_notifier(exc_type, exc_value, exc_tb))
            except Exception:
                sys.__excepthook__(*sys.exc_info())
        if not presented:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
    finally:
        _handling.active = False
