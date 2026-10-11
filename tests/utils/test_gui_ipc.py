"""Tests for the local GUI IPC bridge (Stage G)."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from app.utils.gui_ipc import GuiIpcServer, gui_socket_name, request_gui_raise


def test_socket_name_is_stable_and_scoped(tmp_path: Path) -> None:
    name1 = gui_socket_name(tmp_path / "a")
    name2 = gui_socket_name(tmp_path / "a")
    other = gui_socket_name(tmp_path / "b")
    assert name1 == name2  # stable for the same folder
    assert name1 != other  # scoped per folder (per user/install)
    assert name1.startswith("rimsort-gui-")


def test_request_raise_false_when_no_server(tmp_path: Path) -> None:
    """With nothing listening, the client reports no instance to attach to."""
    assert request_gui_raise(timeout_ms=50, name="rimsort-gui-missing") is False


def test_server_start_stop_and_round_trip(
    qtbot: object, qapp: QCoreApplication, tmp_path: Path
) -> None:
    """A client's raise request reaches a listening server and is acknowledged."""
    name = f"rimsort-gui-test-{tmp_path.name}"
    server = GuiIpcServer(name=name)
    received: list[bool] = []
    server.raise_requested.connect(lambda: received.append(True))
    assert server.start() is True
    assert server.is_listening is True

    box: dict[str, bool] = {}

    def client() -> None:
        box["ok"] = request_gui_raise(timeout_ms=2000, name=name)

    thread = threading.Thread(target=client)
    thread.start()
    # Pump the Qt event loop so the server accepts and handles the connection.
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=3000)  # type: ignore[attr-defined]
    thread.join(timeout=1.0)
    qtbot.wait(50)  # type: ignore[attr-defined]  # let the server slot run

    assert box["ok"] is True
    assert received == [True]

    server.stop()
    assert server.is_listening is False


def test_server_ignores_unknown_command(
    qtbot: object, qapp: QCoreApplication, tmp_path: Path
) -> None:
    """Only the 'raise' command emits; other payloads are ignored."""
    name = f"rimsort-gui-ign-{tmp_path.name}"
    server = GuiIpcServer(name=name)
    received: list[bool] = []
    server.raise_requested.connect(lambda: received.append(True))
    assert server.start() is True

    from PySide6.QtNetwork import QLocalSocket

    def client() -> None:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(2000):
            socket.write(b"something-else\n")
            socket.flush()
            socket.waitForBytesWritten(2000)
            socket.disconnectFromServer()

    thread = threading.Thread(target=client)
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=3000)  # type: ignore[attr-defined]
    thread.join(timeout=1.0)
    qtbot.wait(50)  # type: ignore[attr-defined]

    assert received == []  # no raise for an unknown command
    server.stop()
