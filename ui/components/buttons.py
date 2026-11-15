#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Finanças Pessoais – Liquid Glass
Nova interface PySide6 para a app local de finanças pessoais.
Mantém a base de dados e a app clássica intactas; esta é apenas uma camada visual moderna.
"""

from __future__ import annotations

import sys

from utils.qt_runtime import configure_qt_runtime

configure_qt_runtime()

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
except (
    Exception
) as exc:  # pragma: no cover - only used when PySide6 is missing on the user's PC.
    print("PySide6 não está instalado. Instala com: pip install PySide6")
    print(exc)
    sys.exit(1)


class ChipButton(QPushButton):
    def __init__(self, text: str, checked: bool = False):
        super().__init__(text)
        self.setObjectName("Chip")
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
