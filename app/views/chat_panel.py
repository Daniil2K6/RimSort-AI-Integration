"""Dockable chat panel for the built-in AI assistant.

The panel keeps one active :class:`ChatSession` at a time. Sessions are
persisted as JSON files by :class:`app.ai.chat_store.ChatStore`, models
and providers are managed through :mod:`app.ai.providers`, and each user
turn spins up an `AgentWorker` thread (the MCP context is created and
rescanned lazily inside the worker thread).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from html import escape
from typing import TYPE_CHECKING, Any, cast

from loguru import logger
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, Slot
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ai.agent import CANCELLED_TEXT, AgentWorker
from app.ai.chat_store import ChatMessage, ChatSession, ChatStore, make_title
from app.ai.client import AIClient
from app.ai.prompt import effective_system_prompt
from app.ai.providers import active_provider as get_active_provider
from app.ai.providers import (
    ensure_providers,
    get_provider,
    set_active,
    update_provider,
)
from app.models.settings import Settings
from app.utils.event_bus import EventBus
from app.views.dialogue import BinaryChoiceDialog
from app.windows.ai_provider_dialog import AIProviderDialog, prompt_add_provider

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from app.mcp.context import MCPContext

_FALLBACK_BG = "#ffffff"
_FALLBACK_FG = "#000000"
_FALLBACK_ACCENT = "#346792"
_DARK_LUMINANCE = 0.45


def _parse_hex_color(value: str) -> tuple[int, int, int] | None:
    """Parse ``#rgb``/``#rrggbb`` into an RGB triple."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _mix_colors(first: str, second: str, ratio: float) -> str:
    """Blend two hex colors; returns ``first`` when either is unparseable."""
    rgb_a = _parse_hex_color(first)
    rgb_b = _parse_hex_color(second)
    if rgb_a is None or rgb_b is None:
        return first
    mixed = tuple(round(a + (b - a) * ratio) for a, b in zip(rgb_a, rgb_b, strict=True))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _luminance(color: str) -> float:
    """Relative luminance in 0..1; unparseable colors count as white."""
    rgb = _parse_hex_color(color)
    if rgb is None:
        return 1.0
    red, green, blue = (channel / 255 for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


class ChatPanel(QWidget):
    """Chat UI: history sidebar, message view, input box, provider menu."""

    def __init__(
        self,
        settings: Settings,
        show_settings_dialog: Callable[..., None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._show_settings_dialog = show_settings_dialog
        self._store = ChatStore()
        self._session: ChatSession = self._store.new_session()
        self._worker: AgentWorker | None = None
        self._ctx: MCPContext | None = None
        self._server: MCPServer | None = None
        self._dirty = True
        self._server_lock = threading.Lock()
        self._collapsed = False
        self._model_submenus: list[QMenu] = []

        ensure_providers(self.settings)
        self._build_ui()
        self._refresh_sidebar()
        self._update_title()

        EventBus().settings_have_changed.connect(self._mark_dirty)
        EventBus().do_refresh_mods_lists.connect(self._mark_dirty)

        provider = get_active_provider(self.settings)
        if provider is not None and not self._key_configured(provider):
            self._append_activity(
                self.tr("No API key configured. Use Add Model -> Add Provider first.")
            )

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header = QFrame(self)
        header.setObjectName("ChatHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(2, 2, 2, 2)
        header_layout.setSpacing(4)

        self.history_button = QPushButton(self.tr("History"), header)
        self.history_button.setCheckable(True)
        self.history_button.setChecked(True)
        self.history_button.setToolTip(self.tr("Show or hide the chat history list"))

        self.new_button = QPushButton(self.tr("New Chat"), header)

        self.title_label = QLabel(self.tr("New chat"), header)
        self.title_label.setObjectName("ChatTitle")

        self.model_button = QPushButton(self.tr("Add Model"), header)
        self.model_button.setToolTip(self.tr("Switch models or add a provider"))
        self.model_menu = QMenu(self.model_button)
        self.model_menu.aboutToShow.connect(self._rebuild_model_menu)
        self.model_button.setMenu(self.model_menu)

        self.settings_button = QPushButton(self.tr("Settings"), header)

        self.collapse_button = QPushButton(self.tr("Collapse"), header)
        self.collapse_button.setCheckable(True)
        self.collapse_button.setToolTip(self.tr("Collapse or expand the chat window"))

        header_layout.addWidget(self.history_button)
        header_layout.addWidget(self.new_button)
        header_layout.addWidget(self.title_label, stretch=1)
        header_layout.addWidget(self.model_button)
        header_layout.addWidget(self.settings_button)
        header_layout.addWidget(self.collapse_button)
        layout.addWidget(header)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.history_list = QListWidget(self.splitter)
        self.history_list.setObjectName("ChatHistoryList")
        self.history_list.setMinimumWidth(110)
        self.history_list.setMaximumWidth(240)
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self._show_history_menu)
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        self.splitter.addWidget(self.history_list)

        body = QWidget(self.splitter)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        self.status_label = QLabel(body)
        body_layout.addWidget(self.status_label)

        self.history_view = QTextBrowser(body)
        self.history_view.setObjectName("ChatHistory")
        self.history_view.setOpenExternalLinks(True)
        self.history_view.setReadOnly(True)
        body_layout.addWidget(self.history_view, stretch=1)

        self.input = QPlainTextEdit(body)
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText(
            self.tr("Ask about your mods... (Enter to send, Shift+Enter for newline)")
        )
        self.input.setMinimumHeight(52)
        self.input.setMaximumHeight(140)
        self.input.installEventFilter(self)
        body_layout.addWidget(self.input)

        buttons = QHBoxLayout()
        self.clear_button = QPushButton(self.tr("Clear"), body)
        self.stop_button = QPushButton(self.tr("Stop"), body)
        self.send_button = QPushButton(self.tr("Send"), body)
        self.stop_button.setEnabled(False)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.send_button)
        body_layout.addLayout(buttons)

        self.splitter.addWidget(body)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([150, 600])
        layout.addWidget(self.splitter, stretch=1)

        self.history_button.toggled.connect(self.history_list.setVisible)
        self.new_button.clicked.connect(self._new_chat)
        self.collapse_button.toggled.connect(self._set_collapsed)
        self.settings_button.clicked.connect(self._open_settings)
        self.clear_button.clicked.connect(self._clear)
        self.stop_button.clicked.connect(self._request_stop)
        self.send_button.clicked.connect(self._send)

    def focus_input(self) -> None:
        """Give keyboard focus to the message box (used by the View menu)."""
        if self._collapsed:
            self.collapse_button.setChecked(False)
        self.input.setFocus()

    def shutdown(self) -> None:
        """Persist the current chat and stop any worker before teardown."""
        self._save_session()
        worker = self._worker
        if worker is None:
            return
        worker.request_cancel()
        worker.resolve_confirmation(False)
        if not worker.wait(5000):
            logger.warning("AI chat worker still running at shutdown; waiting")
            worker.wait()

    # ------------------------------------------------------------ provider
    @staticmethod
    def _key_configured(provider: dict[str, Any]) -> bool:
        """Whether the provider is usable without asking for a key."""
        if str(provider.get("api_key", "")).strip():
            return True
        url = str(provider.get("base_url", ""))
        return url.startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))

    @Slot()
    def _rebuild_model_menu(self) -> None:
        """Fill the Add Model menu: add provider + provider/model switcher."""
        menu = self.model_menu
        menu.clear()
        self._model_submenus.clear()
        add_action = menu.addAction(self.tr("Add Provider..."))
        add_action.triggered.connect(self._add_provider)

        provider_list = self.settings.ai_providers
        if provider_list:
            menu.addSeparator()
        for provider in provider_list:
            provider_id = str(provider.get("id", ""))
            label = str(provider.get("name") or provider.get("base_url") or "?")
            submenu = menu.addMenu(label)
            # Keep a Python reference: QAction.menu() wrappers alone get
            # garbage-collected and take the C++ submenu with them.
            self._model_submenus.append(submenu)
            models = [str(m) for m in (provider.get("models") or [])]
            if not models:
                add_model_action = submenu.addAction(self.tr("Add Model..."))
                add_model_action.triggered.connect(
                    lambda checked=False, pid=provider_id: self._edit_provider(pid)
                )
            for model in models:
                action = submenu.addAction(model)
                action.setCheckable(True)
                action.setChecked(
                    provider_id == self.settings.ai_provider_id
                    and model == self.settings.ai_model
                )
                action.triggered.connect(
                    lambda checked=False, pid=provider_id, m=model: self._select_model(
                        pid, m
                    )
                )
            submenu.addSeparator()
            edit_action = submenu.addAction(self.tr("Edit Provider..."))
            edit_action.triggered.connect(
                lambda checked=False, pid=provider_id: self._edit_provider(pid)
            )

    @Slot()
    def _add_provider(self) -> None:
        provider = prompt_add_provider(self.settings, self)
        if provider is None:
            return
        self._append_activity(
            self.tr("Provider '{name}' added.").format(name=provider["name"])
        )

    @Slot()
    def _edit_provider(self, provider_id: str) -> None:
        provider = get_provider(self.settings, provider_id)
        if provider is None:
            return
        dialog = AIProviderDialog(parent=self, provider=provider)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = dialog.result_provider()
        if result is None:
            return
        update_provider(self.settings, provider_id, result)
        self.settings.save()
        self._append_activity(
            self.tr("Provider '{name}' updated.").format(name=result["name"])
        )

    @Slot()
    def _select_model(self, provider_id: str, model: str) -> None:
        set_active(self.settings, provider_id, model)
        self.settings.save()
        provider = get_provider(self.settings, provider_id)
        name = str(provider.get("name", "")) if provider else provider_id
        self._append_activity(
            self.tr("Using {model} ({provider}).").format(model=model, provider=name)
        )

    # ------------------------------------------------------------ sessions
    def _save_session(self) -> None:
        """Persist the active session when it has messages."""
        session = self._session
        if session.is_empty:
            return
        try:
            self._store.save(session)
        except OSError:
            logger.exception("AI chat: could not save session {}", session.id)
            return
        self._refresh_sidebar()

    def _update_title(self) -> None:
        self.title_label.setText(self._session.title or self.tr("New chat"))

    def _refresh_sidebar(self) -> None:
        """Rebuild the history list from the store, keeping selection."""
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for session in self._store.list_sessions():
            title = session.title or self.tr("New chat")
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, session.id)
            item.setToolTip(title)
            self.history_list.addItem(item)
            if session.id == self._session.id:
                item.setSelected(True)
        self.history_list.blockSignals(False)

    @Slot(QListWidgetItem)
    def _on_history_item_clicked(self, item: QListWidgetItem) -> None:
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(chat_id, str):
            self._switch_chat(chat_id)

    @Slot()
    def _new_chat(self) -> None:
        if self._worker is not None:
            return
        self._save_session()
        self._session = self._store.new_session()
        self.history_view.clear()
        self.status_label.clear()
        self._update_title()
        self._refresh_sidebar()
        self.input.setFocus()

    def _switch_chat(self, chat_id: str) -> None:
        if self._worker is not None or chat_id == self._session.id:
            return
        self._save_session()
        session = self._store.load(chat_id)
        if session is None:
            self._refresh_sidebar()
            return
        self._session = session
        self._render_session()
        self._update_title()
        self._refresh_sidebar()

    def _render_session(self) -> None:
        self.history_view.clear()
        for message in self._session.messages:
            if message.role == "user":
                self._append_user(message.content)
            elif message.role == "assistant":
                self._append_assistant(message.content)

    @Slot(QPoint)
    def _show_history_menu(self, pos: QPoint) -> None:
        item = self.history_list.itemAt(pos)
        if item is None:
            return
        chat_id = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(chat_id, str):
            return
        menu = QMenu(self)
        open_action = menu.addAction(self.tr("Open"))
        rename_action = menu.addAction(self.tr("Rename"))
        delete_action = menu.addAction(self.tr("Delete"))
        chosen = menu.exec(self.history_list.viewport().mapToGlobal(pos))
        if chosen is open_action:
            self._switch_chat(chat_id)
        elif chosen is rename_action:
            self._rename_chat(chat_id)
        elif chosen is delete_action:
            self._delete_chat(chat_id)

    def _rename_chat(self, chat_id: str) -> None:
        session = self._store.load(chat_id)
        if session is None:
            return
        title, ok = QInputDialog.getText(
            self,
            self.tr("Rename Chat"),
            self.tr("Title:"),
            text=session.title or self.tr("New chat"),
        )
        if not ok or not title.strip():
            return
        session.title = title.strip()
        self._store.save(session)
        if chat_id == self._session.id:
            self._session.title = session.title
            self._update_title()
        self._refresh_sidebar()

    def _delete_chat(self, chat_id: str) -> None:
        if self._worker is not None:
            return
        session = self._store.load(chat_id)
        title = session.title if session is not None else chat_id
        answer = BinaryChoiceDialog(
            title=self.tr("Delete Chat"),
            text=self.tr('Delete chat "{title}"?').format(title=title),
            information=self.tr("The saved conversation will be removed."),
            positive_text=self.tr("Delete"),
            negative_text=self.tr("Cancel"),
            parent=self,
        )
        if not answer.exec_is_positive():
            return
        self._store.delete(chat_id)
        if chat_id == self._session.id:
            self._session = self._store.new_session()
            self.history_view.clear()
            self._update_title()
        self._refresh_sidebar()

    # ------------------------------------------------------------- sending
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
        provider = get_active_provider(self.settings)
        if provider is None or not self.settings.ai_model.strip():
            self._append_error(self.tr("Add a model first: Add Model -> Add Provider."))
            self._open_settings()
            return
        self.input.clear()
        if self._session.is_empty:
            self._session.title = make_title(text)
            self._update_title()
        self._session.messages.append(ChatMessage(role="user", content=text))
        self._save_session()
        self._append_user(text)

        api_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": effective_system_prompt(self.settings.ai_system_prompt),
            }
        ]
        api_messages.extend(
            {"role": message.role, "content": message.content}
            for message in self._session.messages
        )
        client = AIClient(
            base_url=str(provider.get("base_url", "")),
            api_key=str(provider.get("api_key", "")),
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
        self._store.delete(self._session.id)
        self._session = self._store.new_session()
        self.history_view.clear()
        self._update_title()
        self._refresh_sidebar()

    @Slot()
    def _open_settings(self) -> None:
        self._show_settings_dialog("AI Assistant")

    @Slot(str)
    def _on_finished_ok(self, text: str) -> None:
        if text and text != CANCELLED_TEXT:
            self._session.messages.append(ChatMessage(role="assistant", content=text))
            self._save_session()
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
        self.new_button.setEnabled(not busy)
        self.history_list.setEnabled(not busy)
        self.model_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.status_label.setText(self.tr("Thinking...") if busy else "")

    @Slot(bool)
    def _set_collapsed(self, collapsed: bool) -> None:
        """Collapse the chat down to its header row, or expand it back."""
        self._collapsed = collapsed
        self.splitter.setVisible(not collapsed)
        self.collapse_button.setText(
            self.tr("Expand") if collapsed else self.tr("Collapse")
        )
        if not collapsed:
            self.input.setFocus()

    # --------------------------------------------------------------- style
    def _qss_block(self, selector: str) -> dict[str, str]:
        """Declarations of one selector from the app-wide stylesheet."""
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return {}
        stylesheet = app.styleSheet()
        if not stylesheet:
            return {}
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", stylesheet)
        if match is None:
            return {}
        rules: dict[str, str] = {}
        for declaration in match.group(1).split(";"):
            prop, sep, value = declaration.partition(":")
            if sep:
                rules[prop.strip().lower()] = value.strip()
        return rules

    def _chat_colors(self) -> tuple[str, str, str]:
        """Theme colors for the chat: history background/foreground/accent."""
        history = self._qss_block("QTextBrowser#ChatHistory")
        background = history.get("background-color", _FALLBACK_BG)
        foreground = history.get("color", _FALLBACK_FG)
        primary = self._qss_block("QPushButton#primaryButton")
        accent = primary.get("background-color", _FALLBACK_ACCENT)
        return background, foreground, accent

    def _assistant_color(self) -> str:
        """Assistant replies: white on dark themes, theme text on light ones."""
        background, foreground, _ = self._chat_colors()
        if _luminance(background) < _DARK_LUMINANCE:
            return "#ffffff"
        return foreground

    def _activity_color(self) -> str:
        background, foreground, _ = self._chat_colors()
        return _mix_colors(foreground, background, 0.5)

    def _error_color(self) -> str:
        background, _, _ = self._chat_colors()
        if _luminance(background) < _DARK_LUMINANCE:
            return "#ff7b72"
        return "#c01c28"

    # -------------------------------------------------------------- output
    def _user_background(self) -> str:
        """Accent-tinted bubble background for the user's own messages."""
        background, _, accent = self._chat_colors()
        return _mix_colors(background, accent, 0.45)

    def _assistant_background(self) -> str:
        """Subtle panel background for assistant replies."""
        background, foreground, _ = self._chat_colors()
        return _mix_colors(background, foreground, 0.12)

    def _error_background(self) -> str:
        background, _, _ = self._chat_colors()
        return _mix_colors(background, self._error_color(), 0.18)

    def _prepare_message_block(self) -> QTextCursor:
        """Return a cursor in a fresh block ready for the next message.

        A blank spacer block separates consecutive messages, and list
        formatting is always cleared so a message never inherits a list
        from the previous one.
        """
        cursor = self.history_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        document = self.history_view.document()
        has_content = document.blockCount() > 1 or document.firstBlock().length() > 1
        if has_content:
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
            cursor.insertBlock()
            cursor.setBlockFormat(QTextBlockFormat())
        else:
            cursor.setBlockFormat(QTextBlockFormat())
        return cursor

    def _bubble_indent(self) -> int:
        """Inset that keeps each bubble inside ~70% of the view width."""
        width = self.history_view.viewport().width()
        return max(120, min(400, int(width * 0.3)))

    def _style_message_blocks(
        self,
        start: int,
        end: int,
        background: str,
        alignment: Qt.AlignmentFlag | None = None,
        left_margin: int = 6,
        right_margin: int = 6,
    ) -> None:
        """Paint ``background`` across the non-empty blocks in [start, end].

        Empty placeholder blocks (e.g. when a reply starts with a list)
        stay unstyled so they read as natural spacing, not as stubs.
        """
        document = self.history_view.document()
        block = document.findBlock(start)
        while block.isValid() and block.position() <= end:
            if block.length() > 1:
                fmt = QTextBlockFormat()
                fmt.setBackground(QColor(background))
                fmt.setLeftMargin(left_margin)
                fmt.setRightMargin(right_margin)
                fmt.setTopMargin(0)
                fmt.setBottomMargin(0)
                if alignment is not None:
                    fmt.setAlignment(alignment)
                block_cursor = QTextCursor(document)
                block_cursor.setPosition(block.position())
                block_cursor.mergeBlockFormat(fmt)
            block = block.next()

    def _append_user(self, text: str) -> None:
        if not text.strip():
            return
        cursor = self._prepare_message_block()
        start = cursor.position()
        cursor.insertHtml(f"<b>{escape(text).replace(chr(10), '<br>')}</b>")
        end = cursor.position()
        self._style_message_blocks(
            start,
            end,
            self._user_background(),
            Qt.AlignmentFlag.AlignRight,
            left_margin=self._bubble_indent(),
        )
        self.history_view.ensureCursorVisible()

    def _append_assistant(self, text: str) -> None:
        """Append an assistant reply rendered from Markdown in the reply color."""
        if not text.strip():
            return
        cursor = self._prepare_message_block()
        start = cursor.position()
        cursor.insertMarkdown(text)
        end = cursor.position()
        self._style_message_blocks(
            start,
            end,
            self._assistant_background(),
            Qt.AlignmentFlag.AlignLeft,
            right_margin=self._bubble_indent(),
        )
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._assistant_color()))
        style_cursor = QTextCursor(self.history_view.document())
        style_cursor.setPosition(start)
        style_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        style_cursor.mergeCharFormat(fmt)
        self.history_view.ensureCursorVisible()

    def _append_activity(self, text: str) -> None:
        if not text.strip():
            return
        cursor = self._prepare_message_block()
        cursor.insertHtml(
            f'<span style="color:{self._activity_color()}; font-family:monospace;'
            f' font-size:11px;">{escape(text)}</span>'
        )
        self.history_view.ensureCursorVisible()

    def _append_error(self, text: str) -> None:
        if not text.strip():
            return
        cursor = self._prepare_message_block()
        start = cursor.position()
        cursor.insertHtml(escape(text).replace(chr(10), "<br>"))
        end = cursor.position()
        self._style_message_blocks(
            start,
            end,
            self._error_background(),
            Qt.AlignmentFlag.AlignLeft,
        )
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._error_color()))
        style_cursor = QTextCursor(self.history_view.document())
        style_cursor.setPosition(start)
        style_cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        style_cursor.mergeCharFormat(fmt)
        self.history_view.ensureCursorVisible()
