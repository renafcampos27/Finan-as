#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Qt runtime defaults used before importing PySide6."""

from __future__ import annotations

import os
import sys

from utils.app_logging import get_log_path


def configure_qt_runtime() -> None:
    """Force conservative Qt rendering defaults once, without overriding user env."""
    os.environ.setdefault("QT_OPENGL", "software")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    # Qt 6 scales widgets in logical pixels automatically.  Keeping the exact
    # fractional factor avoids a 125/150% screen being rounded to a different
    # layout breakpoint; the explicit flag also protects older bundled Qt builds.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")


def install_global_exception_hook() -> None:
    """Register Qt presentation with the single logging/exception owner."""
    from utils.error_handler import install_exception_handler

    install_exception_handler(_present_unhandled_exception)


def _present_unhandled_exception(exc_type, exc_value, _exc_traceback) -> bool:
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    error_msg = (
        f"Ocorreu um erro inesperado na aplicação:\n\n"
        f"{exc_type.__name__}: {exc_value}\n\n"
        f"Mais detalhes no registo:\n{get_log_path()}"
    )
    try:
        from PySide6.QtCore import QThread, Qt
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            message = QMessageBox(app.activeWindow())
            message.setIcon(QMessageBox.Critical)
            message.setWindowTitle("Erro Inesperado — Finanças Pessoais")
            message.setTextFormat(Qt.PlainText)
            message.setText(error_msg)
            message.setStandardButtons(QMessageBox.Ok)
            message.exec()
            return True
    except Exception:
        # The owner will use Python's primitive fallback if presentation fails.
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0, error_msg, "Erro Inesperado — Finanças Pessoais", 0x10
            )
            return True
        except Exception:
            return False
    return False


def detect_windows_dark_theme() -> bool:
    """Detect if Windows OS is configured for dark mode applications."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0
    except Exception:
        return False


def bring_existing_window_to_front(window_title: str) -> bool:
    """Bring an existing application window to foreground on Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, window_title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE = 9
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def is_qt_valid(obj: object) -> bool:
    """Return False if obj is None or a deleted Shiboken C++ object wrapper.

    Returns True for valid Qt objects, and for non-Shiboken objects (e.g. test mocks).
    """
    if obj is None:
        return False
    try:
        from shiboken6 import isValid as _shiboken_is_valid

        return bool(_shiboken_is_valid(obj))
    except RuntimeError:
        return False
    except (ImportError, TypeError, AttributeError, ValueError):
        return True


__all__ = [
    "bring_existing_window_to_front",
    "configure_qt_runtime",
    "detect_windows_dark_theme",
    "install_global_exception_hook",
    "is_qt_valid",
]
