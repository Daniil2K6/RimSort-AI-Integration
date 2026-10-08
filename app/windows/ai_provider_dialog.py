"""Dialog for adding or editing an OpenAI-compatible AI provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ai.providers import (
    PROVIDER_PRESETS,
    ProviderError,
    add_provider,
    fetch_models,
    new_provider,
    set_active,
)
from app.views.dialogue import InformationBox

if TYPE_CHECKING:
    from app.models.settings import Settings

_API_KEY_PLACEHOLDER = "Optional for local endpoints"


class AIProviderDialog(QDialog):
    """Add/edit a provider: name, base URL, API key, model list."""

    def __init__(
        self,
        parent: QWidget | None = None,
        provider: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._existing: dict[str, Any] | None = provider
        self._result: dict[str, Any] | None = None

        editing = provider is not None
        self.setWindowTitle(
            self.tr("Edit Provider") if editing else self.tr("Add Provider")
        )
        self.setModal(True)
        self.setMinimumWidth(480)
        self._build_ui(show_presets=not editing)
        if provider is not None:
            self._fill(provider)

    # ------------------------------------------------------------------ UI
    def _build_ui(self, show_presets: bool) -> None:
        layout = QVBoxLayout(self)

        grid = QGridLayout()
        layout.addLayout(grid)

        self.preset_combo: QComboBox | None = None
        row = 0
        if show_presets:
            grid.addWidget(
                QLabel(self.tr("Preset:")),
                row,
                0,
                alignment=Qt.AlignmentFlag.AlignRight,
            )
            combo = QComboBox()
            combo.addItem(self.tr("— Select preset —"))
            for preset in PROVIDER_PRESETS:
                combo.addItem(preset.label, userData=preset)
            combo.currentIndexChanged.connect(self._on_preset_chosen)
            grid.addWidget(combo, row, 1)
            self.preset_combo = combo
            row += 1

        grid.addWidget(
            QLabel(self.tr("Name:")), row, 0, alignment=Qt.AlignmentFlag.AlignRight
        )
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("OpenAI")
        grid.addWidget(self.name_edit, row, 1)
        row += 1

        grid.addWidget(
            QLabel(self.tr("Base URL:")), row, 0, alignment=Qt.AlignmentFlag.AlignRight
        )
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://api.openai.com/v1")
        grid.addWidget(self.url_edit, row, 1)
        row += 1

        grid.addWidget(
            QLabel(self.tr("API key:")), row, 0, alignment=Qt.AlignmentFlag.AlignRight
        )
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(_API_KEY_PLACEHOLDER)
        grid.addWidget(self.key_edit, row, 1)
        row += 1

        hint = QLabel(
            self.tr(
                "Any OpenAI-compatible endpoint works, including local "
                "servers (Ollama, LM Studio, llama.cpp)."
            )
        )
        hint.setWordWrap(True)
        grid.addWidget(hint, row, 0, 1, 2)
        row += 1

        grid.addWidget(QLabel(self.tr("Models:")), row, 0, Qt.AlignmentFlag.AlignTop)
        models_box = QVBoxLayout()
        self.models_edit = QPlainTextEdit()
        self.models_edit.setPlaceholderText(
            self.tr("One model id per line.\nLeave empty and press Fetch to load them.")
        )
        self.models_edit.setMinimumHeight(90)
        models_box.addWidget(self.models_edit)

        fetch_row = QHBoxLayout()
        self.fetch_button = QPushButton(self.tr("Fetch Models"))
        self.fetch_button.clicked.connect(self._on_fetch_models)
        self.fetch_status = QLabel()
        self.fetch_status.setWordWrap(True)
        fetch_row.addWidget(self.fetch_button)
        fetch_row.addWidget(self.fetch_status, stretch=1)
        models_box.addLayout(fetch_row)

        models_wrap = QWidget()
        models_wrap.setLayout(models_box)
        grid.addWidget(models_wrap, row, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self._validate_and_accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def _fill(self, provider: dict[str, Any]) -> None:
        self.name_edit.setText(str(provider.get("name", "")))
        self.url_edit.setText(str(provider.get("base_url", "")))
        self.key_edit.setText(str(provider.get("api_key", "")))
        models = provider.get("models") or []
        self.models_edit.setPlainText("\n".join(str(m) for m in models))

    # ------------------------------------------------------------- actions
    def _on_preset_chosen(self, index: int) -> None:
        if self.preset_combo is None:
            return
        preset = self.preset_combo.itemData(index)
        if preset is None:
            return
        self.name_edit.setText(preset.label)
        self.url_edit.setText(preset.base_url)
        if preset.needs_key:
            self.key_edit.setPlaceholderText(self.tr("API key"))
        else:
            self.key_edit.setPlaceholderText(_API_KEY_PLACEHOLDER)
        if preset.default_models:
            self.models_edit.setPlainText("\n".join(preset.default_models))

    def _on_fetch_models(self) -> None:
        url = self.url_edit.text().strip()
        if not url.startswith(("http://", "https://")):
            self.fetch_status.setText(self.tr("Enter a valid base URL first."))
            return
        self.fetch_button.setEnabled(False)
        self.fetch_status.setText(self.tr("Fetching…"))
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QApplication.processEvents()
        try:
            models = fetch_models(url, self.key_edit.text().strip())
        except ProviderError as exc:
            self.fetch_status.setText(str(exc))
        else:
            self.models_edit.setPlainText("\n".join(models))
            self.fetch_status.setText(
                self.tr("Found {n} models.").format(n=len(models))
            )
        finally:
            QApplication.restoreOverrideCursor()
            self.fetch_button.setEnabled(True)

    def _validate_and_accept(self) -> None:
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip().rstrip("/")
        if not name:
            InformationBox(
                title=self.tr("Add Provider"),
                text=self.tr("Enter a provider name."),
                parent=self,
            ).exec()
            return
        if not url.startswith(("http://", "https://")):
            InformationBox(
                title=self.tr("Add Provider"),
                text=self.tr("Base URL must start with http:// or https://."),
                parent=self,
            ).exec()
            return
        models = [
            line.strip()
            for line in self.models_edit.toPlainText().splitlines()
            if line.strip()
        ]
        if self._existing is not None:
            result = {
                **self._existing,
                "name": name,
                "base_url": url,
                "api_key": self.key_edit.text().strip(),
                "models": models,
            }
        else:
            result = new_provider(
                name,
                url,
                self.key_edit.text().strip(),
                models,
            )
        self._result = result
        self.accept()

    def result_provider(self) -> dict[str, Any] | None:
        """The provider dict built on OK, or ``None`` when cancelled."""
        return self._result


def prompt_add_provider(
    settings: Settings, parent: QWidget | None = None
) -> dict[str, Any] | None:
    """Run the add-provider dialog; on OK store and activate the provider.

    Returns the stored provider, or ``None`` when cancelled/incomplete.
    """
    dialog = AIProviderDialog(parent=parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    provider = dialog.result_provider()
    if provider is None:
        return None
    add_provider(settings, provider)
    models = [str(m) for m in (provider.get("models") or [])]
    set_active(
        settings,
        str(provider.get("id", "")),
        models[0] if models else "",
    )
    settings.save()
    return provider
