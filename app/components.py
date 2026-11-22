import os
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QTabWidget, QWidget, QLabel, QTextEdit,
    QPushButton, QTreeView, QFileSystemModel, QMenuBar, QAbstractItemView
)
from PySide6.QtCore import Qt, Signal, QModelIndex, QDir
from PySide6.QtGui import QAction


class TopMenuBar(QMenuBar):
    """Encapsulated top menu bar emitting signals for File, Sort, Select, Favorites, and About actions."""
    open_folder_requested = Signal()
    exit_requested = Signal()
    sort_criteria_changed = Signal(str)
    sort_reverse_toggled = Signal(bool)
    select_all_requested = Signal()
    deselect_requested = Signal()
    inverse_selection_requested = Signal()
    about_requested = Signal()
    favorite_selected = Signal(str)  # Emits selected folder path from favorites
    add_favorite_requested = Signal()  # Emits signal to add current folder to favorites
    # --- Tool Signals ---
    convert_requested = Signal()
    purge_exif_requested = Signal()
    inject_exif_requested = Signal()
    export_exif_requested = Signal()
    export_comfy_workflow_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent=None, favorites=None, sort_criteria="name", sort_reverse=False, max_favorites=15):
        super().__init__(parent)
        self.sort_date_action = None
        self.sort_name_action = None
        self.sort_size_action = None
        self.sort_reverse_action = None
        self.favorites = favorites or []
        self.max_favorites = max(1, int(max_favorites)) if max_favorites is not None else 15
        self.initial_sort_criteria = sort_criteria
        self.initial_sort_reverse = sort_reverse
        self.setup_ui()

    def update_favorites(self, favorites_list, max_favorites=None):
        """Allows updating the favorites menu dynamically if settings change."""
        self.favorites = favorites_list or []
        if max_favorites is not None:
            try:
                self.max_favorites = max(1, int(max_favorites))
            except (TypeError, ValueError):
                pass
        if hasattr(self, 'favorites_menu'):
            self.favorites_menu.clear()
            add_fav_action = QAction("⭐ Add to Favorites", self)
            add_fav_action.triggered.connect(self.add_favorite_requested.emit)
            self.favorites_menu.addAction(add_fav_action)
            self.favorites_menu.addSeparator()
            self._populate_favorites_actions()

    def _populate_favorites_actions(self):
        """Helper method to populate favorite folder actions up to max_favorites."""
        valid_paths = [p for p in (self.favorites or []) if p and os.path.isdir(p)][:self.max_favorites]
        if valid_paths:
            for path in valid_paths:
                folder_name = os.path.basename(os.path.normpath(path))
                fav_action = QAction(folder_name, self)
                fav_action.triggered.connect(lambda checked=False, p=path: self.favorite_selected.emit(p))
                self.favorites_menu.addAction(fav_action)
        else:
            empty_action = QAction("(No favorites configured)", self)
            empty_action.setEnabled(False)
            self.favorites_menu.addAction(empty_action)

    def setup_ui(self):
        self.setStyleSheet("""
            QMenuBar {
                background-color: #131b2e; color: #cbd5e1; font-family: sans-serif;
                font-size: 13px; border: 1px solid #1e293b; border-radius: 6px; padding: 4px;
            }
            QMenuBar::item { background: transparent; padding: 6px 12px; border-radius: 4px; }
            QMenuBar::item:selected { background-color: #1e293b; color: #ffffff; }
            QMenu {
                background-color: #0d1321; color: #cbd5e1; border: 1px solid #1e293b;
                border-radius: 6px; padding: 4px;
            }
            QMenu::item { padding: 6px 24px; border-radius: 4px; }
            QMenu::item:selected { background-color: #1e293b; color: #ffffff; }
        """)

        file_menu = self.addMenu("File")
        open_action = QAction("Open...", self)
        open_action.triggered.connect(self.open_folder_requested.emit)
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.exit_requested.emit)
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        self.favorites_menu = self.addMenu("Favorites")
        add_fav_action = QAction("⭐ Add to Favorites", self)
        add_fav_action.triggered.connect(self.add_favorite_requested.emit)
        self.favorites_menu.addAction(add_fav_action)
        self.favorites_menu.addSeparator()
        self._populate_favorites_actions()

        sort_menu = self.addMenu("Sort")
        self.sort_name_action = QAction("By Name", self, checkable=True)
        self.sort_name_action.setChecked(self.initial_sort_criteria == "name")
        self.sort_name_action.triggered.connect(lambda: self._on_sort_selected("name"))

        self.sort_date_action = QAction("By Date Modified", self, checkable=True)
        self.sort_date_action.setChecked(self.initial_sort_criteria == "date")
        self.sort_date_action.triggered.connect(lambda: self._on_sort_selected("date"))

        self.sort_size_action = QAction("By File Size", self, checkable=True)
        self.sort_size_action.setChecked(self.initial_sort_criteria == "size")
        self.sort_size_action.triggered.connect(lambda: self._on_sort_selected("size"))

        sort_menu.addAction(self.sort_name_action)
        sort_menu.addAction(self.sort_date_action)
        sort_menu.addAction(self.sort_size_action)
        sort_menu.addSeparator()

        self.sort_reverse_action = QAction("Reverse Order", self, checkable=True)
        self.sort_reverse_action.setChecked(self.initial_sort_reverse)
        self.sort_reverse_action.triggered.connect(lambda checked: self.sort_reverse_toggled.emit(checked))
        sort_menu.addAction(self.sort_reverse_action)

        select_menu = self.addMenu("Select")
        select_all_action = QAction("Select All", self)
        select_all_action.setShortcut("Ctrl+A")
        select_all_action.triggered.connect(self.select_all_requested.emit)

        deselect_action = QAction("Deselect", self)
        deselect_action.setShortcut("Ctrl+D")
        deselect_action.triggered.connect(self.deselect_requested.emit)

        inverse_action = QAction("Inverse", self)
        inverse_action.setShortcut("Ctrl+I")
        inverse_action.triggered.connect(self.inverse_selection_requested.emit)

        select_menu.addAction(select_all_action)
        select_menu.addAction(deselect_action)
        select_menu.addAction(inverse_action)

        tools_menu = self.addMenu("Tools")
        convert_action = QAction("Convert...", self)
        convert_action.triggered.connect(self.convert_requested.emit)
        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.settings_requested.emit)
        purge_action = QAction("Purge EXIF data", self)
        purge_action.triggered.connect(self.purge_exif_requested.emit)
        inject_action = QAction("Inject EXIF data...", self)
        inject_action.triggered.connect(self.inject_exif_requested.emit)
        export_action = QAction("Export EXIF data...", self)
        export_action.triggered.connect(self.export_exif_requested.emit)
        export_comfy_action = QAction("Export ComfyUI Workflows...", self)
        export_comfy_action.triggered.connect(self.export_comfy_workflow_requested.emit)

        tools_menu.addAction(convert_action)
        tools_menu.addSeparator()
        tools_menu.addAction(purge_action)
        tools_menu.addAction(inject_action)
        tools_menu.addAction(export_action)
        tools_menu.addAction(export_comfy_action)
        tools_menu.addSeparator()
        tools_menu.addAction(settings_action)

        about_action = QAction("About", self)
        about_action.triggered.connect(self.about_requested.emit)
        self.addAction(about_action)

    def _on_sort_selected(self, criteria: str):
        self.sort_name_action.setChecked(criteria == "name")
        self.sort_date_action.setChecked(criteria == "date")
        self.sort_size_action.setChecked(criteria == "size")
        self.sort_criteria_changed.emit(criteria)


class SidebarPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(300)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tab_widget = QTabWidget(self)
        self.tab_widget.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #1e293b;
                background-color: #0b0f19;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #131b2e;
                color: #94a3b8;
                border: 1px solid #1e293b;
                padding: 6px 12px;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 2px;
                font-size: 12px;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background-color: #1e293b;
                color: #f8fafc;
                border-bottom-color: #1e293b;
            }
        """)

        inspector_tab = QWidget()
        inspector_layout = QVBoxLayout(inspector_tab)
        inspector_layout.setContentsMargins(10, 10, 10, 10)
        self.inspector_label = QLabel("Inspector Properties:\n\n• Folder: None Selected\n• Images Loaded: 0")
        self.inspector_label.setWordWrap(True)
        self.inspector_label.setStyleSheet("color: #cbd5e1; font-size: 12px; background: transparent;")
        inspector_layout.addWidget(self.inspector_label)
        inspector_layout.addStretch()
        self.tab_widget.addTab(inspector_tab, "Inspector")

        exif_tab = QWidget()
        exif_layout = QVBoxLayout(exif_tab)
        exif_layout.setContentsMargins(6, 6, 6, 6)

        self.exif_view = QTextEdit()
        self.exif_view.setReadOnly(True)
        self.exif_view.setHtml("<i>No EXIF metadata available.<br>Select an image to inspect camera details.</i>")
        self.exif_view.setStyleSheet("""
            QTextEdit {
                background-color: #05070a;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 6px;
                font-size: 11px;
            }
        """)
        exif_layout.addWidget(self.exif_view)
        self.tab_widget.addTab(exif_tab, "EXIF")

        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.setContentsMargins(10, 10, 10, 10)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setPlainText("• Application initialized successfully.\n• Ready to load folder.")
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #05070a;
                color: #94a3b8;
                border: 1px solid #1e293b;
                border-radius: 6px;
                font-family: monospace;
                font-size: 10px;
            }
        """)
        log_layout.addWidget(self.log_text)
        self.tab_widget.addTab(log_tab, "Activity")

        layout.addWidget(self.tab_widget)


