"""Turn-based chat widgets modeled on the VS Code chat layout.

The conversation is a vertical stack of turn widgets inside a scroll
area: user turns are right-aligned tinted bubbles, assistant turns are
left-aligned stacks of tool-call rows plus one markdown bubble. Every
message owns its own ``QTextDocument``, so rendering, streaming and
scrolling never share or mutate a single rich-text document.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPaintEvent,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)


class MessageBubble(QFrame):
    """One chat message rendered as a rounded tinted bubble.

    User messages render as bold plain text; assistant and error
    messages render Markdown through the bubble's own document.
    Right-clicking a bubble offers Copy, and — when the panel enables
    them — Edit & Resend (last user message) and Regenerate (last
    assistant reply).
    """

    edit_requested = Signal()
    regenerate_requested = Signal()

    def __init__(
        self,
        background: str,
        text_color: str,
        role: str = "assistant",
        markdown: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MessageBubble")
        self.setProperty("role", role)
        self._text_color = text_color
        self._background = QColor(background)
        self._editable = False
        self._regenerable = False
        self._document = QTextDocument(self)
        self._document.setDocumentMargin(2)

        self._label = QLabel(self)
        self._label.setObjectName("MessageBubbleText")
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._label.setOpenExternalLinks(True)
        self._label.setWordWrap(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.addWidget(self._label)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the rounded tinted background without a stylesheet.

        A stylesheet on the bubble frame breaks QLabel's word wrap
        (Qt's heightForWidth stops working through styled widgets), so
        the background is painted directly instead.
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._background)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)
        super().paintEvent(event)

    # ------------------------------------------------------------- content
    def set_plain(self, text: str) -> None:
        """Render ``text`` as bold text (used for user and error messages).

        Goes through the bubble's document like Markdown does: setting a
        stylesheet on the label would break QLabel's word wrap (Qt quirk).
        """
        self._document.setHtml(f"<b>{escape(text).replace(chr(10), '<br>')}</b>")
        self._recolor()
        self._label.setText(self._document.toHtml())

    def set_markdown(self, text: str) -> None:
        """Render ``text`` as Markdown in the bubble's text color."""
        self._document.setMarkdown(text)
        self._recolor()
        self._label.setText(self._document.toHtml())

    def _recolor(self) -> None:
        """Force the whole document into the bubble's text color."""
        cursor = QTextCursor(self._document)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._text_color))
        cursor.mergeCharFormat(fmt)

    def plain_text(self) -> str:
        """The bubble's text without any markup."""
        return self._document.toPlainText()

    def background_color(self) -> str:
        """The bubble's tinted background color as a hex string."""
        return self._background.name()

    def rendered_html(self) -> str:
        """The rendered HTML of the bubble (empty for plain-text bubbles)."""
        return self._document.toHtml()

    # ------------------------------------------------------------- actions
    def set_editable(self, editable: bool) -> None:
        """Whether the context menu offers Edit & Resend."""
        self._editable = editable

    def set_regenerable(self, regenerable: bool) -> None:
        """Whether the context menu offers Regenerate."""
        self._regenerable = regenerable

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction(self.tr("Copy"))
        edit_action = (
            menu.addAction(self.tr("Edit & Resend")) if self._editable else None
        )
        regenerate_action = (
            menu.addAction(self.tr("Regenerate")) if self._regenerable else None
        )
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is copy_action:
            QApplication.clipboard().setText(self.plain_text())
        elif edit_action is not None and chosen is edit_action:
            self.edit_requested.emit()
        elif regenerate_action is not None and chosen is regenerate_action:
            self.regenerate_requested.emit()


