# version 0.0.17

import os
import tempfile

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QGraphicsDropShadowEffect, QFileDialog, QScrollArea, QSplitter
)
from PySide6.QtCore import Qt, QThreadPool, QTimer, QRunnable, Signal, QObject
from PySide6.QtGui import QColor, QImageReader, QGuiApplication

from .img_viewer import ImageViewerWindow, DetachedImageViewerWindow
from .dialogs import ChildTestDialog, AboutDialog, SettingsDialog
from .workers import ThumbnailLoaderTask, ThumbnailSignalEmitter, FolderScannerSignals, FolderScannerTask
from .widgets import WindowDragHandler, TitleBar, ResponsiveGridWidget, ResizeHandleWidget
from .components import SidebarPanel, DirectoryBrowserPanel, TopMenuBar
from .helpers import load_settings, save_settings, format_file_size, extract_exif_metadata,add_folder_to_favorites

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

        self._setup_ui()

        # Handle direct argument launch vs normal last-folder boot
        if initial_image_path and os.path.exists(initial_image_path):
            abs_path = os.path.abspath(initial_image_path)
            folder_dir = os.path.dirname(abs_path)

            self.left_browser.set_selected_folder(folder_dir)
            self.path_label.setText(f"{folder_dir}")
            self.save_last_folder(folder_dir)

            self.initial_target_image = abs_path
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

        # --- Top Menu Panel ---
        self.menu_bar = TopMenuBar(self, favorites=favorites_list)
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

        # Standard behavior[cite: 1, 2]
        self.set_status(f"Opening viewer for: {os.path.basename(file_path)}")
        self.hide()
        self.viewer_window = ImageViewerWindow(file_path, self)
        self.viewer_window.show()

    def open_child_window(self):
        self.set_status("Opening Child Window...")
        dialog = ChildTestDialog(self, custom_title="✨ Child Test Dialog Window")
        dialog.exec()
        self.set_status("Ready")

    def show_about_dialog(self):
        self.set_status("Viewing About Dialog...")
        dialog = AboutDialog(self)
        dialog.exec()
        self.set_status("Ready")

    def open_settings_dialog(self):
        self.set_status("Opening Settings Window...")
        dialog = SettingsDialog(self)
        dialog.exec()
        self.set_status("Ready")

    def set_sort_criteria(self, criteria):
        self.current_sort_criteria = criteria
        self.apply_sorting()

    def toggle_sort_reverse(self, is_reversed):
        self.sort_reverse = is_reversed
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