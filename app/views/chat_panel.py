"""Dockable chat panel for the built-in AI assistant.

The panel keeps a short role/text history, spins up an `AgentWorker`
thread per user turn, and hosts the MCP context lazily (created and
rescanned in the worker thread on first use).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from PySide6.QtCore import QEvent, QObject, Qt, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ai.agent import CANCELLED_TEXT, AgentWorker
from app.ai.client import AIClient
from app.ai.prompt import effective_system_prompt
from app.models.settings import Settings
from app.utils.event_bus import EventBus
from app.views.dialogue import BinaryChoiceDialog

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from app.mcp.context import MCPContext


class ChatPanel(QWidget):
    """Chat UI: history view, input box, toolbar buttons."""

    def __init__(
        self,
        settings: Settings,
        show_settings_dialog: Callable[..., None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._show_settings_dialog = show_settings_dialog
        self._history: list[tuple[str, str]] = []
        self._worker: AgentWorker | None = None
        self._ctx: MCPContext | None = None
        self._server: MCPServer | None = None
        self._dirty = True
        self._server_lock = threading.Lock()

        self._build_ui()
        EventBus().settings_have_changed.connect(self._mark_dirty)
        EventBus().do_refresh_mods_lists.connect(self._mark_dirty)
        if not self.settings.ai_api_key.strip():
            self._append_activity(
                "Set the API key and model in Settings -> AI Assistant first."
            )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.status_label = QLabel(self)
        layout.addWidget(self.status_label)

        self.history_view = QTextBrowser(self)
        self.history_view.setOpenExternalLinks(True)
        self.history_view.setReadOnly(True)
        layout.addWidget(self.history_view, stretch=1)

        self.input = QPlainTextEdit(self)
        self.input.setPlaceholderText(
            self.tr("Ask about your mods... (Enter to send, Shift+Enter for newline)")
        )
        self.input.setMinimumHeight(52)
        self.input.setMaximumHeight(140)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)

        buttons = QHBoxLayout()
        self.settings_button = QPushButton(self.tr("Settings"), self)
        self.clear_button = QPushButton(self.tr("Clear"), self)
        self.stop_button = QPushButton(self.tr("Stop"), self)
        self.send_button = QPushButton(self.tr("Send"), self)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.settings_button)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.send_button)
        layout.addLayout(buttons)

        self.settings_button.clicked.connect(self._open_settings)
        self.clear_button.clicked.connect(self._clear)
        self.stop_button.clicked.connect(self._request_stop)
        self.send_button.clicked.connect(self._send)

    def focus_input(self) -> None:
        """Give keyboard focus to the message box (used by the View menu)."""
        self.input.setFocus()

    def shutdown(self) -> None:
        """Stop any running worker before the window is destroyed."""
        worker = self._worker
        if worker is None:
            return
        worker.request_cancel()
        worker.resolve_confirmation(False)
        if not worker.wait(5000):
            logger.warning("AI chat worker still running at shutdown; waiting")
            worker.wait()

    @Slot()
    def _mark_dirty(self) -> None:
        self._dirty = True

    @Slot()
    def _send(self) -> None:
        if self._worker is not None:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self.settings.ai_api_key.strip() or not self.settings.ai_model.strip():
            self._append_error(
                self.tr("Set the API key and model in Settings -> AI Assistant first.")
            )
            self._open_settings()
            return
        self.input.clear()
        self._history.append(("user", text))
        self._append_user(text)
        api_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": effective_system_prompt(self.settings.ai_system_prompt),
            }
        ]
        api_messages.extend(
            {"role": role, "content": content} for role, content in self._history
        )
        client = AIClient(
            base_url=self.settings.ai_base_url,
            api_key=self.settings.ai_api_key,
            model=self.settings.ai_model,
        )
        self._set_busy(True)
        worker = AgentWorker(
            client=client,
            server_provider=self._provide_server,
            messages=api_messages,
        )
        worker.activity.connect(self._append_activity)
        worker.confirm_requested.connect(self._on_confirm_requested)
        worker.finished_ok.connect(self._on_finished_ok)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    @Slot()
    def _request_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_cancel()

    @Slot()
    def _clear(self) -> None:
        if self._worker is not None:
            return
        self._history.clear()
        self.history_view.clear()

    @Slot()
    def _open_settings(self) -> None:
        self._show_settings_dialog("AI Assistant")

    @Slot(str)
    def _on_finished_ok(self, text: str) -> None:
        if text and text != CANCELLED_TEXT:
            self._history.append(("assistant", text))
        self._append_assistant(text)

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._append_error(message)

    @Slot()
    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()

    @Slot(str)
    def _on_confirm_requested(self, pretty: str) -> None:
        if self._worker is None:
            return
        answer = BinaryChoiceDialog(
            title=self.tr("Confirm AI action"),
            text=self.tr("The assistant wants to run:") + f"\n\n{pretty}",
            information=self.tr(
                "This changes your mod data (ModsConfig.xml, modpacks) or launches "
                "the game. Approve only if you asked for this."
            ),
            positive_text=self.tr("Run"),
            negative_text=self.tr("Cancel"),
            parent=self,
        )
        self._worker.resolve_confirmation(answer.exec_is_positive())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.input and event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            ):
                self._send()
                return True
        return super().eventFilter(watched, event)

    def _provide_server(self) -> MCPServer:
        """Create the MCP context/server on first use and rescan when dirty."""
        from app.mcp.context import MCPContext
        from app.mcp.server import build_server

        with self._server_lock:
            ctx = self._ctx
            if ctx is None:
                ctx = MCPContext()
                self._ctx = ctx
            if self._server is None:
                self._server = build_server(ctx)
                self._dirty = True
            if self._dirty:
                try:
                    ctx.refresh()
                    self._dirty = False
                except Exception:
                    logger.exception("AI chat: mod rescan failed; using cached data")
            server = self._server
        if server is None:  # pragma: no cover - guarded by assignment above
            raise RuntimeError("AI chat: MCP server unavailable")
        return server

    def _set_busy(self, busy: bool) -> None:
        self.input.setEnabled(not busy)
        self.send_button.setEnabled(not busy)
        self.clear_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.status_label.setText(self.tr("Thinking...") if busy else "")

    def _append_user(self, text: str) -> None:
        self.history_view.append(
            f'<p style="font-weight:bold;">{escape(text).replace(chr(10), "<br>")}</p>'
        )

    def _append_assistant(self, text: str) -> None:
        self.history_view.append(
            f'<p style="color:#1a5fb4;">{escape(text).replace(chr(10), "<br>")}</p>'
        )

    def _append_activity(self, text: str) -> None:
        self.history_view.append(
            '<p style="color:gray; font-family:monospace; font-size:11px;">'
            f"{escape(text)}</p>"
        )

    def _append_error(self, text: str) -> None:
        self.history_view.append(
            f'<p style="color:#c01c28;">{escape(text).replace(chr(10), "<br>")}</p>'
        )