class ToolCallRow(QFrame):
    """One agent activity line: a small monospace status row.

    The status is inferred from the text prefix the agent emits:
    ``…`` while running, ``→`` when done, ``✗`` when declined.
    """

    def __init__(self, text: str, muted_color: str, error_color: str) -> None:
        super().__init__()
        self.setObjectName("ChatToolRow")
        self._muted_color = muted_color
        self._error_color = error_color
        self._label = QLabel(self)
        self._label.setObjectName("ChatToolRowText")
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 0, 4, 0)
        self._layout.addWidget(self._label)
        self.set_status_text(text)

    def set_status_text(self, text: str) -> None:
        """Update the row's label and recolor it from the status prefix."""
        self._label.setText(text)
        if text.lstrip().startswith("✗"):
            color = self._error_color
        else:
            color = self._muted_color
        self._label.setStyleSheet(
            f"color: {color}; font-family: monospace; font-size: 11px;"
        )

    def plain_text(self) -> str:
        """The row's text."""
        return self._label.text()


def _tool_key(text: str) -> str:
    """Strip the status prefix/suffix off an activity line for matching."""
    body = text.strip()
    for prefix in ("… ", "→ ", "✗ "):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break
    return body.removesuffix(" — declined")


class AssistantTurn(QWidget):
    """One assistant turn: tool-call rows plus a streaming markdown bubble."""

    regenerate_requested = Signal()

    def __init__(
        self,
        background: str,
        text_color: str,
        activity_color: str,
        error_color: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AssistantTurn")
        self._text_color = text_color
        self._activity_color = activity_color
        self._error_color = error_color
        self._bubble: MessageBubble | None = None
        self._stream_parts: list[str] = []
        self._final_text = ""
        self._tool_rows: dict[str, ToolCallRow] = {}
        self._background = background
        self._regenerable = False
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)

    # ---------------------------------------------------------------- tools
    def add_tool(self, text: str) -> None:
        """Add or update a tool-call row for an activity line."""
        key = _tool_key(text)
        row = self._tool_rows.get(key)
        if row is None:
            row = ToolCallRow(text, self._activity_color, self._error_color)
            self._tool_rows[key] = row
            self._layout.addWidget(row)
        else:
            row.set_status_text(text)

    # --------------------------------------------------------------- bubble
    def _ensure_bubble(self) -> MessageBubble:
        if self._bubble is None:
            self._bubble = MessageBubble(
                self._background,
                self._text_color,
                role="assistant",
                markdown=True,
                parent=self,
            )
            self._bubble.set_regenerable(self._regenerable)
            self._bubble.regenerate_requested.connect(self.regenerate_requested.emit)
            self._layout.addWidget(self._bubble)
        return self._bubble

    def append_stream(self, delta: str) -> None:
        """Accumulate one streamed delta and re-render the bubble."""
        if not delta:
            return
        self._stream_parts.append(delta)
        self._ensure_bubble().set_markdown("".join(self._stream_parts))

    def finalize(self, text: str | None = None) -> None:
        """Settle the turn's text, re-rendering the bubble if needed."""
        if text is not None and text.strip():
            self._final_text = text
            self._ensure_bubble().set_markdown(text)
        elif self._stream_parts:
            self._final_text = "".join(self._stream_parts)

    def set_note(self, text: str) -> None:
        """Append a muted note row (e.g. "Generation stopped.")."""
        if text.strip():
            self.add_tool(text)

    def markdown_text(self) -> str:
        """The turn's settled text (final text, or the streamed partial)."""
        return self._final_text or "".join(self._stream_parts)

    def plain_text(self) -> str:
        """All text of the turn: tool rows plus the bubble text."""
        chunks = [row.plain_text() for row in self._tool_rows.values()]
        if self._bubble is not None:
            chunks.append(self._bubble.plain_text())
        return "\n".join(chunks)

    def is_empty(self) -> bool:
        """Whether the turn has neither tool rows nor bubble text."""
        return not self._tool_rows and not self.markdown_text().strip()

    # -------------------------------------------------------------- actions
    def set_regenerable(self, regenerable: bool) -> None:
        """Whether the bubble's context menu offers Regenerate."""
        self._regenerable = regenerable
        if self._bubble is not None:
            self._bubble.set_regenerable(regenerable)

    def bubble(self) -> MessageBubble | None:
        """The turn's markdown bubble, if it has been created."""
        return self._bubble
