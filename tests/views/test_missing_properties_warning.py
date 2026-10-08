"""Tests for the startup warnings gated by user preferences."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.views.main_content_panel import MainContent

# Private method under test; accessed via its name-mangled form.
_check_missing_properties = getattr(  # noqa: B009
    MainContent, "_MainContent__check_and_warn_missing_mod_properties"
)


def _make_self(missing_properties_warning: bool) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(missing_properties_warning=missing_properties_warning),
        window_manager=MagicMock(),
        metadata_controller=MagicMock(),
    )


def test_disabled_by_user_preference_skips_scan() -> None:
    """When the preference is off, no scan or panel may happen."""
    self = _make_self(missing_properties_warning=False)

    _check_missing_properties(self)

    self.window_manager.get_missing_packageid_paths.assert_not_called()
    self.window_manager.get_missing_publishfieldid_paths.assert_not_called()


def test_enabled_but_nothing_missing_shows_no_panel() -> None:
    """With the preference on and clean metadata the panel stays closed."""
    self = _make_self(missing_properties_warning=True)
    self.window_manager.get_missing_packageid_paths.return_value = []
    self.window_manager.get_missing_publishfieldid_paths.return_value = []

    with patch("app.views.main_content_panel.MissingModPropertiesPanel") as panel_cls:
        _check_missing_properties(self)

    panel_cls.assert_not_called()


def test_enabled_with_missing_properties_shows_panel() -> None:
    """With the preference on and broken mods the panel is displayed."""
    self = _make_self(missing_properties_warning=True)
    self.window_manager.get_missing_packageid_paths.return_value = ["/mods/broken"]
    self.window_manager.get_missing_publishfieldid_paths.return_value = []

    with patch("app.views.main_content_panel.MissingModPropertiesPanel") as panel_cls:
        _check_missing_properties(self)

    panel_cls.assert_called_once()
    self.window_manager.register.assert_called_once_with(panel_cls.return_value)
    panel_cls.return_value.show.assert_called_once()
