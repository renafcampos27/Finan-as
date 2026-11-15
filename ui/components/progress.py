#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys
from typing import Callable

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
    from PySide6.QtWidgets import QSizePolicy, QWidget
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)

from ui.core import Theme


class ProgressPill(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str,
        progress: float,
        accent: str,
        theme_provider: Callable[[], Theme],
    ):
        super().__init__()
        self.title, self.subtitle, self.progress, self.accent = (
            title,
            subtitle,
            max(0, min(1, progress)),
            accent,
        )
        self.theme_provider = (
            theme_provider
            if callable(theme_provider)
            else (lambda theme=theme_provider: theme)
        )
        self.setMinimumHeight(62)

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        theme = self.theme_provider()
        p.fillRect(self.rect(), QColor("#182033") if theme.dark else QColor("#F8FBFF"))
        r = self.rect().adjusted(0, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), 17, 17)
        p.fillPath(
            path,
            QColor(255, 255, 255, 84) if not theme.dark else QColor(255, 255, 255, 18),
        )
        p.setPen(QColor(theme.text))
        p.setFont(QFont("Segoe UI", 9, QFont.Bold))
        p.drawText(14, 22, self.title)
        p.setPen(QColor(theme.muted))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(14, 42, self.subtitle)
        bx, by, bw, bh = self.width() - 126, 26, 82, 8
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(148, 163, 184, 52))
        p.drawRoundedRect(bx, by, bw, bh, 4, 4)
        p.setBrush(QColor(self.accent))
        p.drawRoundedRect(bx, by, int(bw * self.progress), bh, 4, 4)
        p.setPen(QColor(theme.text))
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.drawText(self.width() - 38, 33, f"{int(self.progress * 100)}%")


class CompactProgressPill(QWidget):
    def __init__(
        self,
        title: str,
        subtitle: str,
        progress: float,
        accent: str,
        theme_provider: Callable[[], Theme],
    ):
        super().__init__()
        self.title, self.subtitle, self.progress, self.accent = (
            title,
            subtitle,
            max(0, min(1, progress)),
            accent,
        )
        self.theme_provider = (
            theme_provider
            if callable(theme_provider)
            else (lambda theme=theme_provider: theme)
        )
        self.setMinimumHeight(50)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        theme = self.theme_provider()
        p.fillRect(self.rect(), QColor("#182033") if theme.dark else QColor("#F8FBFF"))
        r = self.rect().adjusted(0, 2, -2, -2)
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), 14, 14)
        p.fillPath(
            path,
            QColor(255, 255, 255, 92) if not theme.dark else QColor(255, 255, 255, 18),
        )

        # Text uses the left side; progress/percentage stays close to it instead of at the far edge.
        text_w = max(112, min(190, int(self.width() * 0.42)))
        bar_x = text_w + 18
        pct_w = 36
        bar_w = max(46, self.width() - bar_x - pct_w - 18)
        bar_w = min(bar_w, 118)

        p.setPen(QColor(theme.text))
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.drawText(12, 18, self.title)
        p.setPen(QColor(theme.muted))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(12, 37, self.subtitle)

        by, bh = 23, 8
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(148, 163, 184, 55))
        p.drawRoundedRect(bar_x, by, bar_w, bh, 4, 4)
        p.setBrush(QColor(self.accent))
        p.drawRoundedRect(bar_x, by, int(bar_w * self.progress), bh, 4, 4)
        p.setPen(QColor(theme.text))
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        p.drawText(bar_x + bar_w + 8, by + 8, f"{int(self.progress * 100)}%")
