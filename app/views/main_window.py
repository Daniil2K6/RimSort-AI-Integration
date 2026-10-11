import os
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.views.settings_dialog import SettingsDialog

from loguru import logger
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.ai.gui_bridge import INVOKE_ALLOWLIST, GuiActionBridge, SurfaceId
from app.controllers.file_search_controller import FileSearchController
from app.controllers.instance_controller import InstanceController
from app.controllers.main_content_controller import MainContentController
from app.controllers.menu_bar_controller import MenuBarController
from app.controllers.metadata_controller import MetadataController
from app.controllers.mods_panel_controller import ModsPanelController
from app.controllers.todds_controller import ToddsController
from app.controllers.troubleshooting_controller import TroubleshootingController
from app.models.instance import Instance
from app.models.settings import Settings
from app.utils import globals
from app.utils.acf_utils import refresh_acf_metadata
from app.utils.app_info import AppInfo
from app.utils.event_bus import EventBus
from app.utils.gui_info import GUIInfo
from app.utils.steam.steamcmd.wrapper import SteamcmdInterface
from app.utils.watchdog import WatchdogHandler
from app.utils.window_launch_state import apply_window_launch_state
from app.views.acf_log_reader import AcfLogReader
from app.views.chat_panel import ChatPanel
from app.views.dialogue import BinaryChoiceDialog
from app.views.file_search_dialog import FileSearchDialog
from app.views.main_content_panel import MainContent
from app.views.menu_bar import MenuBar
from app.views.player_log_tab import PlayerLogTab
from app.views.status_panel import Status
from app.views.troubleshooting_dialog import TroubleshootingDialog


