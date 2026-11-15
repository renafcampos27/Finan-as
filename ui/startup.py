"""Compact startup window with progress from real protection operations."""

from __future__ import annotations

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
)

from utils.startup_progress import StartupProgress
from utils.version import APP_VERSION_LABEL


def prepare_foreground_dialog(
    dialog: QDialog,
    startup: "StartupProgressWindow | None" = None,
) -> QDialog:
    """Keep a recovery/instance warning above the topmost startup window."""

    dialog.setWindowModality(Qt.ApplicationModal)
    dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    if startup is not None:
        startup.show()
        startup.raise_()
        startup.activateWindow()
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    app = QApplication.instance()
    if app is not None:
        app.processEvents(QEventLoop.AllEvents, 25)
    return dialog


def exec_foreground_message(
    startup: "StartupProgressWindow | None",
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    *,
    informative_text: str = "",
    buttons: QMessageBox.StandardButtons = QMessageBox.Ok,
    default_button: QMessageBox.StandardButton = QMessageBox.NoButton,
) -> QMessageBox.StandardButton:
    """Execute a readable application-modal message in the foreground."""

    dialog = QMessageBox(startup)
    dialog.setIcon(icon)
    dialog.setWindowTitle(str(title))
    dialog.setText(str(text))
    if informative_text:
        dialog.setInformativeText(str(informative_text))
    dialog.setStandardButtons(buttons)
    if default_button != QMessageBox.NoButton:
        dialog.setDefaultButton(default_button)
    prepare_foreground_dialog(dialog, startup)
    return QMessageBox.StandardButton(dialog.exec())


