"""Local IPC so a second process can hand off to the running GUI (Stage G).

The primary RimSort instance listens on a per-user ``QLocalServer``. A second
launch with ``--attach-gui`` connects, sends a ``raise`` command and exits, so
the user lands on the already-running window instead of an error dialog. The
transport is a local socket only — never a TCP listener on any interface.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, QTimer, Signal
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
        self.bridge: Any = None
        self._buffers: dict[QLocalSocket, bytearray] = {}

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
        server.setMaxPendingConnections(32)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._on_new_connection)
        try:
            if not server.listen(self._name):
                logger.warning(
                    "GUI IPC server could not listen on {}: {}",
                    self._name,
                    server.errorString(),
                )
                server.deleteLater()
                return False
        except OSError as exc:
            logger.warning(f"GUI IPC server could not listen on {self._name}: {exc}")
            server.deleteLater()
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
            if len(self._buffers) >= 32:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(1024 * 1024)
            deadline = QTimer(socket)
            deadline.setSingleShot(True)
            deadline.timeout.connect(socket.abort)
            deadline.start(30000)
            self._buffers[socket] = bytearray()
            socket.disconnected.connect(lambda s=socket: self._cleanup_client(s))
            socket.readyRead.connect(lambda s=socket: self._handle_client(s))
            # Handle clients that already sent data before we connected.
            if socket.bytesAvailable():
                self._handle_client(socket)

    def _handle_client(self, socket: QLocalSocket) -> None:
        buffer = self._buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(socket.readAll().data())
        if len(buffer) > 1024 * 1024:
            socket.abort()
            return
        if b"\n" not in buffer:
            return
        data = bytes(buffer).split(b"\n", 1)[0].decode("utf-8", errors="replace")
        self._buffers.pop(socket, None)
        if data == "raise":
            logger.info("GUI IPC: raise requested by a client")
            self.raise_requested.emit()
            socket.disconnectFromServer()
            return
        try:
            request = json.loads(data)
            if not isinstance(request, dict) or request.get("version") != 1:
                raise ValueError("Invalid IPC request")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise TypeError("Invalid action parameters")
            if request.get("action") == "raise":
                self.raise_requested.emit()
                result = {"ok": True}
            else:
                result = (
                    self.bridge.submit(str(request.get("action", "")), params)
                    if self.bridge is not None
                    else {"ok": False, "code": "gui_unavailable"}
                )
        except (ValueError, TypeError):
            result = {"ok": False, "code": "gui_invalid_request"}
        socket.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
        socket.flush()
        socket.disconnectFromServer()

    def _cleanup_client(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()


def request_gui_raise(
    timeout_ms: int = _WRITE_TIMEOUT_MS, name: str | None = None
) -> bool:
    """Ask a running instance to raise its window.

    :return: True if a running instance accepted the request, False when none
    is listening (or the write timed out).
    """
    return bool(
        RemoteGuiBridge(name).submit("raise", timeout=timeout_ms / 1000).get("ok")
    )


class RemoteGuiBridge:
    """Bounded semantic actions over the desktop's per-user local socket.

    A failed or timed-out write is never retried: it may already have executed.
    Works with Qt local sockets on Windows, macOS and Linux.
    """

    def __init__(self, name: str | None = None) -> None:
        self.name = name or gui_socket_name()

    def submit(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        socket = QLocalSocket()
        deadline = time.monotonic() + timeout

        def remaining() -> int:
            return max(1, int((deadline - time.monotonic()) * 1000))

        try:
            socket.connectToServer(self.name)
            while (
                socket.state() == QLocalSocket.LocalSocketState.ConnectingState
                and time.monotonic() < deadline
            ):
                socket.waitForConnected(min(20, remaining()))
                time.sleep(0.001)
            if socket.state() != QLocalSocket.LocalSocketState.ConnectedState:
                return {
                    "ok": False,
                    "code": "gui_unavailable",
                    "error": "Откройте RimSort для управления интерфейсом.",
                }
            payload = (
                json.dumps(
                    {"version": 1, "action": action, "params": params or {}},
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > 1024 * 1024:
                return {"ok": False, "code": "gui_request_too_large"}
            socket.write(payload)
            socket.flush()
            while socket.bytesToWrite() and time.monotonic() < deadline:
                socket.waitForBytesWritten(min(20, remaining()))
                time.sleep(0.001)
            response = bytearray()
            while time.monotonic() < deadline:
                response.extend(socket.readAll().data())
                if len(response) > 1024 * 1024:
                    return {"ok": False, "code": "gui_response_too_large"}
                if b"\n" in response:
                    result = json.loads(bytes(response).split(b"\n", 1)[0])
                    if isinstance(result, dict):
                        return result
                    break
                if not socket.waitForReadyRead(min(20, remaining())):
                    response.extend(socket.readAll().data())
                    if (
                        b"\n" not in response
                        and socket.state()
                        == QLocalSocket.LocalSocketState.UnconnectedState
                    ):
                        break
                time.sleep(0.001)  # Allow Python GUI slots in same-process tests.
            return {
                "ok": False,
                "code": "gui_timeout",
                "error": "Нет подтверждения от RimSort. Проверьте состояние перед повтором действия.",
                "socket_error": socket.errorString(),
                "pending_bytes": socket.bytesToWrite(),
            }
        except (ValueError, OSError):
            return {"ok": False, "code": "gui_invalid_response"}
        finally:
            socket.abort()

    def get_state(self, detail: str = "summary") -> dict[str, Any]:
        return self.submit("get_state", {"detail": detail})


__all__ = ["CMD_RAISE", "GuiIpcServer", "gui_socket_name", "request_gui_raise"]