class MainWindow(QMainWindow):
    """
    Subclass QMainWindow to customize the main application window.
    """

    def __init__(
        self,
        settings: Settings,
        get_active_instance: Callable[[], Instance],
        set_instance: Callable[[Instance], None],
        show_settings_dialog: Callable[..., None],
        metadata_controller: MetadataController,
        settings_dialog: "SettingsDialog | None" = None,
        debug_mode: bool = False,
    ) -> None:
        """
        Initialize the main application window. Construct the layout,
        add the three main views, and set up relevant signals and slots.
        """
        logger.info("Initializing MainWindow")
        super().__init__()

        self.settings = settings
        self._get_active_instance = get_active_instance
        self._set_instance = set_instance
        self._show_settings_dialog = show_settings_dialog
        self.metadata_controller = metadata_controller

        # Set global references
        globals.MAIN_WINDOW = self

        # Create the main application window
        self.DEBUG_MODE = debug_mode
        # SteamCMDInterface
        self.steamcmd_wrapper = SteamcmdInterface.instance()
        # Content initialization should only fire on startup. Otherwise, this is handled by Refresh button

        # Watchdog
        self.watchdog_event_handler: WatchdogHandler | None = None
        # Set up the window
        current_instance = self.settings.current_instance
        self.__set_window_title(current_instance)

        # Create the window layout
        app_layout = QVBoxLayout()
        app_layout.setContentsMargins(0, 0, 0, 0)  # Space from main layout to border
        app_layout.setSpacing(0)  # Space between widgets

        # Create a tab widget
        self.tab_widget = QTabWidget()
        app_layout.addWidget(self.tab_widget)

        # Create various panels on the application GUI
        self.main_content_panel: MainContent = MainContent(
            settings=self.settings,
            show_settings_dialog=self._show_settings_dialog,
            settings_dialog=settings_dialog,
            metadata_controller=self.metadata_controller,
        )
        self.main_content_panel.disable_enable_widgets_signal.connect(
            self.__disable_enable_widgets
        )
        self.main_content_panel.stop_watchdog_signal.connect(self.shutdown_watchdog)

        self.bottom_panel = Status()

        # Create and add the Main Content panel tab
        self.main_content_tab = QWidget()
        self.main_content_layout = QVBoxLayout()
        self.main_content_tab.setLayout(self.main_content_layout)

        # Add the MainContent panel to the tab
        self.main_content_layout.addWidget(self.main_content_panel.main_layout_frame)

        # Create button layout and add it to the main content layout
        button_layout = QHBoxLayout()
        self.main_content_layout.addLayout(button_layout)

        self.game_version_label = QLabel()
        self.game_version_label.setFont(GUIInfo().smaller_font)
        self.game_version_label.setEnabled(False)
        button_layout.addWidget(self.game_version_label)

        button_layout.addStretch()

        # Define button attributes
        self.refresh_button = QPushButton(self.tr("Refresh"))
        self.clear_button = QPushButton(self.tr("Clear"))
        self.restore_button = QPushButton(self.tr("Restore"))
        self.sort_button = QPushButton(self.tr("Sort"))
        self.save_button = QPushButton(self.tr("Save"))
        self.run_button = QPushButton(self.tr("Run"))
        self.ai_chat_button = QPushButton(self.tr("AI Chat"))
        self.ai_chat_button.setCheckable(True)
        self.ai_chat_button.setToolTip(self.tr("Toggle the AI assistant chat"))

        buttons = [
            self.refresh_button,
            self.clear_button,
            self.restore_button,
            self.sort_button,
            self.save_button,
            self.run_button,
            self.ai_chat_button,
        ]

        for button in buttons:
            button.setMinimumWidth(100)
            button_layout.addWidget(button)

        self.tab_widget.addTab(self.main_content_tab, self.tr("Main Content"))

        # Create and add the ACF Data tab
        self.acf_log_reader_tab = QWidget()
        self.acf_log_reader_layout = QVBoxLayout()
        self.acf_log_reader_tab.setLayout(self.acf_log_reader_layout)

        # Instantiate the AcfDataWindow and add it to the tab
        self.acf_log_reader = AcfLogReader(
            metadata_controller=self.metadata_controller,
            active_mods_list=self.main_content_panel.mods_panel.active_mods_list,
        )
        self.acf_log_reader_layout.addWidget(self.acf_log_reader)

        self.tab_widget.addTab(self.acf_log_reader_tab, self.tr("ACF Log Reader"))

        # Create and add the Player Log tab
        self.player_log_widget = PlayerLogTab(self.settings)
        self.tab_widget.addTab(self.player_log_widget, self.tr("Player Log"))

        # Create and add the Search tab
        self.file_search_tab = QWidget()
        self.file_search_layout = QVBoxLayout()
        self.file_search_tab.setLayout(self.file_search_layout)

        # Instantiate the SearchWindow and add it to the tab
        self.file_search_dialog = FileSearchDialog()
        self.file_search_controller = FileSearchController(
            settings=self.settings,
            dialog=self.file_search_dialog,
            metadata_controller=self.metadata_controller,
        )
        self.file_search_layout.addWidget(self.file_search_dialog)

        self.tab_widget.addTab(self.file_search_tab, self.tr("File Search"))

        # Create and add the Troubleshooting tab
        self.troubleshooting_tab = QWidget()
        self.troubleshooting_layout = QVBoxLayout()
        self.troubleshooting_tab.setLayout(self.troubleshooting_layout)

        # Instantiate the TroubleshootingDialog and add it to the tab
        self.troubleshooting_dialog = TroubleshootingDialog()
        self.troubleshooting_controller = TroubleshootingController(
            settings=self.settings,
            dialog=self.troubleshooting_dialog,
        )
        self.troubleshooting_layout.addWidget(self.troubleshooting_dialog)
        self.tab_widget.addTab(self.troubleshooting_tab, self.tr("Troubleshooting"))

        # Save button flashing animation
        self.save_button_flashing_animation = QTimer()
        self.save_button_flashing_animation.timeout.connect(
            partial(EventBus().do_button_animation.emit, self.save_button)
        )

        # Create the bottom panel
        app_layout.addWidget(self.bottom_panel.frame)

        # Display all items
        widget = QWidget()
        widget.setLayout(app_layout)
        self.setCentralWidget(widget)

        # Built-in AI assistant chat (collapsible dock on the right)
        self.ai_chat_dock = QDockWidget(self.tr("AI Assistant"), self)
        self.ai_chat_dock.setObjectName("AIAssistantDock")
        self.gui_bridge = self._build_gui_bridge()
        self.chat_panel = ChatPanel(
            settings=self.settings,
            show_settings_dialog=self._show_settings_dialog,
            gui_bridge=self.gui_bridge,
        )
        self.ai_chat_dock.setWidget(self.chat_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.ai_chat_dock)
        if not self.settings.ai_chat_visible:
            self.ai_chat_dock.hide()
        self.ai_chat_dock.visibilityChanged.connect(self._on_ai_chat_visibility_changed)
        self.ai_chat_button.setChecked(not self.ai_chat_dock.isHidden())
        self.ai_chat_button.clicked.connect(self._on_ai_chat_button_clicked)
        self.ai_chat_dock.visibilityChanged.connect(self.ai_chat_button.setChecked)
        EventBus().do_show_ai_chat.connect(self._show_ai_chat)

        self.mods_panel_controller = ModsPanelController(
            view=self.main_content_panel.mods_panel,
            settings=self.settings,
        )

        self.menu_bar = MenuBar(menu_bar=self.menuBar(), settings=self.settings)
        self.menu_bar_controller = MenuBarController(
            view=self.menu_bar,
            settings=self.settings,
            show_settings_dialog=self._show_settings_dialog,
        )

        self.main_content_controller = MainContentController(
            view=self.main_content_panel,
            settings=self.settings,
            metadata_controller=self.metadata_controller,
        )

        self.todds_controller = ToddsController(
            settings=self.settings,
            metadata_controller=self.main_content_panel.metadata_controller,
        )
        self.main_content_panel.todds_controller = self.todds_controller

        # Connect Instances Menu Bar signals
        EventBus().do_activate_current_instance.connect(self.__switch_to_instance)

        # launch the main window
        self._launch_main_window()
        logger.debug("Finished MainWindow initialization")

    def _launch_main_window(self) -> None:
        """Apply main window launch state from settings"""
        main_window_launch_state = self.settings.main_window_launch_state
        custom_width = self.settings.main_window_custom_width
        custom_height = self.settings.main_window_custom_height

        apply_window_launch_state(
            self, main_window_launch_state, custom_width, custom_height
        )
        logger.info(
            f"Main window started with launch state: {main_window_launch_state}"
        )

    def __disable_enable_widgets(self, enable: bool) -> None:
        # Disable widgets
        q_app = QApplication.instance()
        if not isinstance(q_app, QApplication):
            return
        for widget in q_app.allWidgets():
            widget.setEnabled(enable)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Clean up child windows and resources before closing.

        :param event: The close event to handle.
        """
        # Abort any in-progress metadata scanning so background threads stop
        self.metadata_controller.is_abort_requested = True

        # Break out of the nested QEventLoop in do_threaded_loading_animation
        # so the startup sequence can unwind and the process can exit
        self.main_content_panel.abort_loading()

        # Stop filesystem watchdog if running
        self.shutdown_watchdog()

        # Stop any in-flight AI chat worker
        self.chat_panel.shutdown()

        # Close all child windows
        self.main_content_panel.close_child_windows()

        event.accept()

    @Slot()
    def _show_ai_chat(self) -> None:
        """Reveal the AI assistant dock and focus its input box."""
        self.ai_chat_dock.show()
        self.ai_chat_dock.raise_()
        self.chat_panel.focus_input()

    # ---- GUI action bridge (Stage C) ------------------------------------
    def _build_gui_bridge(self) -> GuiActionBridge:
        """Create the main-thread bridge that lets the agent drive the UI."""
        bridge = GuiActionBridge(self)

        bridge.register_executor("get_state", self._gui_get_state)
        bridge.register_executor("navigate", self._gui_navigate)
        bridge.register_executor("select_mod", self._gui_select_mod)
        bridge.register_executor("set_filter", self._gui_set_filter)
        bridge.register_executor("open_workshop_item", self._gui_open_workshop_item)
        bridge.register_executor("invoke_action", self._gui_invoke_action)
        bridge.register_executor("get_modlist", self._gui_get_modlist)
        bridge.register_executor("apply_modlist", self._gui_apply_modlist)

        bridge.set_capabilities(
            targets=[
                SurfaceId.MODS_TAB.value,
                SurfaceId.PLAYER_LOG_TAB.value,
                SurfaceId.ACF_LOG_TAB.value,
                SurfaceId.FILE_SEARCH_TAB.value,
                SurfaceId.TROUBLESHOOTING_TAB.value,
            ],
            actions=sorted(INVOKE_ALLOWLIST),
        )
        return bridge

    def _gui_get_modlist(self, params: dict[str, Any]) -> dict[str, Any]:
        panel = self.main_content_panel
        data = panel._import_export_service.collect_active_mods(
            panel.mods_panel.active_mods_list.paths, panel.duplicate_mods
        )
        instance = self.settings.instances[self.settings.current_instance]
        return {
            "ok": True,
            "working": list(data.active_mods),
            "instance": self.settings.current_instance,
            "config_path": str(
                (Path(instance.config_folder) / "ModsConfig.xml").resolve()
            ),
        }

    def _gui_apply_modlist(self, params: dict[str, Any]) -> dict[str, Any]:
        """Compare, persist and render in one main-thread transaction."""
        state = self._gui_get_modlist({})
        if params.get("config_path") != state["config_path"]:
            return {"ok": False, "code": "gui_instance_mismatch"}
        if params.get("expected_working") != state["working"]:
            return {
                "ok": False,
                "code": "revision_conflict",
                "error": "Список в окне изменился. Получите состояние и обновите план.",
            }
        ids = params.get("package_ids")
        if not isinstance(ids, list) or not all(isinstance(pid, str) for pid in ids):
            return {"ok": False, "code": "invalid_modlist"}
        from app.mcp.context import MCPContext

        panel = self.main_content_panel
        active, inactive, duplicates, missing = (
            panel.metadata_controller.get_mods_from_list(mod_list=ids)
        )
        # Unknown entries are preserved in the disk file, never trigger a modal
        # download prompt from an unattended MCP request.
        ctx = MCPContext()
        if str(ctx.mods_config_path.resolve()) != state["config_path"]:
            return {"ok": False, "code": "gui_instance_mismatch"}
        result = ctx.write_active_config(
            ids,
            known_expansions=params.get("known_expansions"),
            backup=bool(params.get("backup", True)),
        )
        panel.duplicate_mods = duplicates
        panel.missing_mods = missing
        panel.mods_panel.reset_all_filters_and_search("Active")
        panel.mods_panel.reset_all_filters_and_search("Inactive")
        panel._insert_data_into_lists(active, inactive)
        panel.active_mods_uuids_last_save = list(active)
        return {"ok": True, **result, "gui_updated": True, "gui_missing": missing}

    def _gui_tab_widget_for(self, target: str) -> QWidget | None:
        """Map a stable SurfaceId to its real tab widget (no label matching)."""
        return {
            SurfaceId.MODS_TAB.value: self.main_content_tab,
            SurfaceId.PLAYER_LOG_TAB.value: self.player_log_widget,
            SurfaceId.ACF_LOG_TAB.value: self.acf_log_reader_tab,
            SurfaceId.FILE_SEARCH_TAB.value: self.file_search_tab,
            SurfaceId.TROUBLESHOOTING_TAB.value: self.troubleshooting_tab,
        }.get(target)

    def _gui_navigate(self, params: dict[str, Any]) -> dict[str, Any]:
        target = str(params.get("target", ""))
        widget = self._gui_tab_widget_for(target)
        if widget is None:
            return {"ok": False, "code": "gui_unknown_target", "error": target}
        index = self.tab_widget.indexOf(widget)
        if index < 0:
            return {"ok": False, "code": "gui_unknown_target", "error": target}
        self.tab_widget.setCurrentIndex(index)
        return {"target": target, "visible_tab": self.tab_widget.tabText(index)}

    def _gui_find_mod_item(self, package_id: str) -> tuple[Any, Any]:
        """Locate the list item whose UserRole packageid matches, else None."""
        for mod_list in (
            self.main_content_panel.mods_panel.active_mods_list,
            self.main_content_panel.mods_panel.inactive_mods_list,
        ):
            for row in range(mod_list.count()):
                item = mod_list.item(row)
                if item is None:
                    continue
                if (
                    self._gui_item_package_id(item) or ""
                ).lower() == package_id.lower():
                    return mod_list, item
        return None, None

    def _gui_item_package_id(self, item: Any) -> str | None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, dict):
            return data.get("packageid")
        path = getattr(data, "path", None)
        if path:
            mod = self.main_content_panel.metadata_controller.get_mod(path)
            pid = getattr(mod, "package_id", None)
            return str(pid) if pid is not None else None
        return None

    def _gui_select_mod(self, params: dict[str, Any]) -> dict[str, Any]:
        package_id = str(params.get("package_id", ""))
        reveal = bool(params.get("reveal", True))
        if reveal:
            self._gui_navigate({"target": SurfaceId.MODS_TAB.value})
        mod_list, item = self._gui_find_mod_item(package_id)
        if item is None:
            return {
                "ok": False,
                "code": "gui_mod_not_found",
                "error": package_id,
            }
        mod_list.setCurrentItem(item)
        if reveal and item.isHidden():
            list_name = (
                "Active"
                if mod_list is self.main_content_panel.mods_panel.active_mods_list
                else "Inactive"
            )
            self.main_content_panel.mods_panel.reset_all_filters_and_search(list_name)
        mod_list.scrollToItem(item)
        mod_list.setFocus(Qt.FocusReason.OtherFocusReason)
        self._gui_highlight_list(mod_list)
        return {
            "selected": package_id,
            "list": "active"
            if mod_list is self.main_content_panel.mods_panel.active_mods_list
            else "inactive",
        }

    def _gui_highlight_list(self, mod_list: Any) -> None:
        """Temporarily frame a mod list so the agent's target is visible.

        Uses a themed palette highlight border and reverts after a short
        delay — a temporary overlay, never the system cursor.
        """
        if not self.settings.ai_chat_highlight:
            return
        mod_list.setStyleSheet("QListWidget { border: 2px solid palette(highlight); }")
        QTimer.singleShot(1200, lambda: mod_list.setStyleSheet(""))

    def _gui_set_filter(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", ""))
        list_name = str(params.get("list", "inactive"))
        panel = self.main_content_panel.mods_panel
        if list_name == "active":
            search = panel.active_mods_search
            mod_list = panel.active_mods_list
        else:
            search = panel.inactive_mods_search
            mod_list = panel.inactive_mods_list
        search.setText(query)
        return {"list": list_name, "query": query, "visible": mod_list.count()}

    def _gui_open_workshop_item(self, params: dict[str, Any]) -> dict[str, Any]:
        url = str(params.get("url", ""))
        self.main_content_panel._do_browse_workshop_url(url)
        return {"opened": url}

    def _gui_invoke_action(self, params: dict[str, Any]) -> dict[str, Any]:
        action_id = str(params.get("action_id", ""))
        if action_id == "open_settings":
            self._show_settings_dialog()
        elif action_id == "refresh_mods":
            EventBus().do_refresh_mods_lists.emit()
        elif action_id == "clear_chat":
            self.chat_panel.clear_button.click()
        elif action_id == "toggle_ai_chat":
            self.ai_chat_dock.setVisible(self.ai_chat_dock.isHidden())
        return {"acted": action_id}

    def _gui_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        panel = self.main_content_panel.mods_panel
        active = panel.active_mods_list
        inactive = panel.inactive_mods_list
        current = self.tab_widget.currentIndex()
        selected_item = active.currentItem() or inactive.currentItem()
        selected_pkg = None
        if selected_item is not None:
            selected_pkg = self._gui_item_package_id(selected_item)
        return {
            "visible_tab": self.tab_widget.tabText(current),
            "active_count": active.count(),
            "inactive_count": inactive.count(),
            "active_filter": panel.active_mods_search.text(),
            "inactive_filter": panel.inactive_mods_search.text(),
            "selected_package_id": selected_pkg,
        }

    @Slot(bool)
    def _on_ai_chat_button_clicked(self, checked: bool) -> None:
        """Toggle the AI chat dock from the main-window button row."""
        if checked:
            self._show_ai_chat()
        else:
            self.ai_chat_dock.hide()

    @Slot(bool)
    def _on_ai_chat_visibility_changed(self, visible: bool) -> None:
        """Persist dock visibility so it survives a restart.

        Hiding or minimizing the whole window also flips the dock's
        visibility flag, but that is not a user toggle — ignore it,
        otherwise the chat would stay closed on the next launch.
        """
        if not self.isVisible() or self.isMinimized():
            return
        if self.settings.ai_chat_visible == visible:
            return
        self.settings.ai_chat_visible = visible
        self.settings.save()

    def showEvent(self, event: QShowEvent) -> None:
        # Call the original showEvent handler
        super().showEvent(event)

    def initialize_content(self, is_initial: bool = True) -> None:
        # Set all items as outdated in aux DB
        EventBus().do_set_all_entries_in_aux_db_as_outdated.emit()
        # POPULATE INSTANCES SUBMENU
        self.menu_bar_controller._on_instances_submenu_population(
            instance_names=list(self.settings.instances.keys())
        )
        self.menu_bar_controller._on_set_current_instance(
            self.settings.current_instance
        )
        # REFRESH CONFIGURED METADATA
        self.main_content_panel._do_refresh(is_initial=is_initial)
        # If the window was closed during scanning, skip remaining initialization
        if not self.isVisible():
            return
        # CHECK FOR STEAMCMD SETUP
        if not os.path.exists(
            self.steamcmd_wrapper.steamcmd_prefix
        ) or not self.steamcmd_wrapper.check_for_steamcmd(
            prefix=self.steamcmd_wrapper.steamcmd_prefix
        ):
            if not self._get_active_instance().steamcmd_ignore:
                self.steamcmd_wrapper.on_steamcmd_not_found(
                    ask_ignore=True,
                    settings=self.settings,
                    active_instance=self._get_active_instance(),
                )
        else:
            self.steamcmd_wrapper.setup = True

        # UPDATE DATABASES ON STARTUP IF ENABLED
        # This is called here after all controllers are initialized and signals are connected
        if is_initial:
            self.main_content_controller._update_databases_on_startup_if_enabled_silent()

        # CHECK USER PREFERENCE FOR WATCHDOG
        if self.settings.watchdog_toggle:
            # Setup watchdog
            self.initialize_watchdog()

        self.__check_steam_integration()

        # Force initial setup to False and save settings
        if self._get_active_instance().initial_setup:
            self._get_active_instance().initial_setup = False
            self.settings.save()
        # IF CHECK FOR UPDATE ON STARTUP...
        if self.settings.check_for_update_startup:
            EventBus().do_check_for_application_update.emit()
        # Delete outdated entries in aux DB
        EventBus().do_delete_outdated_entries_in_aux_db.emit()

    def __check_steam_integration(self) -> None:
        """Ask the user if they would like to enable Steam Client Integration for the active instance if it is the first time they are setting up RimSort."""
        instance = self._get_active_instance()

        if instance.initial_setup and not instance.steam_client_integration:
            diag = BinaryChoiceDialog(
                title=self.tr("Steam Client Integration"),
                text=self.tr(
                    "<h3>Would you like to enable Steam Client Integration for this instance?</h3>"
                ),
                information=self.tr("""This will allow you to use RimSort features that require the Steam Client. This includes, among other things, unsubscribing from workshop mods and opening workshop links via the Steam Client. 
                <br><br>
                You can change this in the settings under the Advanced tab."""),
                negative_text="No",
            )
            if diag.exec_is_positive():
                instance.steam_client_integration = True
                self._set_instance(instance)

    def __switch_to_instance(self, instance: str) -> None:
        """Switch to a different instance."""
        self.shutdown_watchdog()
        # Set current instance
        self.settings.current_instance = instance
        instance_path = str(InstanceController.get_instance_folder_path(instance))
        self.settings.current_instance_path = instance_path
        # Update window title with current instance
        self.__set_window_title(instance)
        # Save settings
        self.settings.save()
        # Clear mod lists
        self.main_content_panel._insert_data_into_lists([], [])
        # Initialize content
        self.initialize_content(is_initial=False)

    def __set_window_title(self, instance: str) -> None:
        """
        Sets the window title with the name of the instance being used.

        :param instance: Name of the instance currently being used.
        """
        title = f"RimSort {AppInfo().app_version} | {instance} Instance"
        if AppInfo().is_dev_mode:
            title += " [DEV]"
        self.setWindowTitle(title)

    def initialize_watchdog(self) -> None:
        logger.info("Initializing watchdog FS Observer")
        self.watchdog_event_handler = WatchdogHandler(
            instance=self._get_active_instance(),
        )
        self.watchdog_event_handler.acf_changed.connect(
            partial(refresh_acf_metadata, self.main_content_panel.metadata_controller)
        )
        self.watchdog_event_handler.mod_created.connect(
            self.main_content_panel.metadata_controller.process_creation
        )
        self.watchdog_event_handler.mod_deleted.connect(
            self.main_content_panel.metadata_controller.process_deletion
        )
        self.watchdog_event_handler.mod_updated.connect(
            self.main_content_panel.metadata_controller.process_update
        )
        self.watchdog_event_handler.start()

    def shutdown_watchdog(self) -> None:
        if self.watchdog_event_handler is not None:
            self.watchdog_event_handler.stop()
            self.watchdog_event_handler = None
