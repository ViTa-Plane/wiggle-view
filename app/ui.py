import os
import shutil
import tempfile
from collections import OrderedDict
from PIL import Image
from PIL.ExifTags import TAGS

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QGraphicsDropShadowEffect, QFileDialog, QScrollArea, QSplitter, QDialog, QMenu,
    QMessageBox, QTextEdit
)
from PySide6.QtCore import Qt, QThreadPool, QTimer, QRunnable, Signal, QObject, QFileSystemWatcher, QMimeData, QUrl
from PySide6.QtGui import QColor, QImageReader, QGuiApplication, QImage

from .img_viewer import ImageViewerWindow, DetachedImageViewerWindow
from .dialogs import AboutDialog, SettingsDialog, BatchCompleteDialog, BatchConvertDialog
from .workers import (
    ThumbnailLoaderTask, ThumbnailSignalEmitter, FolderScannerSignals, FolderScannerTask,
    BatchConvertWorker, BatchConvertWorkerSignals, ComfyMetadataScannerSignals, ComfyMetadataScannerTask,
    CacheCleanupTask
)
from .widgets import WindowDragHandler, TitleBar, ResponsiveGridWidget, ResizeHandleWidget
from .components import SidebarPanel, DirectoryBrowserPanel, TopMenuBar
from .helpers import (
    load_settings, save_settings, format_file_size, extract_exif_metadata,
    add_folder_to_favorites, extract_image_metadata
)

SETTINGS_FILE = "settings.json"


class MetadataWorkerSignals(QObject):
    metadata_ready = Signal(str, str, str, str, str)  # file_path, filename, dims_str, exif_text, comfy_text


class MetadataLoaderTask(QRunnable):
    """Background worker task to load QImage dimensions, EXIF, and ComfyUI metadata off the main GUI thread."""

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

        # Check for ComfyUI workflow/prompt in PNG metadata
        comfy_text = ""
        if self.file_path.lower().endswith(".png"):
            try:
                with Image.open(self.file_path) as img:
                    if hasattr(img, "text") and img.text:
                        for key in ["workflow", "prompt"]:
                            if key in img.text:
                                import json
                                try:
                                    parsed = json.loads(img.text[key])
                                    comfy_text = json.dumps(parsed, indent=4)
                                    break
                                except Exception:
                                    comfy_text = img.text[key]
                                    break
            except Exception as e:
                print(f"Error scanning ComfyUI metadata: {e}")

        self.signals.metadata_ready.emit(self.file_path, self.filename, dims_str, exif_text, comfy_text)


