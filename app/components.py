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

    def __init__(self, parent=None, favorites=None, sort_criteria="name", sort_reverse=False):
        super().__init__(parent)
        self.sort_date_action = None
        self.sort_name_action = None
        self.sort_size_action = None
        self.sort_reverse_action = None
        self.favorites = favorites or []
        self.initial_sort_criteria = sort_criteria
        self.initial_sort_reverse = sort_reverse
        self.setup_ui()

    def update_favorites(self, favorites_list):
        """Allows updating the favorites menu dynamically if settings change."""
        self.favorites = favorites_list
        # Rebuild or update favorites menu items here if needed
        if hasattr(self, 'favorites_menu'):
            self.favorites_menu.clear()
            add_fav_action = QAction("⭐ Add to Favorites", self)
            add_fav_action.triggered.connect(self.add_favorite_requested.emit)
            self.favorites_menu.addAction(add_fav_action)
            self.favorites_menu.addSeparator()
            self._populate_favorites_actions()

    def _populate_favorites_actions(self):
        """Helper method to populate favorite folder actions."""
        if self.favorites:
            for path in self.favorites:
                if path and os.path.isdir(path):
                    folder_name = os.path.basename(os.path.normpath(path))
                    fav_action = QAction(folder_name, self)
                    # Bind path securely using default argument in lambda
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

        # --- File Menu ---
        file_menu = self.addMenu("File")
        open_action = QAction("Open...", self)
        open_action.triggered.connect(self.open_folder_requested.emit)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.exit_requested.emit)

        file_menu.addAction(open_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

        # --- Favorites Menu ---
        self.favorites_menu = self.addMenu("Favorites")
        # Add "Add to Favorites" command at the top
        add_fav_action = QAction("⭐ Add to Favorites", self)
        add_fav_action.triggered.connect(self.add_favorite_requested.emit)
        self.favorites_menu.addAction(add_fav_action)
        self.favorites_menu.addSeparator()

        self._populate_favorites_actions()

        # --- Sort Menu ---
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

        # --- Select Menu ---
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

        # --- Tools Menu ---
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

        # --- About Menu ---
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

        # Tab Widget container
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

        # 1. Inspector Tab
        inspector_tab = QWidget()
        inspector_layout = QVBoxLayout(inspector_tab)
        inspector_layout.setContentsMargins(10, 10, 10, 10)
        self.inspector_label = QLabel("Inspector Properties:\n\n• Folder: None Selected\n• Images Loaded: 0")
        self.inspector_label.setWordWrap(True)
        self.inspector_label.setStyleSheet("color: #cbd5e1; font-size: 12px; background: transparent;")
        inspector_layout.addWidget(self.inspector_label)
        inspector_layout.addStretch()
        self.tab_widget.addTab(inspector_tab, "Inspector")

        # 2. EXIF Tab
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

        # 3. Activity/Log Tab
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
    folder_selected = Signal(str)  # Emits selected directory path
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
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(8)

        title_label = QLabel("DIRECTORIES")
        title_label.setStyleSheet(
            "color: #64748b; font-family: sans-serif; font-size: 11px; font-weight: bold; background: transparent; padding-left: 4px;"
        )
        layout.addWidget(title_label)

        # File system model setup for directory browsing (using QDir filters)
        self.fs_model = QFileSystemModel()
        self.fs_model.setFilter(QDir.Filter.AllDirs | QDir.Filter.NoDotAndDotDot)
        self.fs_model.setRootPath("")  # Root system path

        self.tree_view = QTreeView()
        self.tree_view.setModel(self.fs_model)
        # Hide extra columns (Size, Type, Date Modified) so only folder names show cleanly
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

        # Enable drag-and-drop onto tree folders
        self.tree_view.setAcceptDrops(True)
        self.tree_view.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.tree_view.setDropIndicatorShown(True)
        self.tree_view.viewport().setAcceptDrops(True)
        self.tree_view.dragEnterEvent = self._tree_drag_enter
        self.tree_view.dragMoveEvent = self._tree_drag_move
        self.tree_view.dropEvent = self._tree_drop

        self.tree_view.clicked.connect(self.on_dir_clicked)

        layout.addWidget(self.tree_view)

        # Quick action button at the bottom
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
        """Expands parent tree hierarchy and selects/scrolls the target folder into view."""
        if folder_path and os.path.isdir(folder_path):
            index = self.fs_model.index(folder_path)
            if index.isValid():
                # Expand all parent indexes up to root so deeply nested folders are visible
                parent = index.parent()
                while parent.isValid():
                    self.tree_view.expand(parent)
                    parent = parent.parent()

                self.tree_view.expand(index)
                self.tree_view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                self.tree_view.setCurrentIndex(index)

    def _tree_drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.DropAction.MoveAction)
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
                event.setDropAction(Qt.DropAction.MoveAction)
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
            event.setDropAction(Qt.DropAction.MoveAction)
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