class StartupProgressWindow(QDialog):
    """Small non-interruptible window shown while stored data is protected."""

    def __init__(self) -> None:
        super().__init__(None)
        self._allow_close = False
        self._percent = 0
        self.setObjectName("StartupProgressWindow")
        self.setWindowTitle("Finanças Pessoais")
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(520, 270)
        self.setAccessibleName("Progresso de arranque")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        card = QFrame()
        card.setObjectName("StartupCard")
        card.setAttribute(Qt.WA_StyledBackground, True)
        root.addWidget(card)

        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(38)
        shadow.setOffset(0, 10)
        shadow.setColor(self._shadow_color())
        card.setGraphicsEffect(shadow)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(14)
        self.icon = QLabel("💶")
        self.icon.setObjectName("StartupIcon")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setFixedSize(54, 54)
        header.addWidget(self.icon)

        names = QVBoxLayout()
        names.setSpacing(2)
        title = QLabel("Finanças Pessoais")
        title.setObjectName("StartupTitle")
        subtitle = QLabel(f"Liquid Glass  ·  {APP_VERSION_LABEL}")
        subtitle.setObjectName("StartupSubtitle")
        names.addWidget(title)
        names.addWidget(subtitle)
        header.addLayout(names, 1)

        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("StartupPercent")
        self.percent_label.setAlignment(Qt.AlignCenter)
        self.percent_label.setFixedSize(58, 34)
        header.addWidget(self.percent_label, 0, Qt.AlignTop)
        layout.addLayout(header)

        self.stage_label = QLabel("A preparar a aplicação")
        self.stage_label.setObjectName("StartupStage")
        self.stage_label.setWordWrap(True)
        self.stage_label.setAccessibleName("Etapa atual")
        layout.addWidget(self.stage_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("StartupProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(12)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("A confirmar a localização segura dos teus dados…")
        self.detail_label.setObjectName("StartupDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setMinimumHeight(36)
        self.detail_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.detail_label.setAccessibleName("Detalhe da etapa atual")
        layout.addWidget(self.detail_label)

        safety = QLabel(
            "🛡️  Os dados existentes nunca são substituídos sem verificação."
        )
        safety.setObjectName("StartupSafety")
        safety.setWordWrap(True)
        layout.addWidget(safety)

        self._apply_style()

    def _is_dark(self) -> bool:
        app = QApplication.instance()
        return bool(
            app is not None and app.palette().window().color().lightness() < 128
        )

    def _shadow_color(self):
        from PySide6.QtGui import QColor

        return QColor(5, 18, 55, 145 if self._is_dark() else 72)

    def _apply_style(self) -> None:
        if self._is_dark():
            surface = "#111A2D"
            border = "#3C4C70"
            title = "#F5F8FF"
            body = "#DCE6FA"
            subtle = "#9EB0D1"
            track = "#263552"
            chip = "#203B67"
            accent = "#66B3FF"
        else:
            surface = "#F8FAFF"
            border = "#C9D8F2"
            title = "#071A3D"
            body = "#19345F"
            subtle = "#60779D"
            track = "#DFE9F8"
            chip = "#E0EEFF"
            accent = "#2788F5"
        self.setStyleSheet(
            f"""
            QFrame#StartupCard {{
                background-color: {surface};
                border: 1px solid {border};
                border-radius: 24px;
            }}
            QLabel {{
                color: {body};
                background: transparent;
                border: none;
            }}
            QLabel#StartupIcon {{
                background-color: {chip};
                border: 1px solid {border};
                border-radius: 18px;
                font-size: 25px;
            }}
            QLabel#StartupTitle {{
                color: {title};
                font-size: 22px;
                font-weight: 800;
            }}
            QLabel#StartupSubtitle {{
                color: {subtle};
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel#StartupPercent {{
                color: {accent};
                background-color: {chip};
                border: 1px solid {border};
                border-radius: 12px;
                font-size: 13px;
                font-weight: 800;
            }}
            QLabel#StartupStage {{
                color: {title};
                font-size: 15px;
                font-weight: 750;
            }}
            QLabel#StartupDetail {{
                color: {subtle};
                font-size: 12px;
            }}
            QLabel#StartupSafety {{
                color: {body};
                background-color: {chip};
                border-radius: 10px;
                padding: 7px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            QProgressBar#StartupProgress {{
                background-color: {track};
                border: none;
                border-radius: 6px;
            }}
            QProgressBar#StartupProgress::chunk {{
                background-color: {accent};
                border-radius: 6px;
            }}
            """
        )

    def showEvent(self, event: QShowEvent) -> None:  # type: ignore[override]
        super().showEvent(event)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.x() + max(0, (area.width() - self.width()) // 2),
            area.y() + max(0, (area.height() - self.height()) // 2),
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if self._allow_close:
            super().closeEvent(event)
        else:
            event.ignore()

    def update_progress(self, progress: StartupProgress) -> None:
        self._percent = max(self._percent, int(progress.percent))
        self.progress_bar.setValue(self._percent)
        self.percent_label.setText(f"{self._percent}%")
        self.stage_label.setText(progress.stage)
        detail = progress.detail
        if progress.total > 0:
            counter = f"{min(progress.current, progress.total)}/{progress.total}"
            detail = f"{counter}  ·  {detail}" if detail else counter
        self.detail_label.setText(detail or "Operação concluída.")
        self.stage_label.setAccessibleDescription(self.detail_label.text())
        app = QApplication.instance()
        if app is not None:
            app.processEvents(QEventLoop.ExcludeUserInputEvents, 25)

    def show_error(self, detail: str) -> None:
        self.stage_label.setText("Não foi possível concluir o arranque")
        self.detail_label.setText(str(detail))
        self.percent_label.setText("!")
        self.percent_label.setStyleSheet(
            "color: #B42318; background-color: #FEE4E2;"
            "border: 1px solid #FDA29B; border-radius: 12px;"
        )
        app = QApplication.instance()
        if app is not None:
            app.processEvents(QEventLoop.ExcludeUserInputEvents, 25)

    def finish(self) -> None:
        self.update_progress(
            StartupProgress(
                100,
                "Tudo pronto",
                "A abrir o Dashboard…",
            )
        )
        self._allow_close = True
        self.close()

    def dismiss(self) -> None:
        self._allow_close = True
        self.close()
