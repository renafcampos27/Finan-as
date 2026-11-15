"""Profile-scoped, current-user-only activation; QLockFile remains authoritative."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from utils.app_logging import log_warning
from utils.qt_runtime import is_qt_valid

_REQUEST = b"ACTIVATE\n"
_ACK = b"ACTIVATED\n"


def activation_endpoint(lock_path: Path) -> str:
    identity = os.path.normcase(str(lock_path.resolve()))
    return (
        "financas-activate-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    )


class InstanceActivationServer(QObject):
    """Only activation is accepted; this endpoint cannot read or mutate data."""

    def __init__(self, lock_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window: QWidget | None = None
        self._pending = False
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        endpoint = activation_endpoint(lock_path)
        if not self.server.listen(endpoint):
            # The caller already owns the profile's QLockFile. A stale endpoint
            # from its previous process can therefore be removed here only.
            QLocalServer.removeServer(endpoint)
            if not self.server.listen(endpoint):
                log_warning(
                    "Cannot open current-profile activation channel: %s",
                    self.server.errorString(),
                )

    def set_window(self, window: QWidget) -> None:
        self._window = window
        if self._pending:
            self._activate()

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            socket.setReadBufferSize(64)
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(socket.deleteLater)
            QTimer.singleShot(1000, socket, socket.disconnectFromServer)
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        if socket.bytesAvailable() > len(_REQUEST):
            socket.disconnectFromServer()
            return
        if not socket.canReadLine():
            return
        if bytes(socket.readLine(64)) != _REQUEST:
            socket.disconnectFromServer()
            return
        self._activate()
        socket.write(_ACK)
        socket.flush()
        socket.disconnectFromServer()

    def _activate(self) -> None:
        app = QApplication.instance()
        active = app.activeModalWidget() if app is not None else None
        window = active if active is not None else self._window
        if not is_qt_valid(window):
            self._pending = True
            return
        self._pending = False
        if window.isMinimized():
            window.showNormal()
        else:
            window.show()
        window.raise_()
        window.activateWindow()


def request_instance_activation(lock_path: Path, timeout_ms: int = 400) -> bool:
    """Request activation only after losing the profile lock; no startup writes."""
    socket = QLocalSocket()
    socket.connectToServer(activation_endpoint(lock_path))
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(_REQUEST)
    socket.flush()
    if not socket.bytesAvailable() and not socket.waitForReadyRead(timeout_ms):
        socket.disconnectFromServer()
        return False
    acknowledged = bytes(socket.readLine(64)) == _ACK
    socket.disconnectFromServer()
    return acknowledged
