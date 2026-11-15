"""Shutdown controls stay usable while the data-bound main window is disabled."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout


class ShutdownWaitDialog(QDialog):
    cancel_shutdown = Signal()
    request_task_cancellation = Signal()

    def __init__(self) -> None:
        # An independent top-level owner remains enabled when the app is disabled.
        super().__init__(None)
        self.setWindowTitle("Encerramento em curso — Finanças Pessoais")
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        self.pending = QLabel()
        self.pending.setTextFormat(Qt.PlainText)
        self.pending.setWordWrap(True)
        self.pending.setAccessibleName("Estado do trabalho pendente")
        layout.addWidget(self.pending)
        explanation = QLabel(
            "Os trabalhos em execução ainda estão a terminar. Não é seguro fechar "
            "os dados durante uma escrita. Podes pedir cancelamento às tarefas que "
            "o suportam ou cancelar o encerramento; a aplicação retoma quando "
            "os trabalhos terminarem."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        buttons = QVBoxLayout()
        self.cancel_tasks = QPushButton("Pedir cancelamento das tarefas")
        self.cancel_tasks.clicked.connect(self.request_task_cancellation)
        buttons.addWidget(self.cancel_tasks)
        self.cancel_close = QPushButton("Cancelar encerramento")
        self.cancel_close.clicked.connect(self.cancel_shutdown)
        buttons.addWidget(self.cancel_close)
        layout.addLayout(buttons)
        self.setTabOrder(self.cancel_tasks, self.cancel_close)
        screen = self.screen()
        available = screen.availableGeometry().width() if screen is not None else 600
        self.resize(max(280, min(520, available - 32)), self.sizeHint().height())

    def update_pending(
        self, message: str, *, cancellation_pending: bool = False
    ) -> None:
        self.pending.setText(message)
        self.cancel_close.setEnabled(not cancellation_pending)
        if cancellation_pending:
            self.cancel_close.setText("A aguardar para retomar…")

    def reject(self) -> None:
        # Closing this control requests cancellation; it never exits the process.
        self.cancel_shutdown.emit()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.cancel_shutdown.emit()
