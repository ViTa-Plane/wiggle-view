# version 0.7.3

import os
import shutil
import tempfile
import send2trash
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QGraphicsDropShadowEffect, QFileDialog, QScrollArea, QSplitter, QInputDialog, QDialog, QMenu,
    QMessageBox
)
from PySide6.QtCore import Qt, QThreadPool, QTimer, QRunnable, Signal, QObject, QFileSystemWatcher, QMimeData, QUrl
from PySide6.QtGui import QColor, QImageReader, QGuiApplication

from .img_viewer import ImageViewerWindow, DetachedImageViewerWindow
from .dialogs import AboutDialog, SettingsDialog, BatchCompleteDialog, BatchConvertDialog
from .workers import ThumbnailLoaderTask, ThumbnailSignalEmitter, FolderScannerSignals, FolderScannerTask
from .widgets import WindowDragHandler, TitleBar, ResponsiveGridWidget, ResizeHandleWidget
from .components import SidebarPanel, DirectoryBrowserPanel, TopMenuBar
from .helpers import load_settings, save_settings, format_file_size, extract_exif_metadata, add_folder_to_favorites, \
    extract_image_metadata

SETTINGS_FILE = "settings.json"


class MetadataWorkerSignals(QObject):
    metadata_ready = Signal(str, str, str, str)  # file_path, filename, dims_str, exif_text


class MetadataLoaderTask(QRunnable):
    """Background worker task to load QImage dimensions and EXIF metadata off the main GUI thread."""

    def __init__(self, file_path, filename, size_str, signals):
        super().__init__()
        self.file_path = file_path
        self.filename = filename
        self.size_str = size_str
        self.signals = signals

    def run(self):
        dims_str = "Unknown dims"
        if os.path.exists(self.file_path):
            reader = QImageReader(self.file_path)
            img_size = reader.size()
            if img_size.isValid():
                dims_str = f"{img_size.width()} × {img_size.height()} px"

        exif_text = extract_exif_metadata(self.file_path)
        self.signals.metadata_ready.emit(self.file_path, self.filename, dims_str, exif_text)