class MainWindow(QMainWindow, WindowDragHandler):
    def __init__(self, initial_image_path=None):
        super().__init__(parent=None)
        WindowDragHandler.__init__(self)
        self.resize(1420, 860)
        self.setMinimumSize(750, 500)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.CARD_IMAGE_SIZE = 170
        self.GRID_CELL_WIDTH = 200
        self.GRID_CELL_HEIGHT = 220

        self.apply_concurrency_settings()

        self.thread_pool = QThreadPool.globalInstance()

        self.thumbnail_signals = ThumbnailSignalEmitter()
        self.thumbnail_signals.thumbnail_ready.connect(self.on_thumbnail_ready)

        self.scanner_signals = FolderScannerSignals()
        self.scanner_signals.batch_scanned.connect(self.on_batch_scanned)
        self.scanner_signals.scan_complete.connect(self.on_scan_complete)

        # Background metadata loading signals
        self.meta_signals = MetadataWorkerSignals()
        self.meta_signals.metadata_ready.connect(self.on_metadata_ready)

        # thumbnail directory
        self.thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")
        os.makedirs(self.thumb_cache_dir, exist_ok=True)

        # --- Trigger initial background cache cleanup on startup ---
        from .workers import CacheCleanupTask
        self.thread_pool.start(CacheCleanupTask(self.thumb_cache_dir))
        # -----------------------------------------------------------

        self.loaded_items = []
        self.items_by_path = {}  # O(1) lookup map: file_path -> item_dict
        self.pending_thumbnail_files = []
        self.scan_id = 0	# Track active scan generation

        # Coalesced grid repaint timer (prevents thousands of redundant repaints)
        self._grid_repaint_timer = QTimer(self)
        self._grid_repaint_timer.setSingleShot(True)
        self._grid_repaint_timer.setInterval(50)
        self._grid_repaint_timer.timeout.connect(self._flush_grid_repaint)
        self._needs_layout_update = False

        # Sorting state tracking
        self.current_sort_criteria = "name"
        self.sort_reverse = False

        # Initialize the File System Watcher
        self.file_watcher = QFileSystemWatcher(self)
        self.file_watcher.directoryChanged.connect(self._on_directory_changed)
        self.current_watched_dir = None

        '''Trigger the lazy ComfyUI scanner once the scan completes so badges 
        fade/pop in smoothly without slowing down the initial grid population:'''

        # Replace comfy_status_ready with comfy_batch_ready
        self.comfy_signals = ComfyMetadataScannerSignals()
        self.comfy_signals.comfy_batch_ready.connect(self.on_comfy_batch_ready)
        
        '''folder-state dictionary cache'''
        self.folder_scan_cache = OrderedDict()
        self._MAX_FOLDER_CACHE = 10	# Remembers the last 10 scanned directories instantly

        self.startup_setings_print()


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

    def _get_active_scan_id(self):
        return self.scan_id

    def _schedule_grid_repaint(self, relayout=False):
        if relayout:
            self._needs_layout_update = True
        if not self._grid_repaint_timer.isActive():
            self._grid_repaint_timer.start()

    def _flush_grid_repaint(self):
        if self._needs_layout_update:
            self._needs_layout_update = False
            self.workspace_grid.set_cards(self.loaded_items)
        else:
            self.workspace_grid.update()

    def _rebuild_path_index(self):
        self.items_by_path = {item["file_path"]: item for item in self.loaded_items}

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
        self.menu_bar.add_favorite_requested.connect(self.add_current_folder_to_favorites)
        self.menu_bar.convert_requested.connect(self.batch_convert_images)
        self.menu_bar.purge_exif_requested.connect(self.batch_purge_exif)
        self.menu_bar.inject_exif_requested.connect(self.batch_inject_exif)
        self.menu_bar.export_exif_requested.connect(self.batch_export_exif)
        self.menu_bar.export_comfy_workflow_requested.connect(self.batch_export_comfy_workflow)
        self.menu_bar.settings_requested.connect(self.open_settings_dialog)

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

        # --- Right Panel (Includes ComfyUI Metadata Tab) ---
        self.right_sidebar = SidebarPanel(self)
       # Check and add ComfyUI tab dynamically if sidebar supports tab widgets
        if hasattr(self.right_sidebar, 'tab_widget') and not hasattr(self.right_sidebar, 'comfy_view'):
            self.right_sidebar.comfy_view = QTextEdit()
            self.right_sidebar.comfy_view.setReadOnly(True)
            self.right_sidebar.comfy_view.setStyleSheet("""
                QTextEdit {
                    background-color: #0b0f19;
                    color: #38bdf8;
                    border: 1px solid #1e293b;
                    border-radius: 6px;
                    font-family: monospace;
                    font-size: 11px;
                }
            """)
            self.right_sidebar.tab_widget.addTab(self.right_sidebar.comfy_view, "ComfyUI")

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

    def apply_concurrency_settings(self):
        settings = load_settings(SETTINGS_FILE)
        concurrency = settings.get("thread_pool_concurrency", "Auto (Max Cores)")

        max_threads = QThreadPool.globalInstance().maxThreadCount()
        if "2 Threads" in concurrency:
            max_threads = 2
        elif "4 Threads" in concurrency:
            max_threads = 4
        elif "Auto" in concurrency:
            import multiprocessing
            max_threads = max(2, multiprocessing.cpu_count())

        QThreadPool.globalInstance().setMaxThreadCount(max_threads)

    def startup_setings_print(self):
        from .helpers import load_settings
        settings = load_settings()
        print(f"thumbnail_jpeg_quality: {settings.get("thumbnail_jpeg_quality", None)}")
        print(f"encrypt_thumbnail_cache: {settings.get("encrypt_thumbnail_cache", None)}")

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

        if self.current_watched_dir and self.current_watched_dir != folder_path:
            self.file_watcher.removePath(self.current_watched_dir)
        self.current_watched_dir = folder_path
        if folder_path not in self.file_watcher.directories():
            self.file_watcher.addPath(folder_path)

        self.save_last_folder(folder_path)
        self.left_browser.set_selected_folder(folder_path)
        self.path_label.setText(f"{folder_path}")

        self.scan_id += 1
        current_scan_id = self.scan_id

        # Instant RAM cache hit when switching back to a recently visited directory
        if folder_path in self.folder_scan_cache:
            self.folder_scan_cache.move_to_end(folder_path)
            self.loaded_items = list(self.folder_scan_cache[folder_path])
            self._rebuild_path_index()

            # Ensure any uncached pixmaps are decoded in background
            for item in self.loaded_items:
                if not self.workspace_grid.has_cached_pixmap(item["file_path"]):
                    task = ThumbnailLoaderTask(
                        item["file_path"],
                        self.thumb_cache_dir,
                        self.CARD_IMAGE_SIZE,
                        self.thumbnail_signals,
                        scan_id=current_scan_id,
                        active_scan_getter=self._get_active_scan_id,
                        existing_thumb_path=item.get("thumb_path")
                    )
                    self.thread_pool.start(task)

            self.apply_sorting()
            self.set_status(f"Ready (Cached) — Total images: {len(self.loaded_items)}")
            return

        self.pending_thumbnail_files.clear()
        self.loaded_items.clear()
        self.items_by_path.clear()
        self.workspace_grid.set_cards(self.loaded_items)
        # Track total items processed during the active scan
        self._current_scan_count = 0
        # Track thumbnail generation progress
        self._total_thumbs_to_generate = 0
        self._thumbs_ready_count = 0

        # Trim pixmap cache to free RAM when switching folders
        if hasattr(self.workspace_grid, 'clear_cache_if_needed'):
            self.workspace_grid.clear_cache_if_needed(max_allowed=3000)

        self.set_status(f"Scanning folder: {os.path.basename(folder_path)}")
        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• Folder: {os.path.basename(folder_path)}\n• Scanning..."
        )

        scanner_task = FolderScannerTask(
            folder_path,
            current_scan_id,
            self.scanner_signals,
            active_scan_getter=self._get_active_scan_id
        )
        self.thread_pool.start(scanner_task)

    def on_batch_scanned(self, scan_id, batch):
        if scan_id != self.scan_id:
            return

        new_thumbs_needed = 0
        for item_data in batch:
            file_path = item_data["file_path"]
            self.loaded_items.append(item_data)
            self.items_by_path[file_path] = item_data

            # Only spawn worker if QPixmap isn't already in RAM cache
            if not self.workspace_grid.has_cached_pixmap(file_path):
                new_thumbs_needed += 1
                task = ThumbnailLoaderTask(
                    file_path,
                    self.thumb_cache_dir,
                    self.CARD_IMAGE_SIZE,
                    self.thumbnail_signals,
                    scan_id=scan_id,
                    active_scan_getter=self._get_active_scan_id,
                    existing_thumb_path=item_data.get("thumb_path")
                )
                self.thread_pool.start(task)

        # self._total_thumbs_to_generate += new_thumbs_needed
        # self._current_scan_count = len(self.loaded_items)
        #
        # # Show live count of discovered items & active thumbnail generation
        # if self._total_thumbs_to_generate > 0:
        #     left_to_gen = max(0, self._total_thumbs_to_generate - self._thumbs_ready_count)
        #     self.set_status(
        #         f"Scanning... Discovered {self._current_scan_count} images | Thumbnails Ready: {self._thumbs_ready_count} | Left: {left_to_gen}")
        # else:
        #     self.set_status(f"Scanning folder... Discovered {self._current_scan_count} images (Cached)")

        self.workspace_grid.set_cards(self.loaded_items)

    def on_scan_complete(self, scan_id, total_count):
        if scan_id != self.scan_id:
            return

        if total_count == 0:
            self.set_status("No images found in folder")
        else:
            if self.current_sort_criteria != "name" or self.sort_reverse:
                self.apply_sorting()
            else:
                self.loaded_items.sort(
                    key=lambda x: (os.path.dirname(x.get("file_path", "")).lower(), x.get("filename", ""))
                )
                self._schedule_grid_repaint(relayout=True)
                self.set_status(f"Ready — Total images in gallery: {total_count}")

            # Store scanned directory in RAM cache
            if self.current_watched_dir:
                self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)
                if len(self.folder_scan_cache) > self._MAX_FOLDER_CACHE:
                    self.folder_scan_cache.popitem(last=False)

            comfy_task = ComfyMetadataScannerTask(
                self.loaded_items,
                scan_id,
                self.comfy_signals,
                active_scan_getter=self._get_active_scan_id
            )
            self.thread_pool.start(comfy_task)

            if hasattr(self, 'initial_target_image') and self.initial_target_image:
                target = self.initial_target_image
                self.initial_target_image = None
                self.open_image_viewer(target)

    def on_comfy_batch_ready(self, scan_id, file_paths):
        if scan_id != self.scan_id:
            return
        for fp in file_paths:
            item = self.items_by_path.get(fp)
            if item is not None:
                item["has_comfy"] = True
        self._schedule_grid_repaint(relayout=False)

    def on_thumbnail_ready(self, scan_id, file_path, thumb_path, qimage):
        if scan_id != self.scan_id:
            return
        item = self.items_by_path.get(file_path)
        if item is not None:
            item["thumb_path"] = thumb_path
        self.workspace_grid.cache_thumbnail_image(file_path, qimage)
        self._schedule_grid_repaint(relayout=False)

        # Track completed count quietly without hammering set_status()
        self._thumbs_ready_count += 1

        # Throttle status bar updates using a single-shot timer (e.g., every 150ms)
        if not hasattr(self, '_status_throttle_timer'):
            self._status_throttle_timer = QTimer(self)
            self._status_throttle_timer.setSingleShot(True)
            self._status_throttle_timer.timeout.connect(self._flush_thumbnail_status)

        if not self._status_throttle_timer.isActive():
            self._status_throttle_timer.start(150)


    def _flush_thumbnail_status(self):
        """Batch-updates the status footer smoothly with live RAM cache consumption."""
        left_to_gen = max(0, self._total_thumbs_to_generate - self._thumbs_ready_count)

        # Get live RAM usage from the grid cache
        cache_bytes = self.workspace_grid.get_cache_memory_usage()
        cache_size_str = format_file_size(cache_bytes)
        cache_count = len(self.workspace_grid.pixmap_cache)
        max_cache = self.workspace_grid._MAX_PIXMAP_CACHE_ITEMS

        if left_to_gen > 0:
            self.set_status(
                f"Generating thumbnails... Ready: {self._thumbs_ready_count} | Left: {left_to_gen} | RAM Cache: {cache_count}/{max_cache} items ({cache_size_str})")
        else:
            self.set_status(
                f"Ready — Total images in gallery: {len(self.loaded_items)} | RAM Cache: {cache_count} items ({cache_size_str})")

    def on_card_selected(self, file_path, display_name):
        filename = os.path.basename(file_path)
        item_info = self.items_by_path.get(file_path)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        # 1. Instant GUI feedback with zero main-thread delay
        detailed_status = f"Selected: {filename}  |  Size: {size_str}"
        self.status_label.setText(f"Status: {detailed_status}")

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• File: {filename}\n• Size: {size_str}\n• Resolution: Loading (BG)...\n• Path: {file_path}"
        )
        self.right_sidebar.exif_view.setHtml("<i>Loading metadata in background thread...</i>")
        if hasattr(self.right_sidebar, 'comfy_view'):
            self.right_sidebar.comfy_view.setPlainText("Loading ComfyUI workflow...")

        # 2. Offload heavy metadata & EXIF disk reads to background thread pool
        task = MetadataLoaderTask(file_path, filename, size_str, self.meta_signals)
        self.thread_pool.start(task)

    def on_metadata_ready(self, file_path, filename, dims_str, exif_text, comfy_text):
        if not self.workspace_grid.selected_file_paths or file_path not in self.workspace_grid.selected_file_paths:
            return

        item_info = self.items_by_path.get(file_path)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n• File: {filename}\n• Size: {size_str}\n• Resolution: {dims_str}\n• Path: {file_path}"
        )
        self.right_sidebar.exif_view.setHtml(exif_text)

        if hasattr(self.right_sidebar, 'comfy_view'):
            if comfy_text:
                self.right_sidebar.comfy_view.setPlainText(comfy_text)
            elif file_path.lower().endswith(".png"):
                self.right_sidebar.comfy_view.setPlainText("No ComfyUI workflow or prompt found in this PNG.")
            else:
                self.right_sidebar.comfy_view.setPlainText("ComfyUI workflows are only stored in PNG metadata.")

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
        if hasattr(self.workspace_grid, 'selected_file_paths') and self.workspace_grid.selected_file_paths:
            paths = list(self.workspace_grid.selected_file_paths)
        else:
            paths = [item["file_path"] for item in self.loaded_items]
        return [os.path.abspath(p) for p in paths if p and os.path.exists(p)]

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
            if hasattr(self.right_sidebar, 'comfy_view'):
                self.right_sidebar.comfy_view.setPlainText("Select a PNG with ComfyUI workflow to view data.")
            # Display total image count when selection is cleared / none selected
            self.set_status(f"Ready — Total images in gallery: {total}")
        elif count == 1:
            self.on_card_selected(selected_paths[0], os.path.basename(selected_paths[0]))
        else:
            total_bytes = sum(
                self.items_by_path[p].get("size", 0) for p in selected_paths if p in self.items_by_path
            )
            size_str = format_file_size(total_bytes)
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Multiple Items Selected\n• Selected: {count} of {total}\n• Combined Size: {size_str}"
            )
            self.right_sidebar.exif_view.setHtml(
                f"<i>{count} images selected.<br>Select a single image to inspect EXIF metadata.</i>"
            )
            if hasattr(self.right_sidebar, 'comfy_view'):
                self.right_sidebar.comfy_view.setPlainText("Multiple items selected.")
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

        # Check if any cards are explicitly selected in the grid
        has_selection = hasattr(self.workspace_grid, 'selected_file_paths') and len(
            self.workspace_grid.selected_file_paths) > 0

        # Disable Copy, Cut, and Delete if no image cards are selected
        copy_action.setEnabled(has_selection)
        cut_action.setEnabled(has_selection)
        delete_action.setEnabled(has_selection)

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()
        paste_action.setEnabled(mime_data.hasUrls())

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
        if not hasattr(self, 'current_watched_dir') or not self.current_watched_dir:
            self.set_status("No active folder destination for pasting.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()

        if not mime_data.hasUrls():
            self.set_status("No files found in clipboard to paste.")
            return

        if self.current_watched_dir:
            self.file_watcher.removePath(self.current_watched_dir)

        valid_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
        newly_added_items = []
        success_count = 0

        # Capture cut source paths securely before processing URLs
        cut_paths = getattr(self, '_cut_source_paths', [])

        for url in mime_data.urls():
            src_path = url.toLocalFile()
            if src_path and os.path.exists(src_path):
                ext = os.path.splitext(src_path)[1].lower()
                if ext not in valid_exts:
                    continue
                try:
                    filename = os.path.basename(src_path)
                    dest_path = os.path.join(self.current_watched_dir, filename)

                    base, file_ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(self.current_watched_dir, f"{base}_copy{counter}{file_ext}")
                        counter += 1

                    # Check if this specific source path was marked as a cut operation
                    if cut_paths and os.path.abspath(src_path) in [os.path.abspath(p) for p in cut_paths]:
                        shutil.move(src_path, dest_path)
                        action_performed = "Moved"
                    else:
                        shutil.copy2(src_path, dest_path)
                        action_performed = "Copied"

                    file_stats = os.stat(dest_path)
                    file_name = os.path.basename(dest_path)

                    has_comfy = False
                    if dest_path.lower().endswith(".png"):
                        try:
                            with Image.open(dest_path) as img:
                                if hasattr(img, "text") and img.text and ("workflow" in img.text or "prompt" in img.text):
                                    has_comfy = True
                        except Exception:
                            pass

                    item_data = {
                        "file_path": dest_path,
                        "filename": file_name.lower(),
                        "display_name": (file_name[:16] + '..') if len(file_name) > 18 else file_name,
                        "size": file_stats.st_size,
                        "mtime": file_stats.st_mtime,
                        "thumb_path": None,
                        "has_comfy": has_comfy
                    }

                    newly_added_items.append(item_data)
                    task = ThumbnailLoaderTask(
                        dest_path,
                        self.thumb_cache_dir,
                        self.CARD_IMAGE_SIZE,
                        self.thumbnail_signals,
                        scan_id=self.scan_id,
                        active_scan_getter=self._get_active_scan_id
                    )
                    self.thread_pool.start(task)
                    success_count += 1
                except Exception as e:
                    print(f"Failed to paste/move {src_path}: {e}")

        # Clear cut source paths after operation is finished
        self._cut_source_paths = []
        if self.current_watched_dir:
            self.file_watcher.addPath(self.current_watched_dir)

        if success_count > 0:
            self.loaded_items.extend(newly_added_items)
            self._rebuild_path_index()
            if self.current_watched_dir in self.folder_scan_cache:
                self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)
            self.apply_sorting()
            self.set_status(f"Successfully processed {success_count} file(s).")
        else:
            self.set_status("No valid image files pasted.")

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
            self.status_label.setText("Status: Deletion failed - No valid images selected.")
            return

        # --- Check setting for deletion confirmation ---
        settings = load_settings(SETTINGS_FILE)
        confirm_enabled = settings.get("confirm_on_delete", True)

        if confirm_enabled:
            reply = QMessageBox.question(
                self, "Confirm Delete",
                f"Are you sure you want to move {len(paths)} selected file(s) to the Recycle Bin?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply != QMessageBox.StandardButton.Yes:
                return
        # ---------------------------------------------

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
            self._rebuild_path_index()
            if self.current_watched_dir in self.folder_scan_cache:
                self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)

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
            if hasattr(self, 'directory_browser') and self.left_browser:
                self.left_browser.set_selected_folder(folder_path)

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
            self.menu_bar.clear()
            self.menu_bar.setup_ui()
            folder_name = os.path.basename(os.path.normpath(current_folder))
            self.set_status(f"Added to favorites: {folder_name}")
        else:
            self.set_status("No active folder to add to favorites.")

    def batch_convert_images(self):
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images available for conversion.")
            return

        # Store as instance variable to prevent premature GC crash
        self._active_dialog = BatchConvertDialog(self)
        if self._active_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target_format = self._active_dialog.format_combo.currentText()
        quality = self._active_dialog.quality_slider.value()
        transfer_exif = self._active_dialog.transfer_exif_cb.isChecked()
        remove_original = self._active_dialog.remove_original_cb.isChecked()

        self.set_status("Starting background batch image conversion...")
        self.convert_signals = BatchConvertWorkerSignals()
        self.convert_signals.progress_update.connect(self.set_status)
        self.convert_signals.finished.connect(lambda s, f, msg: self.on_batch_convert_finished(s, f, msg, paths))

        worker = BatchConvertWorker(paths, target_format, quality, transfer_exif, remove_original, self.convert_signals)
        self.thread_pool.start(worker)

    def on_batch_convert_finished(self, success_count, failed_count, msg, original_paths):
        self.set_status(msg)
        if success_count > 0 and original_paths and os.path.dirname(original_paths[0]):
            folder = os.path.dirname(original_paths[0])
            self.folder_scan_cache.pop(folder, None)
            self.load_images_from_folder(folder)

        BatchCompleteDialog(
            self,
            "Batch Conversion Complete" if success_count > 0 else "Batch Conversion Failed",
            msg
        ).exec()

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
        BatchCompleteDialog(self, "EXIF Purge Complete",
                            f"Successfully purged EXIF data from {success_count} images.").exec()

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
        BatchCompleteDialog(self, "EXIF Export Complete", msg).exec()

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
            BatchCompleteDialog(self, "EXIF Injection Complete", msg).exec()
        except Exception as e:
            self.set_status(f"EXIF injection failed: {e}")

    def batch_export_comfy_workflow(self):
        """Exports ComfyUI workflows or prompts from selected PNG images to JSON reports named imageName_workflow.json."""
        paths = self._get_target_file_paths()
        if not paths:
            self.set_status("No images selected for ComfyUI workflow export.")
            return

        export_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory for ComfyUI Workflows")
        if not export_dir:
            return

        import json
        success_count = 0
        failed_count = 0

        for path in paths:
            if not path.lower().endswith(".png"):
                continue  # ComfyUI workflows are stored exclusively in PNG metadata

            try:
                workflow_content = None
                with Image.open(path) as img:
                    if hasattr(img, "text") and img.text:
                        for key in ["workflow", "prompt"]:
                            if key in img.text:
                                workflow_content = img.text[key]
                                break

                if workflow_content:
                    base_name = os.path.splitext(os.path.basename(path))[0]
                    out_json = os.path.join(export_dir, f"{base_name}_workflow.json")
                    try:
                        parsed_json = json.loads(workflow_content)
                        with open(out_json, "w", encoding="utf-8") as f:
                            json.dump(parsed_json, f, indent=4)
                    except Exception:
                        # Fallback to saving raw string data if JSON parsing fails
                        with open(out_json, "w", encoding="utf-8") as f:
                            f.write(workflow_content)
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                failed_count += 1
                print(f"Failed to export workflow for {path}: {e}")

        if success_count > 0:
            msg = f"Successfully exported {success_count} ComfyUI workflow(s)."
            if failed_count > 0:
                msg += f" ({failed_count} skipped/failed)."
            self.set_status(msg)
            BatchCompleteDialog(self, "ComfyUI Workflow Export Complete", msg).exec()
        else:
            self.set_status("No valid ComfyUI workflows found in the selected images.")
            BatchCompleteDialog(self, "Export Failed",
                                "No valid ComfyUI workflows found in the selected images.").exec()

    #  -----------settings menu functions--------------
    def open_settings_dialog(self):
        self.set_status("Opening Settings Window...")
        self._active_dialog = SettingsDialog(self)
        if self._active_dialog.exec() == QDialog.DialogCode.Accepted:
            # Refresh the grid cache limit dynamically right after settings are saved
            if hasattr(self.workspace_grid, 'update_cache_limit_from_settings'):
                self.workspace_grid.update_cache_limit_from_settings()
            self.apply_concurrency_settings()

            # Trigger cleanup immediately if disk limit was adjusted
            self.thread_pool.start(CacheCleanupTask(self.thumb_cache_dir))

            # Invalidate cached folder scans so child-folder / depth changes take effect
            self.folder_scan_cache.clear()
            if self.current_watched_dir and os.path.isdir(self.current_watched_dir):
                self.load_images_from_folder(self.current_watched_dir)

        self.set_status("Ready")

    #  -----------about menu functions--------------
    def show_about_dialog(self):
        self.set_status("Viewing About Dialog...")
        # Store as instance variable
        AboutDialog(self).exec()
        self.set_status("Ready")
        
    #  -----------directory changes watch--------------
    def _on_directory_changed(self, path):
        if not self.current_watched_dir or not os.path.exists(self.current_watched_dir):
            return

        # Use a short 300ms debounce timer to batch rapid-fire filesystem triggers together
        if not hasattr(self, '_watcher_debounce_timer'):
            self._watcher_debounce_timer = QTimer(self)
            self._watcher_debounce_timer.setSingleShot(True)
            self._watcher_debounce_timer.timeout.connect(self._perform_smooth_folder_sync)

        self._watcher_debounce_timer.start(300)


    def _collect_disk_image_paths(self):
        valid_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
        settings = load_settings(SETTINGS_FILE)
        include_subfolders = settings.get("include_child_folders", False)
        max_depth = max(1, int(settings.get("child_folder_depth", 1)))

        disk_files = set()
        if not include_subfolders:
            for f in os.listdir(self.current_watched_dir):
                full_path = os.path.abspath(os.path.join(self.current_watched_dir, f))
                if os.path.isfile(full_path) and os.path.splitext(f)[1].lower() in valid_exts:
                    disk_files.add(full_path)
            return disk_files

        base_depth = len(os.path.normpath(self.current_watched_dir).split(os.sep))
        for root, dirs, files in os.walk(self.current_watched_dir, followlinks=False, onerror=lambda e: None):
            current_depth = len(os.path.normpath(root).split(os.sep)) - base_depth
            if current_depth >= max_depth:
                dirs[:] = []

            for f in files:
                if os.path.splitext(f)[1].lower() in valid_exts:
                    disk_files.add(os.path.abspath(os.path.join(root, f)))

        return disk_files

    def _perform_smooth_folder_sync(self):
        """Smart sync that checks for new/external file changes without harsh UI resets."""
        if not self.current_watched_dir or not os.path.exists(self.current_watched_dir):
            return

        try:
            current_disk_files = self._collect_disk_image_paths()
            loaded_paths = {os.path.abspath(item["file_path"]) for item in self.loaded_items}

            if current_disk_files == loaded_paths:
                return

            if current_disk_files.issubset(loaded_paths):
                removed = loaded_paths - current_disk_files
                self.loaded_items = [
                    item for item in self.loaded_items
                    if os.path.abspath(item["file_path"]) not in removed
                ]
                self._rebuild_path_index()
                self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)
                self.workspace_grid.set_cards(self.loaded_items)
                return

            self.folder_scan_cache.pop(self.current_watched_dir, None)
            self.load_images_from_folder(self.current_watched_dir)
        except Exception as e:
            print(f"Error during smooth folder sync: {e}")

    def apply_sorting(self):
        if not self.loaded_items:
            return

        criteria = self.current_sort_criteria
        is_rev = self.sort_reverse

        if criteria == "name":
            self.loaded_items.sort(
                key=lambda x: (os.path.dirname(x.get("file_path", "")).lower(), x.get("filename", "")),
                reverse=is_rev
            )
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

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)

    def closeEvent(self, event):
        settings = load_settings(SETTINGS_FILE)

        # 1. Wait for any active background thread tasks to finish cleanly
        print("Closing the app, Wait for any active background thread tasks to finish cleanly")
        self.thread_pool.waitForDone()

        # 2. Purge thumbnail cache on exit if enabled
        if settings.get("purge_thumb_cache_on_exit", False):
            try:
                if os.path.exists(self.thumb_cache_dir):
                    shutil.rmtree(self.thumb_cache_dir)
                    os.makedirs(self.thumb_cache_dir, exist_ok=True)
                    print("purge_thumb_cache_on_exit: DONE")
            except Exception as e:
                print(f"Error purging thumbnail cache: {e}")

        # 3. Keep-alive check for detached viewers
        keep_alive = settings.get("keep_detached_viewers_alive", False)
        if hasattr(self, 'detached_viewers') and not keep_alive:
            for viewer in list(self.detached_viewers):
                if viewer:
                    try:
                        if not viewer.isHidden():
                            viewer.close()
                    except RuntimeError:
                        pass

        event.accept()