class DirectoryBrowserPanel(QFrame):
    """Interactive directory browsing panel"""
    folder_selected = Signal(str)
    files_dropped_on_folder = Signal(list, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(230)
        self.setMaximumWidth(500)
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet("""
            QFrame {
                background-color: #0b0f19;
                border: 1px solid #1e293b;
                border-radius: 8px;
            }
            QMenu {
                background-color: #0d1321;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
                color: #e2e8f0;
                background-color: transparent;
            }
            QMenu::item:selected {
                background-color: #3b82f6;
                color: #ffffff;
            }
            QMenu::item:disabled {
                color: #64748b;
            }
            QMenu::separator {
                height: 1px;
                background: #1e293b;
                margin: 4px 0px;
            }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(8)

        title_label = QLabel("DIRECTORIES")
        title_label.setStyleSheet(
            "color: #64748b; font-family: sans-serif; font-size: 11px; font-weight: bold; background: transparent; padding-left: 4px;"
        )
        layout.addWidget(title_label)

        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot)
        self.fs_model.setRootPath("")

        self.tree_view = QTreeView()
        self.tree_view.setModel(self.fs_model)
        self.tree_view.setColumnHidden(1, True)
        self.tree_view.setColumnHidden(2, True)
        self.tree_view.setColumnHidden(3, True)
        self.tree_view.setHeaderHidden(True)
        self.tree_view.setStyleSheet("""
                    QTreeView {
                        background-color: #05070a;
                        color: #cbd5e1;
                        border: 1px solid #1e293b;
                        border-radius: 6px;
                        font-size: 11px;
                    }
                    QTreeView::item {
                        padding: 2px;
                    }
                    QTreeView::item:hover {
                        background-color: #1e293b;
                        color: #f8fafc;
                    }
                    QTreeView::item:selected {
                        background-color: #3b82f6;
                        color: #ffffff;
                    }
                """)

        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.tree_view.setDropIndicatorShown(True)
        self.tree_view.viewport().setAcceptDrops(True)
        self.tree_view.dragEnterEvent = self._tree_drag_enter
        self.tree_view.dragMoveEvent = self._tree_drag_move
        self.tree_view.dropEvent = self._tree_drop

        self.tree_view.clicked.connect(self.on_dir_clicked)
        layout.addWidget(self.tree_view)

        self.btn_browse = QPushButton("📂 Open in Explorer")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_browse.setStyleSheet("""
                    QPushButton {
                        background-color: #3b82f6; color: white; border: none;
                        border-radius: 6px; padding: 8px 12px; font-weight: bold; font-size: 11px;
                    }
                    QPushButton:hover { background-color: #2563eb; }
                """)
        layout.addWidget(self.btn_browse)

    def on_dir_clicked(self, index: QModelIndex):
        dir_path = self.fs_model.filePath(index)
        if os.path.isdir(dir_path):
            self.folder_selected.emit(dir_path)

    def set_selected_folder(self, folder_path):
        if folder_path and os.path.isdir(folder_path):
            self.selected_folder_path = os.path.abspath(folder_path)
            index = self.fs_model.index(folder_path)
            if index.isValid():
                parent = index.parent()
                while parent.isValid():
                    self.tree_view.expand(parent)
                    parent = parent.parent()

                self.tree_view.expand(index)
                self.tree_view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                self.tree_view.setCurrentIndex(index)

    def scroll_to_selected_folder(self, folder_path=None, only_if_out_of_view=False, center_in_view=False):
        target_path = folder_path or getattr(self, "selected_folder_path", None)
        if not target_path or not os.path.isdir(target_path):
            current_idx = self.tree_view.currentIndex()
            if current_idx.isValid():
                target_path = self.fs_model.filePath(current_idx)
        if not target_path or not os.path.isdir(target_path):
            return False

        index = self.fs_model.index(os.path.abspath(target_path))
        if not index.isValid():
            return False

        parent = index.parent()
        while parent.isValid():
            self.tree_view.expand(parent)
            parent = parent.parent()

        self.tree_view.setCurrentIndex(index)

        if only_if_out_of_view and not center_in_view:
            item_rect = self.tree_view.visualRect(index)
            vp_rect = self.tree_view.viewport().rect()
            if not item_rect.isEmpty() and vp_rect.contains(item_rect, proper=True):
                return False

        hint = (
            QAbstractItemView.ScrollHint.PositionAtCenter
            if center_in_view
            else QAbstractItemView.ScrollHint.EnsureVisible
        )
        self.tree_view.scrollTo(index, hint)
        return True

    def _desired_drop_action(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return Qt.DropAction.CopyAction
        return Qt.DropAction.MoveAction

    def _tree_drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(self._desired_drop_action(event))
            event.accept()
        else:
            event.ignore()

    def _tree_drag_move(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        pos = event.position().toPoint()
        index = self.tree_view.indexAt(pos)
        if index.isValid():
            target_path = self.fs_model.filePath(index)
            if os.path.isdir(target_path):
                self.tree_view.setCurrentIndex(index)
                event.setDropAction(self._desired_drop_action(event))
                event.accept()
                return
        event.ignore()

    def _tree_drop(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        pos = event.position().toPoint()
        index = self.tree_view.indexAt(pos)
        if not index.isValid():
            event.ignore()
            return

        target_folder = self.fs_model.filePath(index)
        if not os.path.isdir(target_folder):
            event.ignore()
            return

        src_paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and os.path.isfile(url.toLocalFile())
        ]
        if src_paths:
            action = self._desired_drop_action(event)
            event.setDropAction(action)
            event.accept()
            self.files_dropped_on_folder.emit(src_paths, target_folder)
        else:
            event.ignore()


class LeftToolboxPanel(QFrame):
    """Left navigation panel containing action buttons and app shortcuts."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(150)
        self.setup_ui()

    def setup_ui(self):
        self.setStyleSheet("""
            QFrame {
                background-color: #0b0f19;
                border: 1px solid #1e293b;
                border-radius: 8px;
            }
        """)
        sidebar_layout = QVBoxLayout(self)
        sidebar_layout.setContentsMargins(8, 12, 8, 12)
        sidebar_layout.setSpacing(8)

        sidebar_title = QLabel("TOOLS")
        sidebar_title.setStyleSheet(
            "color: #64748b; font-family: sans-serif; font-size: 11px; font-weight: bold; background: transparent; padding-left: 4px;"
        )
        sidebar_layout.addWidget(sidebar_title)

        tool_btn_style = """
            QPushButton {
                background-color: #131b2e; color: #cbd5e1; border: 1px solid #1e293b;
                border-radius: 6px; padding: 8px 12px; font-weight: 600; text-align: left;
            }
            QPushButton:hover { background-color: #1e293b; color: #f8fafc; border-color: #334155; }
            QPushButton:pressed { background-color: #0d1321; }
        """

        self.btn_dashboard = QPushButton("📊 Dashboard")
        self.btn_dashboard.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dashboard.setStyleSheet(tool_btn_style)
        sidebar_layout.addWidget(self.btn_dashboard)

        self.btn_analytics = QPushButton("📈 Analytics")
        self.btn_analytics.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_analytics.setStyleSheet(tool_btn_style)
        sidebar_layout.addWidget(self.btn_analytics)

        self.btn_settings = QPushButton("⚙️ Settings")
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.setStyleSheet(tool_btn_style)
        sidebar_layout.addWidget(self.btn_settings)

        sidebar_layout.addStretch()

        self.btn_child = QPushButton("📂 Child Win")
        self.btn_child.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_child.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white; border: none;
                border-radius: 6px; padding: 8px 12px; font-weight: bold; text-align: left;
            }
            QPushButton:hover { background-color: #2563eb; }
        """)
        sidebar_layout.addWidget(self.btn_child)
