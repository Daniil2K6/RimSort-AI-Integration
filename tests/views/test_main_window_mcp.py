"""MCP executors use actual list-item metadata and guard GUI saves."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.views.conftest import make_stub_main_window


def test_real_item_metadata_resolves_package_id(qapp: object) -> None:
    window = make_stub_main_window()
    item = MagicMock()
    item.data.return_value = SimpleNamespace(path="/mods/example")
    window.main_content_panel.metadata_controller.get_mod.return_value = (
        SimpleNamespace(package_id="author.example")
    )
    assert window._gui_item_package_id(item) == "author.example"


def test_gui_save_rejects_stale_snapshot_before_disk_or_ui_changes(
    qapp: object, tmp_path: Path
) -> None:
    window = make_stub_main_window()
    window._gui_get_modlist = MagicMock(
        return_value={
            "config_path": str(tmp_path / "ModsConfig.xml"),
            "working": ["user.changed"],
        }
    )
    result = window._gui_apply_modlist(
        {
            "config_path": str(tmp_path / "ModsConfig.xml"),
            "expected_working": ["previous"],
            "package_ids": ["target"],
        }
    )
    assert result["code"] == "revision_conflict"
    window.main_content_panel._insert_data_into_lists.assert_not_called()
    assert not (tmp_path / "ModsConfig.xml").exists()