class MainWindow(QMainWindow, WindowDragHandler):
    def __init__(self, initial_image_path=None):
        super().__init__(parent=None)
        WindowDragHandler.__init__(self)
        self.resize(1360, 860)
        self.setMinimumSize(750, 500)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.CARD_IMAGE_SIZE = 170
        self.GRID_CELL_WIDTH = 200
        self.GRID_CELL_HEIGHT = 220

        self.thread_pool = QThreadPool.globalInstance()

        self.thumbnail_signals = ThumbnailSignalEmitter()
        self.thumbnail_signals.thumbnail_ready.connect(self.on_thumbnail_ready)

        self.scanner_signals = FolderScannerSignals()
        self.scanner_signals.batch_scanned.connect(self.on_batch_scanned)
        self.scanner_signals.scan_complete.connect(self.on_scan_complete)

        # Background metadata loading signals
        self.meta_signals = MetadataWorkerSignals()
        self.meta_signals.metadata_ready.connect(self.on_metadata_ready)

        self.thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")
        os.makedirs(self.thumb_cache_dir, exist_ok=True)

        self.loaded_items = []
        self.pending_thumbnail_files = []
        self.scan_id = 0  # Track active scan generation
        self.batch_timer = QTimer(self)
        self.batch_timer.setInterval(25)
        self.batch_timer.timeout.connect(self.process_next_thumbnail_batch)

        # Sorting state tracking
        self.current_sort_criteria = "name"
        self.sort_reverse = False

        # Initialize the File System Watcher
        self.file_watcher = QFileSystemWatcher(self)
        self.file_watcher.directoryChanged.connect(self._on_directory_changed)
        self.current_watched_dir = None

        self._setup_ui()

        # Handle direct argument launch vs normal last-folder boot
        if initial_image_path and os.path.exists(initial_image_path):
            abs_path = os.path.abspath(initial_image_path)

            if os.path.isdir(abs_path):
                # Case 1: Argument is a folder path -> open the folder directly in gallery
                folder_dir = abs_path
                self.initial_target_image = None
            else:
                # Case 2: Argument is an image file -> open its parent folder and select/view the image
                folder_dir = os.path.dirname(abs_path)
                self.initial_target_image = abs_path

            self.left_browser.set_selected_folder(folder_dir)
            self.path_label.setText(f"{folder_dir}")
            self.save_last_folder(folder_dir)

            self.scan_id += 1
            scanner_task = FolderScannerTask(folder_dir, self.scan_id, self.scanner_signals)
            self.thread_pool.start(scanner_task)
        else:
            self.load_last_folder()

    def _setup_ui(self):
        central_widget = QWidget(self)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.container = QFrame(central_widget)
        self.container.setStyleSheet("""
            QFrame {
                background-color: #0d1321;
                border: 1px solid #1e293b;
                border-radius: 12px;
            }
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(16, 1, 16, 16)
        container_layout.setSpacing(10)

        container_layout.addWidget(TitleBar("WiggleView", self, show_min_max=True))

        self.setStyleSheet("""
            QScrollBar:vertical {
                background: #0b0f19; width: 12px; margin: 0px; border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #1e293b; min-height: 30px; border-radius: 6px;
            }
            QScrollBar::handle:vertical:hover { background: #334155; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; background: none; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

            QScrollBar:horizontal {
                background: #0b0f19; height: 12px; margin: 0px; border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: #1e293b; min-width: 30px; border-radius: 6px;
            }
            QScrollBar::handle:horizontal:hover { background: #334155; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; background: none; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
        """)

        # --- Load favorites from settings ---
        settings_data = load_settings(SETTINGS_FILE)
        favorites_list = settings_data.get("favorites", [])
        self.current_sort_criteria = settings_data.get("sort_criteria", "name")
        self.sort_reverse = settings_data.get("sort_reverse", False)

        # --- Top Menu Panel ---
        self.menu_bar = TopMenuBar(
            self,
            favorites=favorites_list,
            sort_criteria=self.current_sort_criteria,
            sort_reverse=self.sort_reverse
        )

        self.menu_bar.open_folder_requested.connect(self.open_folder_dialog)
        self.menu_bar.exit_requested.connect(self.close)
        self.menu_bar.sort_criteria_changed.connect(self.set_sort_criteria)
        self.menu_bar.sort_reverse_toggled.connect(self.toggle_sort_reverse)
        self.menu_bar.select_all_requested.connect(self.select_all_items)
        self.menu_bar.deselect_requested.connect(self.deselect_all_items)
        self.menu_bar.inverse_selection_requested.connect(self.inverse_selection)
        self.menu_bar.about_requested.connect(self.show_about_dialog)
        self.menu_bar.favorite_selected.connect(self.jump_to_favorite_folder)
        self.menu_bar.favorite_selected.connect(self.jump_to_favorite_folder)
        self.menu_bar.add_favorite_requested.connect(self.add_current_folder_to_favorites)
        self.menu_bar.convert_requested.connect(self.batch_convert_images)
        self.menu_bar.purge_exif_requested.connect(self.batch_purge_exif)
        self.menu_bar.inject_exif_requested.connect(self.batch_inject_exif)
        self.menu_bar.export_exif_requested.connect(self.batch_export_exif)

        container_layout.addWidget(self.menu_bar)

        # --- Body Layout with Splitter ---
        body_split_layout = QHBoxLayout()
        body_split_layout.setSpacing(12)

        # Create a horizontal splitter for the main workspace area
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setStyleSheet("""
            QSplitter::handle {
                background-color: #1e293b;
                width: 4px;
                border-radius: 2px;
            }
            QSplitter::handle:hover {
                background-color: #3b82f6;
            }
        """)

        # --- Left Directory Browser Panel ---
        self.left_browser = DirectoryBrowserPanel(self)
        self.left_browser.folder_selected.connect(self.load_images_from_folder)
        self.left_browser.btn_browse.clicked.connect(self.open_current_folder_in_explorer)

        main_splitter.addWidget(self.left_browser)

        # Center Grid Workspace
        center_container = QWidget(self)
        center_container.setStyleSheet("background: transparent;")
        center_layout = QVBoxLayout(center_container)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)

        # Replace instruction_label with path_label
        self.path_label = QLabel("...")
        self.path_label.setStyleSheet(
            "color: #f8fafc; font-family: sans-serif; font-size: 13px; background: transparent;"
        )
        center_layout.addWidget(self.path_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.verticalScrollBar().setSingleStep(15)
        self.scroll_area.setStyleSheet("""
            QScrollArea {
                background-color: #0b0f19; border: 1px solid #1e293b; border-radius: 8px;
            }
        """)

        self.workspace_grid = ResponsiveGridWidget(
            cell_width=self.GRID_CELL_WIDTH,
            cell_height=self.GRID_CELL_HEIGHT,
            image_size=self.CARD_IMAGE_SIZE
        )

        # Configure Grid Context Menu & Focus Policy
        self.workspace_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.workspace_grid.customContextMenuRequested.connect(self.show_grid_context_menu)
        self.workspace_grid.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.workspace_grid.item_selected.connect(self.on_card_selected)
        self.workspace_grid.selection_changed.connect(self.on_selection_changed)
        self.workspace_grid.item_double_clicked.connect(self.open_image_viewer)
        self.scroll_area.setWidget(self.workspace_grid)

        center_layout.addWidget(self.scroll_area, stretch=1)

        # Add center container to splitter
        main_splitter.addWidget(center_container)

        # Set initial sizes for the splitter panels (e.g., 220px width for directories, remainder for grid)
        main_splitter.setSizes([220, 900])

        body_split_layout.addWidget(main_splitter, stretch=1)

        # --- Right Panel ---
        self.right_sidebar = SidebarPanel(self)
        body_split_layout.addWidget(self.right_sidebar)

        container_layout.addLayout(body_split_layout)

        # Footer Status bar
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel("Status: Ready")
        self.status_label.setStyleSheet(
            "color: #94a3b8; font-family: sans-serif; font-size: 12px; background: transparent;"
        )
        footer_layout.addWidget(self.status_label)

        footer_layout.addStretch()

        self.resize_handle = ResizeHandleWidget(self)
        footer_layout.addWidget(self.resize_handle, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        container_layout.addLayout(footer_layout)
        main_layout.addWidget(self.container)
        self.setCentralWidget(central_widget)

    def load_last_folder(self):
        data = load_settings(SETTINGS_FILE)
        folder = data.get("last_folder", "")

        # Default to Pictures directory if no valid saved folder exists
        if not folder or not os.path.isdir(folder):
            pictures_dir = os.path.join(os.path.expanduser("~"), "Pictures")
            folder = pictures_dir if os.path.isdir(pictures_dir) else os.path.expanduser("~")

        if folder and os.path.isdir(folder):
            self.left_browser.set_selected_folder(folder)
            self.load_images_from_folder(folder)

    def save_last_folder(self, folder_path):
        save_settings(SETTINGS_FILE, {"last_folder": folder_path})

    def set_status(self, message):
        self.status_label.setText(f"Status: {message}")
        if hasattr(self.right_sidebar, 'log_text'):
            current_text = self.right_sidebar.log_text.toPlainText()
            self.right_sidebar.log_text.setPlainText(f"{current_text}\n• {message}")
            self.right_sidebar.log_text.verticalScrollBar().setValue(
                self.right_sidebar.log_text.verticalScrollBar().maximum()
            )

    def open_folder_dialog(self):
        data = load_settings(SETTINGS_FILE)
        start_dir = data.get("last_folder", "")
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")

        folder_path = QFileDialog.getExistingDirectory(
            self, "Select Image Folder", start_dir, QFileDialog.Option.ShowDirsOnly
        )
        if folder_path:
            self.load_images_from_folder(folder_path)

    def load_images_from_folder(self, folder_path):
        if not folder_path or not os.path.exists(folder_path):
            return

        # Update watched directory for QFileSystemWatcher
        if self.current_watched_dir:
            self.file_watcher.removePath(self.current_watched_dir)

        self.current_watched_dir = folder_path
        self.file_watcher.addPath(folder_path)
        self.batch_timer.stop()
        self.loaded_items.clear()
        self.workspace_grid.set_cards(self.loaded_items)
        self.pending_thumbnail_files.clear()

        # Increment scan_id to invalidate any older running background folder tasks
        self.scan_id += 1
        current_scan_id = self.scan_id

        self.set_status(f"Scanning folder (threaded): {os.path.basename(folder_path)}")

        self.save_last_folder(folder_path)
        self.left_browser.set_selected_folder(folder_path)
        self.path_label.setText(f"{folder_path}")

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• Folder: {os.path.basename(folder_path)}\n• Scanning..."
        )

        scanner_task = FolderScannerTask(folder_path, current_scan_id, self.scanner_signals)
        self.thread_pool.start(scanner_task)

    def on_batch_scanned(self, scan_id, batch):
        # Ignore batches from outdated scans
        if scan_id != self.scan_id:
            return

        for item_data in batch:
            self.loaded_items.append(item_data)
            self.pending_thumbnail_files.append(item_data["file_path"])

        self.workspace_grid.set_cards(self.loaded_items)

        if not self.batch_timer.isActive() and self.pending_thumbnail_files:
            self.batch_timer.start()

    def on_scan_complete(self, scan_id, total_count):
        # Ignore completions from outdated scans
        if scan_id != self.scan_id:
            return

        if total_count == 0:
            self.set_status("No images found in folder")
            self.right_sidebar.inspector_label.setText(
                "Inspector Properties:\n\n• Folder Selected\n• Total Images: 0\n• Selected: None"
            )
        else:
            # Apply sorting here once all files are loaded into memory
            self.apply_sorting()
            self.set_status(f"Ready — Total images in gallery: {total_count}")
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Folder Selected\n• Total Images: {total_count}\n• Selected: None"
            )
            if hasattr(self, 'initial_target_image') and self.initial_target_image:
                target = self.initial_target_image
                self.initial_target_image = None
                self.open_image_viewer(target)

    def process_next_thumbnail_batch(self):
        if not self.pending_thumbnail_files:
            self.batch_timer.stop()
            total = len(self.loaded_items)
            self.set_status(f"Ready — Total images in gallery: {total}")
            return

        batch = self.pending_thumbnail_files[:15]
        self.pending_thumbnail_files = self.pending_thumbnail_files[15:]

        for file_path in batch:
            task = ThumbnailLoaderTask(file_path, self.thumb_cache_dir, self.CARD_IMAGE_SIZE, self.thumbnail_signals)
            self.thread_pool.start(task)

    def on_thumbnail_ready(self, file_path, thumb_path):
        for item in self.loaded_items:
            if item["file_path"] == file_path:
                item["thumb_path"] = thumb_path
                break
        self.workspace_grid.update()

    def on_card_selected(self, file_path, display_name):
        filename = os.path.basename(file_path)

        item_info = next((item for item in self.loaded_items if item["file_path"] == file_path), None)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        # 1. Instant GUI feedback with zero main-thread delay
        detailed_status = f"Selected: {filename}  |  Size: {size_str}"
        self.status_label.setText(f"Status: {detailed_status}")

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• File: {filename}\n• Size: {size_str}\n• Resolution: Loading (BG)...\n• Path: {file_path}"
        )
        self.right_sidebar.exif_view.setHtml("<i>Loading metadata in background thread...</i>")

        # 2. Offload heavy metadata & EXIF disk reads to background thread pool
        task = MetadataLoaderTask(file_path, filename, size_str, self.meta_signals)
        self.thread_pool.start(task)

    def on_metadata_ready(self, file_path, filename, dims_str, exif_text):
        # Update UI only if this file is still the active selection
        if not self.workspace_grid.selected_file_paths or file_path not in self.workspace_grid.selected_file_paths:
            return

        item_info = next((item for item in self.loaded_items if item["file_path"] == file_path), None)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• File: {filename}\n• Size: {size_str}\n• Resolution: {dims_str}\n• Path: {file_path}"
        )
        self.right_sidebar.exif_view.setHtml(exif_text)
        self.status_label.setText(f"Status: Selected: {filename}  |  Size: {size_str}  |  Dimensions: {dims_str}")

    def open_image_viewer(self, file_path):
        # Check if Alt key is held down during the action
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.AltModifier:
            self.set_status(f"Opening pinned viewer for: {os.path.basename(file_path)}")

            if not hasattr(self, 'detached_viewers'):
                self.detached_viewers = []

            detached_viewer = DetachedImageViewerWindow(file_path, self.loaded_items, self)
            detached_viewer.show()
            self.detached_viewers.append(detached_viewer)
            return

        # Standard behavior[cite: 1]
        self.set_status(f"Opening viewer for: {os.path.basename(file_path)}")
        self.hide()
        self.viewer_window = ImageViewerWindow(file_path, self)
        self.viewer_window.show()

    def open_current_folder_in_explorer(self):
        data = load_settings(SETTINGS_FILE)
        folder = data.get("last_folder", "")

        if folder and os.path.isdir(folder):
            import subprocess
            import platform

            try:
                if platform.system() == "Windows":
                    os.startfile(folder)
                elif platform.system() == "Darwin":
                    subprocess.run(["open", folder])
                else:
                    subprocess.run(["xdg-open", folder])
                self.set_status(f"Opened folder in Explorer: {os.path.basename(folder)}")
            except Exception as e:
                self.set_status(f"Could not open explorer: {e}")
        else:
            self.set_status("No active folder to open.")

    def _get_target_file_paths(self):
        """Returns currently selected absolute paths if any, otherwise all loaded items."""
        paths = []
        if hasattr(self.workspace_grid, 'selected_file_paths') and self.workspace_grid.selected_file_paths:
            paths = list(self.workspace_grid.selected_file_paths)
        else:
            paths = [item["file_path"] for item in self.loaded_items]

        # Filter down to absolute paths of files that physically exist
        valid_paths = [os.path.abspath(p) for p in paths if p and os.path.exists(p)]
        print(f"DEBUG: Resolved target file paths for deletion -> {valid_paths}")
        return valid_paths

    #  -----------sort menu functions--------------
    def set_sort_criteria(self, criteria):
        self.current_sort_criteria = criteria
        save_settings(SETTINGS_FILE, {"sort_criteria": criteria})
        self.apply_sorting()

    def toggle_sort_reverse(self, is_reversed):
        self.sort_reverse = is_reversed
        save_settings(SETTINGS_FILE, {"sort_reverse": is_reversed})
        self.apply_sorting()

    def apply_sorting(self):
        if not self.loaded_items:
            return

        criteria = self.current_sort_criteria
        is_rev = self.sort_reverse

        if criteria == "name":
            self.loaded_items.sort(key=lambda x: x.get("filename", ""), reverse=is_rev)
            self.set_status(f"Sorted by Name ({'Z-A' if is_rev else 'A-Z'})")
        elif criteria == "date":
            reverse_flag = not is_rev
            self.loaded_items.sort(key=lambda x: x.get("mtime", 0), reverse=reverse_flag)
            self.set_status(f"Sorted by Date ({'Oldest first' if is_rev else 'Newest first'})")
        elif criteria == "size":
            reverse_flag = not is_rev
            self.loaded_items.sort(key=lambda x: x.get("size", 0), reverse=reverse_flag)
            self.set_status(f"Sorted by Size ({'Smallest first' if is_rev else 'Largest first'})")

        self.workspace_grid.set_cards(self.loaded_items)

    #  -----------select menu functions--------------
    def select_all_items(self):
        if self.loaded_items:
            self.workspace_grid.select_all()

    def deselect_all_items(self):
        self.workspace_grid.deselect_all()

    def inverse_selection(self):
        if self.loaded_items:
            self.workspace_grid.invert_selection()

    def on_selection_changed(self, selected_paths):
        count = len(selected_paths)
        total = len(self.loaded_items)

        if count == 0:
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Folder Selected\n• Total Images: {total}\n• Selected: None"
            )
            self.right_sidebar.exif_view.setHtml(
                "<i>No EXIF metadata available.<br>Select an image to inspect camera details.</i>"
            )
            # Display total image count when selection is cleared / none selected
            self.set_status(f"Ready — Total images in gallery: {total}")
        elif count == 1:
            self.on_card_selected(selected_paths[0], os.path.basename(selected_paths[0]))
        else:
            total_bytes = sum(
                item.get("size", 0) for item in self.loaded_items if item["file_path"] in selected_paths
            )
            size_str = format_file_size(total_bytes)
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Multiple Items Selected\n• Selected: {count} of {total}\n• Combined Size: {size_str}"
            )
            self.right_sidebar.exif_view.setHtml(
                f"<i>{count} images selected.<br>Select a single image to inspect EXIF metadata.</i>"
            )
            self.set_status(f"Selected {count} of {total} items ({size_str})")

    #  -----------grid context menu and file operations--------------
    def show_grid_context_menu(self, pos):
        """Displays the right-click context menu on the image grid."""
        selected_paths = self._get_target_file_paths()

        menu = QMenu(self)
        menu.setStyleSheet("""
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
            }
            QMenu::item:selected {
                background-color: #3b82f6;
                color: white;
            }
            QMenu::separator {
                height: 1px;
                background: #1e293b;
                margin: 4px 0px;
            }
        """)

        copy_action = menu.addAction("Copy")
        cut_action = menu.addAction("Cut")
        paste_action = menu.addAction("Paste")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")

        has_selection = len(selected_paths) > 0
        copy_action.setEnabled(has_selection)
        cut_action.setEnabled(has_selection)
        delete_action.setEnabled(has_selection)

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()
        has_files_in_clipboard = mime_data.hasUrls()
        paste_action.setEnabled(has_files_in_clipboard)

        action = menu.exec(self.workspace_grid.mapToGlobal(pos))

        if action == copy_action:
            self.copy_selected_files(selected_paths, cut=False)
        elif action == cut_action:
            self.copy_selected_files(selected_paths, cut=True)
        elif action == paste_action:
            self.paste_files_from_clipboard()
        elif action == delete_action:
            self.delete_selected_files(selected_paths)



    def paste_files_from_clipboard(self):
        """Pastes files from clipboard into the currently active folder directory."""
        if not hasattr(self, 'current_watched_dir') or not self.current_watched_dir:
            self.set_status("No active folder destination for pasting.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()

        if not mime_data.hasUrls():
            self.set_status("No files found in clipboard to paste.")
            return

        success_count = 0
        for url in mime_data.urls():
            src_path = url.toLocalFile()
            if src_path and os.path.exists(src_path):
                try:
                    filename = os.path.basename(src_path)
                    dest_path = os.path.join(self.current_watched_dir, filename)

                    base, ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(self.current_watched_dir, f"{base}_copy{counter}{ext}")
                        counter += 1

                    if hasattr(self, '_cut_source_paths') and src_path in self._cut_source_paths:
                        shutil.move(src_path, dest_path)
                    else:
                        shutil.copy2(src_path, dest_path)

                    success_count += 1
                except Exception as e:
                    print(f"Failed to paste {src_path}: {e}")

        if hasattr(self, '_cut_source_paths'):
            self._cut_source_paths = []

        self.set_status(f"Pasted {success_count} file(s) into current folder.")
        self.load_images_from_folder(self.current_watched_dir)

    def copy_selected_files(self, paths, cut=False):
        """Copies or cuts selected file paths to the system clipboard. If no selection, copies the whole gallery."""
        # If no specific paths are selected, fallback to copying the entire loaded gallery
        if not paths:
            paths = [item["file_path"] for item in self.loaded_items if os.path.exists(item["file_path"])]

        if not paths:
            self.set_status("No images available to copy.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = QMimeData()
        urls = [QUrl.fromLocalFile(p) for p in paths if os.path.exists(p)]
        mime_data.setUrls(urls)

        clipboard.setMimeData(mime_data)

        if cut:
            self._cut_source_paths = paths
        else:
            self._cut_source_paths = []

        action_name = "Cut" if cut else "Copied"
        target_desc = "selected file(s)" if self.workspace_grid.selected_file_paths else "entire gallery"
        self.set_status(f"{action_name} {len(paths)} file(s) ({target_desc}) to clipboard.")

    def delete_selected_files(self, paths):
        """Safely sends selected image files to the Recycle Bin and updates the grid in-place without folder re-scans."""
        if not paths:
            print("DEBUG: delete_selected_files called with empty paths list.")
            self.status_label.setText("Status: Deletion failed - No valid images selected.")
            return

        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Are you sure you want to move {len(paths)} selected file(s) to the Recycle Bin?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted_paths = set()
        try:
            from send2trash import send2trash
            for path in paths:
                norm_path = os.path.abspath(path)
                if os.path.exists(norm_path):
                    send2trash(norm_path)
                    deleted_paths.add(norm_path)
        except ImportError:
            print("DEBUG Error: 'send2trash' module is not installed! Falling back to os.remove().")
            for path in paths:
                try:
                    norm_path = os.path.abspath(path)
                    if os.path.exists(norm_path):
                        os.remove(norm_path)
                        deleted_paths.add(norm_path)
                except Exception as e:
                    print(f"DEBUG Error: Permanent deletion failed for {path}: {e}")
        except Exception as e:
            print(f"DEBUG Error encountered during delete execution: {e}")

        if deleted_paths:
            # 1. Instantly filter out deleted items from loaded_items memory cache
            self.loaded_items = [
                item for item in self.loaded_items
                if os.path.abspath(item["file_path"]) not in deleted_paths
            ]

            # 2. Update the workspace grid cards in-place without scrolling/focus jump
            self.workspace_grid.set_cards(self.loaded_items)

            deleted_count = len(deleted_paths)
            total_remaining = len(self.loaded_items)
            self.set_status(f"Moved {deleted_count} file(s) to Recycle Bin. Total remaining: {total_remaining}")

            # Update sidebar summary label without full scan
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Folder Selected\n• Total Images: {total_remaining}\n• Selected: None"
            )
        else:
            self.set_status("Deletion failed: No files were removed.")

    def keyPressEvent(self, event):
        """Handles keyboard shortcuts like Delete/Backspace for removing selected images."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            selected_paths = self._get_target_file_paths()
            if selected_paths:
                self.delete_selected_files(selected_paths)
                event.accept()
                return
        super().keyPressEvent(event)

    #  -----------favorites  menu functions--------------
    def jump_to_favorite_folder(self, folder_path):
        if folder_path and os.path.isdir(folder_path):
            # 1. Load the images and start the thumbnail batch pipeline
            self.load_images_from_folder(folder_path)

            # 2. Sync the directory browser tree panel so it highlights the folder
            if hasattr(self, 'directory_browser') and self.directory_browser:
                self.directory_browser.set_selected_folder(folder_path)

            # 3. Re-apply the active sort criteria and order
            self.apply_sorting()

            self.set_status(f"Opened favorite: {os.path.basename(folder_path)}")
        else:
            self.set_status(f"Favorite folder no longer exists: {folder_path}")

    def add_current_folder_to_favorites(self):
        settings = load_settings(SETTINGS_FILE)
        current_folder = settings.get("last_folder", "")

        if current_folder and os.path.isdir(current_folder):
            updated_favorites = add_folder_to_favorites(current_folder, SETTINGS_FILE)

            # Dynamically update the menu bar's favorites list
            self.menu_bar.favorites = updated_favorites
            # Rebuild the menu bar actions UI to reflect the latest 6 items
            self.menu_bar.clear()
            self.menu_bar.setup_ui()

            folder_name = os.path.basename(os.path.normpath(current_folder))
            self.set_status(f"Added to favorites: {folder_name} (Limit: 6)")
        else:
            self.set_status("No active folder to add to favorites.")

    #  -----------tools menu functions--------------
    def batch_convert_images(self):
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images available for conversion.")
            return

        dialog = BatchConvertDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target_format = dialog.format_combo.currentText()
        quality = dialog.quality_slider.value()
        transfer_exif = dialog.transfer_exif_cb.isChecked()
        remove_original = dialog.remove_original_cb.isChecked()

        success_count = 0
        failed_count = 0
        target_ext = target_format.lower()
        if target_ext == "jpg":
            target_ext = "jpeg"

        _TAGS_r = {v: k for k, v in TAGS.items()}

        for path in paths:
            try:
                base, _ = os.path.splitext(path)
                new_path = f"{base}_converted.{target_ext}"

                # 1. Extract source metadata if transfer_exif is enabled
                exif_data_bytes = None
                exif_dict = {}
                if transfer_exif:
                    exif_data_bytes, exif_dict = extract_image_metadata(path)

                # Filter out JFIF noise
                filtered_exif_dict = {
                    k: v for k, v in exif_dict.items()
                    if not k.lower().startswith("jfif")
                }

                # 2. Perform conversion and save
                with Image.open(path) as img:
                    if target_ext in ["jpg", "jpeg"] and (
                            img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                        img_to_save = background
                    else:
                        img_to_save = img.convert("RGB") if target_ext in ["jpeg", "jpg"] else img

                    save_format = "JPEG" if target_ext == "jpeg" else target_format.upper()

                    # Initial save with quality if applicable
                    if target_ext in ["jpeg", "jpg", "webp"]:
                        img_to_save.save(new_path, save_format, quality=quality)
                    else:
                        img_to_save.save(new_path, save_format)

                # 3. Apply EXIF/Metadata transfer to the newly converted file if enabled
                if transfer_exif:
                    with Image.open(new_path) as target:
                        if target_ext in ["png"]:
                            from PIL import PngImagePlugin
                            meta = PngImagePlugin.PngInfo()
                            for k, v in exif_dict.items():
                                meta.add_text(str(k), str(v))
                            target.save(new_path, pnginfo=meta)
                        else:
                            # For JPEGs: Build clean EXIF object directly from our extracted dictionary (fixing the direct transfer bug)
                            try:
                                exif_obj = target.getexif()
                                if exif_obj is None:
                                    exif_obj = Image.Exif()

                                _TAGS_r = {v: k for k, v in TAGS.items()}
                                _GPSTAGS_r = {v: k for k, v in GPSTAGS.items()}

                                for k, v in exif_dict.items():
                                    tag_id = _TAGS_r.get(k) or _GPSTAGS_r.get(k)
                                    if tag_id is not None:
                                        try:
                                            exif_obj[tag_id] = v
                                        except Exception:
                                            pass

                                target.save(new_path, exif=exif_obj.tobytes(),
                                            quality=quality if target_ext in ["jpeg", "jpg", "webp"] else None)
                            except Exception as save_err:
                                print(f"Direct EXIF transfer fallback save error: {save_err}")
                                target.save(new_path)

                # 4. Remove original source file if requested
                if remove_original and os.path.exists(path) and os.path.abspath(path) != os.path.abspath(new_path):
                    os.remove(path)

                success_count += 1
            except Exception as e:
                failed_count += 1
                print(f"Failed to convert {path}: {e}")

        if success_count > 0:
            qual_str = f" (Quality: {quality})" if target_format != "PNG" else ""
            msg = f"Successfully converted {success_count} images to {target_format}{qual_str}."
            if failed_count > 0:
                msg += f" ({failed_count} failed)."
            self.set_status(msg)

            if paths and os.path.dirname(paths[0]):
                self.load_images_from_folder(os.path.dirname(paths[0]))

            complete_dialog = BatchCompleteDialog(self, "Batch Conversion Complete", msg)
            complete_dialog.exec()
        else:
            err_msg = f"Conversion failed for all {len(paths)} selected images."
            self.set_status(err_msg)
            complete_dialog = BatchCompleteDialog(self, "Batch Conversion Failed", err_msg)
            complete_dialog.exec()

    def batch_purge_exif(self):
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images selected for EXIF purge.")
            return

        success_count = 0
        for path in paths:
            try:
                with Image.open(path) as img:
                    data = list(img.getdata())
                    clean_img = Image.new(img.mode, img.size)
                    clean_img.putdata(data)
                    clean_img.save(path)  # Overwrites original or saves clean copy
                success_count += 1
            except Exception as e:
                print(f"Failed to purge EXIF for {path}: {e}")
        self.set_status(f"Purged EXIF data from {success_count} images.")
        dialog = BatchCompleteDialog(self, "EXIF Purge Complete",
                                     f"Successfully purged EXIF data from {success_count} images.")
        dialog.exec()

    def batch_export_exif(self):
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images selected for EXIF export.")
            return

        export_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory for EXIF JSON")
        if not export_dir:
            return

        import json
        success_count = 0
        last_json_filename = ""

        for path in paths:
            try:
                base_name = os.path.splitext(os.path.basename(path))[0]
                json_filename = f"{base_name}_EXIF.json"
                out_json = os.path.join(export_dir, json_filename)
                last_json_filename = json_filename

                # Use helper function to extract metadata dictionary
                _, metadata_dict = extract_image_metadata(path)

                with open(out_json, "w", encoding="utf-8") as f:
                    json.dump(metadata_dict, f, indent=4)
                success_count += 1
            except Exception as e:
                print(f"Failed to export EXIF for {path}: {e}")

        if success_count == 1:
            msg = f"Exported EXIF report as {last_json_filename}"
        elif success_count > 1:
            msg = f"Exported {success_count} EXIF reports (e.g., {last_json_filename})"
        else:
            msg = "Failed to export EXIF reports."

        self.set_status(msg)
        dialog = BatchCompleteDialog(self, "EXIF Export Complete", msg)
        dialog.exec()

    def batch_inject_exif(self):
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images selected for EXIF injection.")
            return

        import json
        source_filter = "Source Files (*.jpg *.jpeg *.png *.json)"
        source_file, _ = QFileDialog.getOpenFileName(
            self, "Select EXIF Source (Image or JSON Report)", "", source_filter
        )

        if not source_file:
            return

        try:
            exif_data_bytes = None
            exif_dict = {}

            # Case 1: Source is a JSON report (*_EXIF.json)
            if source_file.endswith(".json"):
                with open(source_file, "r", encoding="utf-8") as f:
                    content = json.load(f)
                    if isinstance(content, dict):
                        exif_dict = content

            # Case 2: Source is a reference image file (JPG, PNG, WebP, etc.)
            else:
                exif_data_bytes, exif_dict = extract_image_metadata(source_file)

            # Build reverse TAGS lookup for JPEGs
            _TAGS_r = {v: k for k, v in TAGS.items()}

            success_count = 0
            for path in paths:
                file_ext = os.path.splitext(path)[1].lower()
                with Image.open(path) as target:
                    if file_ext == '.png':
                        from PIL import PngImagePlugin
                        meta = PngImagePlugin.PngInfo()
                        if hasattr(target, 'text') and target.text:
                            for k, v in target.text.items():
                                if not k.lower().startswith("jfif"):
                                    meta.add_text(k, str(v))
                        for k, v in exif_dict.items():
                            meta.add_text(str(k), str(v))
                        target.save(path, pnginfo=meta)
                    else:
                        if exif_data_bytes:
                            target.save(path, exif=exif_data_bytes)
                        elif exif_dict:
                            try:
                                exif_obj = target.getexif()
                                if exif_obj is None:
                                    exif_obj = Image.Exif()
                                for k, v in exif_dict.items():
                                    if k in _TAGS_r:
                                        tag_id = _TAGS_r[k]
                                        try:
                                            exif_obj[tag_id] = v
                                        except Exception:
                                            pass
                                target.save(path, exif=exif_obj.tobytes())
                            except Exception:
                                target.save(path)
                        else:
                            target.save(path)
                success_count += 1

            msg = f"Successfully injected metadata into {success_count} images."
            self.set_status(msg)
            dialog = BatchCompleteDialog(self, "EXIF Injection Complete", msg)
            dialog.exec()
        except Exception as e:
            self.set_status(f"EXIF injection failed: {e}")

    #  -----------settings menu functions--------------
    def open_settings_dialog(self):
        self.set_status("Opening Settings Window...")
        dialog = SettingsDialog(self)
        dialog.exec()
        self.set_status("Ready")

    #  -----------about menu functions--------------
    def show_about_dialog(self):
        self.set_status("Viewing About Dialog...")
        dialog = AboutDialog(self)
        dialog.exec()
        self.set_status("Ready")

    #  -----------directory changes watch--------------
    def _on_directory_changed(self, path):
        """Triggered when files are added, modified, or deleted externally in the current folder."""
        if not self.current_watched_dir or not os.path.exists(self.current_watched_dir):
            return

        # Use a short 300ms debounce timer to batch rapid-fire filesystem triggers together
        if not hasattr(self, '_watcher_debounce_timer'):
            self._watcher_debounce_timer = QTimer(self)
            self._watcher_debounce_timer.setSingleShot(True)
            self._watcher_debounce_timer.timeout.connect(self._perform_smooth_folder_sync)

        self._watcher_debounce_timer.start(300)

    def _perform_smooth_folder_sync(self):
        """Smart sync that checks for new/external file changes without harsh UI resets."""
        if not self.current_watched_dir or not os.path.exists(self.current_watched_dir):
            return

        # Quick directory file scan just to check diffs
        valid_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
        try:
            current_disk_files = {
                os.path.abspath(os.path.join(self.current_watched_dir, f))
                for f in os.listdir(self.current_watched_dir)
                if os.path.isfile(os.path.join(self.current_watched_dir, f))
                   and os.path.splitext(f)[1].lower() in valid_exts
            }

            loaded_paths = {os.path.abspath(item["file_path"]) for item in self.loaded_items}

            # If disk matches memory exactly, do nothing (prevents unnecessary layout reloads)
            if current_disk_files == loaded_paths:
                return

            # If files were only removed externally, prune them locally without a full scan
            if current_disk_files.issubset(loaded_paths):
                removed = loaded_paths - current_disk_files
                self.loaded_items = [
                    item for item in self.loaded_items
                    if os.path.abspath(item["file_path"]) not in removed
                ]
                self.workspace_grid.set_cards(self.loaded_items)
                return

            # Only if brand new files were added externally do we run a soft background refresh
            self.load_images_from_folder(self.current_watched_dir)
        except Exception as e:
            print(f"Error during smooth folder sync: {e}")
    #  -----------mouse events functions--------------
    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)

    def closeEvent(self, event):
        # Read keep_alive preference dynamically from your settings/config backend
        settings = load_settings(SETTINGS_FILE)
        keep_alive = settings.get("keep_detached_viewers_alive", False)

        # Close all active detached viewers if keep_alive is False
        if hasattr(self, 'detached_viewers') and not keep_alive:
            for viewer in list(self.detached_viewers):
                if viewer:
                    viewer.close()

        event.accept()