"""Local IPC so a second process can hand off to the running GUI (Stage G).

The primary RimSort instance listens on a per-user ``QLocalServer``. A second
launch with ``--attach-gui`` connects, sends a ``raise`` command and exits, so
the user lands on the already-running window instead of an error dialog. The
transport is a local socket only — never a TCP listener on any interface.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

#: Single command understood by the server.
CMD_RAISE = b"raise\n"

#: How long a client waits for the write to flush, ms.
_WRITE_TIMEOUT_MS = 500


def gui_socket_name(storage_folder: Path | None = None) -> str:
    """Stable, per-user local socket name.

    ``QLocalServer`` names are system-global on some platforms, so scope the
    name by a hash of the app storage folder (unique per user/install) to avoid
    one user's instance stealing another's attach.
    """
    if storage_folder is None:
        from app.utils.app_info import AppInfo

        storage_folder = AppInfo().app_storage_folder
    digest = hashlib.sha256(str(storage_folder).encode("utf-8")).hexdigest()[:16]
    return f"rimsort-gui-{digest}"


class GuiIpcServer(QObject):
    """Listens for attach requests and emits ``raise_requested`` (Stage G).

    Lives in the main thread; the slot that raises the window runs there too.
    """

    #: Emitted in the main thread when a client asks to raise the window.
    raise_requested = Signal()

    def __init__(self, parent: QObject | None = None, name: str | None = None) -> None:
        super().__init__(parent)
        self._server: QLocalServer | None = None
        self._name = name if name is not None else gui_socket_name()

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    def start(self) -> bool:
        """Start listening; clears any stale socket from a crashed instance."""
        if self._server is not None:
            return self.is_listening
        # A leftover socket file from a crashed run would block listen().
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        try:
            server.listen(self._name)
        except OSError as exc:
            logger.warning(f"GUI IPC server could not listen on {self._name}: {exc}")
            return False
        self._server = server
        logger.info(f"GUI IPC server listening on {self._name}")
        return True

    def stop(self) -> None:
        """Stop listening and remove the socket file."""
        if self._server is not None:
            self._server.close()
            self._server = None
        QLocalServer.removeServer(self._name)

    def _on_new_connection(self) -> None:
        assert self._server is not None
        while True:
            socket = self._server.nextPendingConnection()
            if socket is None:
                break
            socket.readyRead.connect(lambda s=socket: self._handle_client(s))
            # Handle clients that already sent data before we connected.
            if socket.bytesAvailable():
                self._handle_client(socket)

    def _handle_client(self, socket: QLocalSocket) -> None:
        raw: bytes = socket.readAll().data()
        data = raw.decode("utf-8", errors="replace").strip()
        socket.disconnectFromServer()
        if data == "raise":
            logger.info("GUI IPC: raise requested by a client")
            self.raise_requested.emit()


def request_gui_raise(
    timeout_ms: int = _WRITE_TIMEOUT_MS, name: str | None = None
) -> bool:
    """Ask a running instance to raise its window.

    :return: True if a running instance accepted the request, False when none
    is listening (or the write timed out).
    """
    if name is None:
        name = gui_socket_name()
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(CMD_RAISE)
    socket.flush()
    written = socket.waitForBytesWritten(timeout_ms)
    socket.disconnectFromServer()
    if not written:
        logger.warning("GUI IPC: timed out writing raise request")
    return True


__all__ = ["CMD_RAISE", "GuiIpcServer", "gui_socket_name", "request_gui_raise"]
