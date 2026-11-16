"""Internal, non-executing preview for verified images and PDF attachments."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class AttachmentPreviewDialog(QDialog):
    """Render a verified attachment without invoking an external program."""

    MAX_IMAGE_PIXELS = 50_000_000

    def __init__(
        self,
        path: Path,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Pré-visualização segura · {self.path.name}")
        self.setModal(True)
        self.resize(920, 720)
        self.setMinimumSize(620, 480)
        self._image_pixmap = QPixmap()
        self._image_scale = 1.0
        self._image_label: QLabel | None = None
        self._pdf_document = None
        self._pdf_view = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        heading = QLabel(self.path.name)
        heading.setTextFormat(Qt.PlainText)
        heading.setObjectName("SectionTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        suffix = self.path.suffix.casefold()
        if suffix == ".pdf":
            content, toolbar = self._build_pdf_preview()
        else:
            content, toolbar = self._build_image_preview()
        if toolbar is not None:
            layout.addLayout(toolbar)
        layout.addWidget(content, 1)

        note = QLabel(
            "🔒 O ficheiro foi verificado por tamanho e SHA-256 e está a ser "
            "mostrado dentro da aplicação."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _error_widget(message: str) -> QLabel:
        label = QLabel(message)
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        label.setObjectName("EmptyState")
        return label

    def _build_image_preview(self) -> tuple[QWidget, QHBoxLayout | None]:
        reader = QImageReader(str(self.path))
        size = reader.size()
        if (
            not size.isValid()
            or size.width() <= 0
            or size.height() <= 0
            or size.width() * size.height() > self.MAX_IMAGE_PIXELS
        ):
            return (
                self._error_widget(
                    "A imagem tem dimensões inválidas ou excessivas e não foi aberta."
                ),
                None,
            )
        pixmap = QPixmap.fromImageReader(reader)
        if pixmap.isNull():
            return (
                self._error_widget("Não foi possível interpretar esta imagem."),
                None,
            )
        self._image_pixmap = pixmap
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        self._image_label.setPixmap(pixmap)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setWidget(self._image_label)
        toolbar = QHBoxLayout()
        zoom_out = QPushButton("−")
        zoom_in = QPushButton("+")
        fit = QPushButton("Ajustar")
        zoom_out.clicked.connect(lambda: self._scale_image(0.8))
        zoom_in.clicked.connect(lambda: self._scale_image(1.25))
        fit.clicked.connect(lambda: self._fit_image(scroll))
        toolbar.addStretch(1)
        toolbar.addWidget(zoom_out)
        toolbar.addWidget(zoom_in)
        toolbar.addWidget(fit)
        return scroll, toolbar

    def _apply_image_scale(self) -> None:
        if self._image_label is None or self._image_pixmap.isNull():
            return
        target = self._image_pixmap.size() * self._image_scale
        self._image_label.setPixmap(
            self._image_pixmap.scaled(
                target,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        self._image_label.adjustSize()

    def _scale_image(self, factor: float) -> None:
        self._image_scale = max(0.1, min(6.0, self._image_scale * factor))
        self._apply_image_scale()

    def _fit_image(self, scroll: QScrollArea) -> None:
        if self._image_pixmap.isNull():
            return
        viewport = scroll.viewport().size()
        self._image_scale = min(
            1.0,
            max(
                0.1,
                min(
                    viewport.width() / max(1, self._image_pixmap.width()),
                    viewport.height() / max(1, self._image_pixmap.height()),
                ),
            ),
        )
        self._apply_image_scale()

    def _build_pdf_preview(self) -> tuple[QWidget, QHBoxLayout | None]:
        try:
            from PySide6.QtPdf import QPdfDocument
            from PySide6.QtPdfWidgets import QPdfView
        except ImportError:
            return (
                self._error_widget(
                    "O visualizador PDF interno não está disponível nesta instalação."
                ),
                None,
            )
        document = QPdfDocument(self)
        error = document.load(str(self.path))
        if error != QPdfDocument.Error.None_:
            return (
                self._error_widget("O PDF não pôde ser interpretado com segurança."),
                None,
            )
        view = QPdfView(self)
        view.setDocument(document)
        view.setPageMode(QPdfView.PageMode.MultiPage)
        view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self._pdf_document = document
        self._pdf_view = view
        toolbar = QHBoxLayout()
        zoom_out = QPushButton("−")
        zoom_in = QPushButton("+")
        fit = QPushButton("Ajustar à largura")
        zoom_out.clicked.connect(lambda: self._scale_pdf(0.85))
        zoom_in.clicked.connect(lambda: self._scale_pdf(1.18))
        fit.clicked.connect(lambda: view.setZoomMode(QPdfView.ZoomMode.FitToWidth))
        toolbar.addStretch(1)
        toolbar.addWidget(zoom_out)
        toolbar.addWidget(zoom_in)
        toolbar.addWidget(fit)
        return view, toolbar

    def _scale_pdf(self, factor: float) -> None:
        if self._pdf_view is None:
            return
        from PySide6.QtPdfWidgets import QPdfView

        self._pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
        self._pdf_view.setZoomFactor(
            max(0.2, min(5.0, self._pdf_view.zoomFactor() * factor))
        )

    def _release_pdf(self) -> None:
        # Detach the view before closing its document so pending native page
        # renders cannot outlive the document during dialog destruction.
        if self._pdf_view is not None:
            self._pdf_view.setDocument(None)
        if self._pdf_document is not None:
            self._pdf_document.close()
        self._pdf_view = None
        self._pdf_document = None

    def done(self, result: int) -> None:
        self._release_pdf()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._release_pdf()
        super().closeEvent(event)


__all__ = ["AttachmentPreviewDialog"]
