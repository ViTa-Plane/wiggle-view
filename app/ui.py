import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections import OrderedDict
from PIL import Image

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QGraphicsDropShadowEffect, QFileDialog, QScrollArea, QSplitter, QDialog,
    QTextEdit
)
from PySide6.QtCore import Qt, QThreadPool, QTimer, QRunnable, Signal, QObject, QFileSystemWatcher
from PySide6.QtGui import QColor, QImageReader, QGuiApplication, QShortcut, QKeySequence

from .img_viewer import ImageViewerWindow, DetachedImageViewerWindow
from .dialogs import AboutDialog, SettingsDialog
from .workers import (
    ThumbnailSignalEmitter, FolderScannerSignals, FolderScannerTask,
    ComfyMetadataScannerSignals, ComfyMetadataScannerTask, CacheCleanupTask
)
from .widgets import WindowDragHandler, TitleBar, ResponsiveGridWidget, ResizeHandleWidget
from .components import SidebarPanel, DirectoryBrowserPanel, TopMenuBar
from .helpers import (
    load_settings, save_settings, format_file_size, extract_exif_metadata,
    add_folder_to_favorites
)
from .file_ops import FileOperationsController
from .batch_tools import BatchToolsController
from .folder_sync import FolderSyncController

try:
    from .sqlite_thumb_cache import ThumbnailSQLiteCache
except ImportError:
    from sqlite_thumb_cache import ThumbnailSQLiteCache

SETTINGS_FILE = "settings.json"

try:
    from .crash_reporter import install_crash_reporter
except ImportError:
    from crash_reporter import install_crash_reporter

_CRASH_REPORTER = install_crash_reporter(app_name="WiggleView")


class MetadataWorkerSignals(QObject):
    metadata_ready = Signal(str, str, str, str, str)


class _SqliteThumbnailTask(QRunnable):
    """Generates or loads thumbnails strictly inside the SQLite BLOB database."""

    def __init__(self, source_path, db_cache, target_size, jpeg_quality, signals, scan_id, active_scan_getter, is_closing_getter=None):
        super().__init__()
        self.source_path = source_path
        self.db_cache = db_cache
        self.target_size = target_size
        self.jpeg_quality = jpeg_quality
        self.signals = signals
        self.scan_id = scan_id
        self.active_scan_getter = active_scan_getter
        self.is_closing_getter = is_closing_getter

    def _is_stale(self):
        if callable(self.is_closing_getter) and self.is_closing_getter():
            return True
        return callable(self.active_scan_getter) and self.scan_id != self.active_scan_getter()

    def run(self):
        if self._is_stale():
            return
        if self.db_cache is None or not os.path.exists(self.source_path):
            return
        qimg, virtual_path = self.db_cache.generate_and_cache_qimage(
            self.source_path,
            target_size=self.target_size,
            jpeg_quality=self.jpeg_quality
        )
        if self._is_stale():
            return
        if qimg is not None and not qimg.isNull():
            try:
                self.signals.thumbnail_ready.emit(self.scan_id, self.source_path, virtual_path, qimg)
            except RuntimeError:
                pass


class MetadataLoaderTask(QRunnable):
    """Background worker task to load QImage dimensions, EXIF, and ComfyUI metadata off the GUI thread."""

    def __init__(self, file_path, filename, size_str, signals, is_closing_getter=None):
        super().__init__()
        self.file_path = file_path
        self.filename = filename
        self.size_str = size_str
        self.signals = signals
        self.is_closing_getter = is_closing_getter

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def run(self):
        if self._is_closing():
            return
        dims_str = "Unknown dims"
        if os.path.exists(self.file_path):
            reader = QImageReader(self.file_path)
            img_size = reader.size()
            if img_size.isValid():
                dims_str = f"{img_size.width()} \u00d7 {img_size.height()} px"

        if self._is_closing():
            return
        exif_text = extract_exif_metadata(self.file_path)

        if self._is_closing():
            return
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

        if self._is_closing():
            return
        try:
            self.signals.metadata_ready.emit(self.file_path, self.filename, dims_str, exif_text, comfy_text)
        except RuntimeError:
            pass


class MainWindow(QMainWindow, WindowDragHandler):
    def __init__(self, initial_image_path=None):
        super().__init__(parent=None)
        WindowDragHandler.__init__(self)
        self._is_closing = False
        self.resize(1420, 860)
        self.setMinimumSize(750, 500)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.CARD_IMAGE_SIZE = 170
        self.GRID_CELL_WIDTH = 200
        self.GRID_CELL_HEIGHT = 220

        self.apply_concurrency_settings()
        self.thread_pool = QThreadPool.globalInstance()

        self.file_ops = FileOperationsController(self, SETTINGS_FILE)
        self.batch_tools = BatchToolsController(self)
        self.folder_sync = FolderSyncController(self, SETTINGS_FILE)

        self.thumbnail_signals = ThumbnailSignalEmitter()
        self.thumbnail_signals.thumbnail_ready.connect(self.on_thumbnail_ready)

        self.scanner_signals = FolderScannerSignals()
        self.scanner_signals.batch_scanned.connect(self.on_batch_scanned)
        self.scanner_signals.scan_complete.connect(self.on_scan_complete)

        self.meta_signals = MetadataWorkerSignals()
        self.meta_signals.metadata_ready.connect(self.on_metadata_ready)

        self.thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")
        os.makedirs(self.thumb_cache_dir, exist_ok=True)
        self.thumb_db_cache = ThumbnailSQLiteCache.get_shared(self.thumb_cache_dir)

        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
            self.thumb_db_cache.purge_loose_jpeg_files()
        self.thread_pool.start(CacheCleanupTask(self.thumb_cache_dir))

        self.loaded_items = []
        self.items_by_path = {}
        self.scan_id = 0
        self.total_thumbs_to_generate = 0
        self.thumbs_ready_count = 0

        self.undo_trash_dir = os.path.join(tempfile.gettempdir(), "wiggleview_undo_trash")
        os.makedirs(self.undo_trash_dir, exist_ok=True)

        self.grid_repaint_timer = QTimer(self)
        self.grid_repaint_timer.setSingleShot(True)
        self.grid_repaint_timer.setInterval(100)
        self.grid_repaint_timer.timeout.connect(self._flush_grid_repaint)
        self.needs_layout_update = False

        self.current_sort_criteria = "name"
        self.sort_reverse = False

        self.file_watcher = QFileSystemWatcher(self)
        self.file_watcher.directoryChanged.connect(self.folder_sync.on_directory_changed)
        self.current_watched_dir = None

        self.comfy_signals = ComfyMetadataScannerSignals()
        self.comfy_signals.comfy_batch_ready.connect(self.on_comfy_batch_ready)

        self.folder_scan_cache = OrderedDict()
        self._persistent_child_scan_status = None
        self._MAX_FOLDER_CACHE = 10
        self._MAX_WATCHED_DIRS = 256
        self._MAX_CHILD_SCAN_THUMB_TASKS = 512

        save_settings(SETTINGS_FILE, {"include_subfolders": False})

        self._refresh_cached_jpeg_quality()
        self.startup_settings_print()

        self._setup_ui()

        if initial_image_path and os.path.exists(initial_image_path):
            abs_path = os.path.abspath(initial_image_path)
            if os.path.isdir(abs_path):
                folder_dir = abs_path
                self.initial_target_image = None
            else:
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

        self.crash_log_path = getattr(_CRASH_REPORTER, "log_path", None)
        if self.crash_log_path:
            print(f"Crash report log: {self.crash_log_path}")
        QTimer.singleShot(120, self.center_current_folder_in_tree)

    def _is_shutting_down(self):
        return bool(self._is_closing)

    def _refresh_cached_jpeg_quality(self):
        settings = load_settings(SETTINGS_FILE)
        quality = settings.get("thumbnail_jpeg_quality", 90)
        try:
            quality = int(quality)
        except (TypeError, ValueError):
            quality = 90
        self._cached_jpeg_quality = max(1, min(100, quality))

    def start_thumbnail_task(self, file_path, scan_id):
        if self._is_closing:
            return
        if hasattr(self, "workspace_grid") and hasattr(self.workspace_grid, "mark_thumbnail_in_flight"):
            self.workspace_grid.mark_thumbnail_in_flight(file_path)
        quality = getattr(self, "_cached_jpeg_quality", 90)
        task = _SqliteThumbnailTask(
            file_path,
            getattr(self, "thumb_db_cache", None),
            self.CARD_IMAGE_SIZE,
            quality,
            self.thumbnail_signals,
            scan_id=scan_id,
            active_scan_getter=self.get_active_scan_id,
            is_closing_getter=self._is_shutting_down,
        )
        self.thread_pool.start(task)

    def get_active_scan_id(self):
        if self._is_closing:
            return -1
        return self.scan_id

    def _schedule_grid_repaint(self, relayout=False):
        if self._is_closing:
            return
        if relayout:
            self.needs_layout_update = True
        if not self.grid_repaint_timer.isActive():
            self.grid_repaint_timer.start()

    def _flush_grid_repaint(self):
        if self._is_closing:
            return
        if self.needs_layout_update:
            self.needs_layout_update = False
            self.sort_loaded_items_quietly()
            self.workspace_grid.set_cards(self.loaded_items)
        else:
            self.workspace_grid.update()

    def rebuild_path_index(self):
        self.items_by_path = {item["file_path"]: item for item in self.loaded_items}

    @staticmethod
    def _natural_name_key(name_str):
        """Splits digits and text so 'img_2' sorts before 'img_10' consistently."""
        s = (name_str or "").lower()
        return tuple(int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", s))

    def _item_name_sort_key(self, item):
        """Deterministic name sort key independent of stale cached path fields."""
        fp = os.path.abspath(item.get("file_path", "")) if item.get("file_path") else ""
        parent_dir = os.path.dirname(fp).lower() if fp else ""
        raw_name = item.get("filename") or os.path.basename(fp).lower() or item.get("display_name", "").lower()
        return (parent_dir, self._natural_name_key(raw_name), raw_name, fp.lower())

    def _sync_sort_state_from_settings(self):
        """Ensures active sort criteria/reverse stay synchronized with persisted settings."""
        settings_data = load_settings(SETTINGS_FILE)
        criteria = settings_data.get("sort_criteria")
        if isinstance(criteria, str) and criteria.strip():
            self.current_sort_criteria = criteria.strip().lower()
        if "sort_reverse" in settings_data:
            self.sort_reverse = bool(settings_data["sort_reverse"])

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

        settings_data = load_settings(SETTINGS_FILE)
        favorites_list = settings_data.get("favorites", [])
        max_favorites = settings_data.get("max_favorites", 15)
        self.current_sort_criteria = str(settings_data.get("sort_criteria", "name")).lower()
        self.sort_reverse = bool(settings_data.get("sort_reverse", False))

        self.menu_bar = TopMenuBar(
            self,
            favorites=favorites_list,
            sort_criteria=self.current_sort_criteria,
            sort_reverse=self.sort_reverse,
            max_favorites=max_favorites,
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
        self.menu_bar.convert_requested.connect(self.batch_tools.batch_convert_images)
        self.menu_bar.purge_exif_requested.connect(self.batch_tools.batch_purge_exif)
        self.menu_bar.inject_exif_requested.connect(self.batch_tools.batch_inject_exif)
        self.menu_bar.export_exif_requested.connect(self.batch_tools.batch_export_exif)
        self.menu_bar.export_comfy_workflow_requested.connect(self.batch_tools.batch_export_comfy_workflow)
        self.menu_bar.settings_requested.connect(self.open_settings_dialog)

        container_layout.addWidget(self.menu_bar)

        body_split_layout = QHBoxLayout()
        body_split_layout.setSpacing(12)

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

        self.left_browser = DirectoryBrowserPanel(self)
        self.left_browser.folder_selected.connect(self.load_images_from_folder)
        self.left_browser.files_dropped_on_folder.connect(self.file_ops.move_files_to_folder)
        self.left_browser.btn_browse.clicked.connect(self.open_current_folder_in_explorer)

        main_splitter.addWidget(self.left_browser)

        center_container = QWidget(self)
        center_container.setStyleSheet("background: transparent;")
        center_layout = QVBoxLayout(center_container)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(10)

        self.path_label = QLabel("...")
        self.path_label.setStyleSheet(
            "color: #f8fafc; font-family: sans-serif; font-size: 13px; background: transparent;"
        )
        center_layout.addWidget(self.path_label)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self._apply_scroll_speed_setting()
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

        self.workspace_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.workspace_grid.customContextMenuRequested.connect(self.file_ops.show_grid_context_menu)
        self.workspace_grid.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.workspace_grid.item_selected.connect(self.on_card_selected)
        self.workspace_grid.selection_changed.connect(self.on_selection_changed)
        self.workspace_grid.item_double_clicked.connect(self.open_image_viewer)
        self.scroll_area.setWidget(self.workspace_grid)

        center_layout.addWidget(self.scroll_area, stretch=1)
        main_splitter.addWidget(center_container)

        main_splitter.setSizes([220, 900])
        self.main_splitter = main_splitter
        self.main_splitter.splitterMoved.connect(lambda _pos, _idx: self._schedule_keep_selected_in_view())

        self._keep_selected_timer = QTimer(self)
        self._keep_selected_timer.setSingleShot(True)
        self._keep_selected_timer.setInterval(100)
        self._keep_selected_timer.timeout.connect(self._keep_selected_thumbnail_in_view)

        body_split_layout.addWidget(main_splitter, stretch=1)

        self.right_sidebar = SidebarPanel(self)
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

        self.undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.undo_shortcut.activated.connect(self.file_ops.undo)

        self.redo_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self.redo_shortcut.activated.connect(self.file_ops.redo)

        self.center_folder_shortcut = QShortcut(QKeySequence("U"), self)
        self.center_folder_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.center_folder_shortcut.activated.connect(self.center_current_folder_in_tree)

        self.scroll_to_selected_shortcut = QShortcut(QKeySequence("Y"), self)
        self.scroll_to_selected_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.scroll_to_selected_shortcut.activated.connect(self.scroll_to_selected_thumbnail_in_grid)

        self.toggle_child_scan_shortcut = QShortcut(QKeySequence("F"), self)
        self.toggle_child_scan_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.toggle_child_scan_shortcut.activated.connect(self.folder_sync.toggle_subfolder_scan)

        self.open_viewer_space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.open_viewer_space_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.open_viewer_space_shortcut.activated.connect(self.open_selected_or_first_image_in_viewer)

    def _apply_scroll_speed_setting(self):
        settings = load_settings(SETTINGS_FILE)
        try:
            scroll_speed = int(settings.get("scroll_speed", 15))
        except (TypeError, ValueError):
            scroll_speed = 15
        scroll_speed = max(1, min(29, scroll_speed))
        if hasattr(self, "scroll_area") and self.scroll_area is not None:
            self.scroll_area.verticalScrollBar().setSingleStep(scroll_speed)

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

    def startup_settings_print(self):
        settings = load_settings()
        print(f"thumbnail_jpeg_quality: {settings.get('thumbnail_jpeg_quality', None)}")
        print(f"encrypt_thumbnail_cache: {settings.get('encrypt_thumbnail_cache', None)}")

    def _center_folder_in_tree_for_path(self, folder_path):
        if self._is_closing or not folder_path or not os.path.isdir(folder_path):
            return
        if hasattr(self, "left_browser") and self.left_browser and hasattr(self.left_browser, "scroll_to_selected_folder"):
            self.left_browser.scroll_to_selected_folder(
                folder_path=os.path.abspath(folder_path),
                only_if_out_of_view=False,
                center_in_view=True
            )

    def load_last_folder(self):
        data = load_settings(SETTINGS_FILE)
        folder = data.get("last_folder", "")

        if not folder or not os.path.isdir(folder):
            pictures_dir = os.path.join(os.path.expanduser("~"), "Pictures")
            folder = pictures_dir if os.path.isdir(pictures_dir) else os.path.expanduser("~")

        if folder and os.path.isdir(folder):
            abs_folder = os.path.abspath(folder)
            self.left_browser.set_selected_folder(abs_folder)
            self.load_images_from_folder(abs_folder)
            self._center_folder_in_tree_for_path(abs_folder)
            for delay_ms in (60, 220, 500):
                QTimer.singleShot(delay_ms, lambda fp=abs_folder: self._center_folder_in_tree_for_path(fp))

    def save_last_folder(self, folder_path):
        save_settings(SETTINGS_FILE, {"last_folder": folder_path})

    def set_footer_status(self, message):
        if self._is_closing:
            return
        self.status_label.setText(f"Status: {message}")

    def set_status(self, message, log=True):
        if self._is_closing:
            return
        child_prefix = getattr(self, "_persistent_child_scan_status", None)
        if child_prefix and not message.startswith("Subfolder scan:"):
            footer_msg = f"{child_prefix} | {message}"
        else:
            footer_msg = message
        self.status_label.setText(f"Status: {footer_msg}")
        if log and hasattr(self.right_sidebar, 'log_text'):
            log_widget = self.right_sidebar.log_text
            if hasattr(log_widget, "appendPlainText"):
                log_widget.appendPlainText(f"\u2022 {message}")
            else:
                current_text = log_widget.toPlainText()
                prefix = f"{current_text}\n" if current_text else ""
                log_widget.setPlainText(f"{prefix}\u2022 {message}")
            log_widget.verticalScrollBar().setValue(
                log_widget.verticalScrollBar().maximum()
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
        if self._is_closing or not folder_path or not os.path.exists(folder_path):
            return

        self._sync_sort_state_from_settings()
        self.current_watched_dir = folder_path
        self._persistent_child_scan_status = None
        self.left_browser.set_selected_folder(folder_path)
        self.path_label.setText(f"{folder_path}")
        QTimer.singleShot(0, lambda fp=folder_path: self.save_last_folder(fp))
        if folder_path not in self.file_watcher.directories():
            QTimer.singleShot(15, lambda fp=folder_path: self.folder_sync.sync_file_watcher_paths(fp))

        self.scan_id += 1
        current_scan_id = self.scan_id
        self.total_thumbs_to_generate = 0
        self.thumbs_ready_count = 0
        if hasattr(self.workspace_grid, "clear_in_flight_thumbnails"):
            self.workspace_grid.clear_in_flight_thumbnails()

        if folder_path in self.folder_scan_cache:
            self.folder_scan_cache.move_to_end(folder_path)
            self.loaded_items = list(self.folder_scan_cache[folder_path])
            self.rebuild_path_index()
            self.sort_loaded_items_quietly()
            self.workspace_grid.set_cards(self.loaded_items)

            for item in self.loaded_items:
                fp = item["file_path"]
                already_pending = (
                    self.workspace_grid.is_thumbnail_pending(fp)
                    if hasattr(self.workspace_grid, "is_thumbnail_pending")
                    else self.workspace_grid.has_cached_pixmap(fp)
                )
                if not already_pending:
                    self.total_thumbs_to_generate += 1
                    self.start_thumbnail_task(fp, current_scan_id)

            self.set_status(f"Ready (Cached) \u2014 Total images: {len(self.loaded_items)}")
            QTimer.singleShot(30, self.folder_sync.perform_smooth_folder_sync)
            return

        self.loaded_items.clear()
        self.items_by_path.clear()
        self.workspace_grid.set_cards(self.loaded_items)

        if hasattr(self.workspace_grid, 'clear_cache_if_needed'):
            self.workspace_grid.clear_cache_if_needed(max_allowed=3000)

        self.set_status(f"Scanning folder: {os.path.basename(folder_path)}")
        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n\u2022 Folder: {os.path.basename(folder_path)}\n\u2022 Scanning..."
        )

        scanner_task = FolderScannerTask(
            folder_path,
            current_scan_id,
            self.scanner_signals,
            active_scan_getter=self.get_active_scan_id
        )
        self.thread_pool.start(scanner_task)

    def on_batch_scanned(self, scan_id, batch):
        if self._is_closing or scan_id != self.scan_id:
            return

        self._sync_sort_state_from_settings()
        for item_data in batch:
            file_path = item_data["file_path"]
            self.loaded_items.append(item_data)
            self.items_by_path[file_path] = item_data

            already_pending = (
                self.workspace_grid.is_thumbnail_pending(file_path)
                if hasattr(self.workspace_grid, "is_thumbnail_pending")
                else self.workspace_grid.has_cached_pixmap(file_path)
            )
            if not already_pending:
                self.total_thumbs_to_generate += 1
                self.start_thumbnail_task(file_path, scan_id)

        self.sort_loaded_items_quietly()
        if len(self.loaded_items) == len(batch):
            self.needs_layout_update = False
            self.workspace_grid.set_cards(self.loaded_items)
        else:
            self._schedule_grid_repaint(relayout=True)

    def on_scan_complete(self, scan_id, total_count):
        if self._is_closing or scan_id != self.scan_id:
            return

        self._sync_sort_state_from_settings()
        if total_count == 0:
            self.workspace_grid.set_cards(self.loaded_items)
            self.set_status("No images found in folder")
        else:
            self.sort_loaded_items_quietly()
            self.needs_layout_update = False
            self.workspace_grid.set_cards(self.loaded_items)
            self.set_status(f"Ready \u2014 Total images in gallery: {total_count}")

            if self.current_watched_dir:
                self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)
                if len(self.folder_scan_cache) > self._MAX_FOLDER_CACHE:
                    self.folder_scan_cache.popitem(last=False)

            comfy_task = ComfyMetadataScannerTask(
                self.loaded_items,
                scan_id,
                self.comfy_signals,
                active_scan_getter=self.get_active_scan_id
            )
            self.thread_pool.start(comfy_task)

            if hasattr(self, 'thumb_db_cache') and self.thumb_db_cache is not None:
                self.thumb_db_cache.flush()

            if hasattr(self, 'initial_target_image') and self.initial_target_image:
                target = self.initial_target_image
                self.initial_target_image = None
                self.open_image_viewer(target)

    def on_comfy_batch_ready(self, scan_id, file_paths):
        if self._is_closing or scan_id != self.scan_id:
            return
        for fp in file_paths:
            item = self.items_by_path.get(fp)
            if item is not None:
                item["has_comfy"] = True
        self._schedule_grid_repaint(relayout=False)

    def on_thumbnail_ready(self, scan_id, file_path, thumb_path, qimage):
        if hasattr(self, "workspace_grid") and hasattr(self.workspace_grid, "unmark_thumbnail_in_flight"):
            self.workspace_grid.unmark_thumbnail_in_flight(file_path)
        if self._is_closing or scan_id != self.scan_id:
            return
        item = self.items_by_path.get(file_path)
        if item is not None:
            item["thumb_path"] = thumb_path
        self.workspace_grid.cache_thumbnail_image(file_path, qimage)
        self._schedule_grid_repaint(relayout=False)

        if self.thumbs_ready_count < self.total_thumbs_to_generate:
            self.thumbs_ready_count += 1

            if not hasattr(self, '_status_throttle_timer'):
                self._status_throttle_timer = QTimer(self)
                self._status_throttle_timer.setSingleShot(True)
                self._status_throttle_timer.timeout.connect(self._flush_thumbnail_status)

            if not self._status_throttle_timer.isActive():
                self._status_throttle_timer.start(150)

    def build_full_gallery_status(self):
        processed_count = min(self.thumbs_ready_count, self.total_thumbs_to_generate)
        left_to_gen = max(0, self.total_thumbs_to_generate - processed_count)

        cache_bytes = self.workspace_grid.get_cache_memory_usage()
        cache_size_str = format_file_size(cache_bytes)
        cache_count = len(self.workspace_grid.pixmap_cache)
        max_cache = self.workspace_grid._MAX_PIXMAP_CACHE_ITEMS

        disk_suffix = ""
        if not self._is_closing and self.folder_sync.is_disk_cache_enabled():
            settings = load_settings(SETTINGS_FILE)
            limit_str = self.folder_sync.get_disk_cache_limit_str(settings)
            disk_size_str = self.folder_sync.get_disk_cache_size_str()
            size_display = f"{disk_size_str} / {limit_str}" if limit_str else disk_size_str
            disk_suffix = (
                f" | Disk Cache: Processed: {processed_count} | Left: {left_to_gen} "
                f"| Disk Cache Size: {size_display}"
            )

        if left_to_gen > 0:
            base_msg = (
                f"Generating thumbnails... Ready: {processed_count} | Left: {left_to_gen} "
                f"| RAM Cache: {cache_count}/{max_cache} items ({cache_size_str}){disk_suffix}"
            )
        else:
            base_msg = (
                f"Ready — Total images in gallery: {len(self.loaded_items)} "
                f"| RAM Cache: {cache_count} items ({cache_size_str}){disk_suffix}"
            )

        child_prefix = getattr(self, "_persistent_child_scan_status", None)
        if child_prefix:
            return f"{child_prefix} | {base_msg}"
        return base_msg

    def _flush_thumbnail_status(self):
        if self._is_closing:
            return
        self.set_footer_status(self.build_full_gallery_status())

    def on_card_selected(self, file_path, display_name):
        if self._is_closing:
            return
        filename = os.path.basename(file_path)
        item_info = self.items_by_path.get(file_path)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        detailed_status = f"Selected: {filename}  |  Size: {size_str}"
        self.status_label.setText(f"Status: {detailed_status}")

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n\u2022 File: {filename}\n\u2022 Size: {size_str}\n\u2022 Resolution: Loading (BG)...\n\u2022 Path: {file_path}"
        )
        self.right_sidebar.exif_view.setHtml("<i>Loading metadata in background thread...</i>")
        if hasattr(self.right_sidebar, 'comfy_view'):
            self.right_sidebar.comfy_view.setPlainText("Loading ComfyUI workflow...")

        task = MetadataLoaderTask(
            file_path,
            filename,
            size_str,
            self.meta_signals,
            is_closing_getter=self._is_shutting_down,
        )
        self.thread_pool.start(task)

    def on_metadata_ready(self, file_path, filename, dims_str, exif_text, comfy_text):
        if self._is_closing:
            return
        if not self.workspace_grid.selected_file_paths or file_path not in self.workspace_grid.selected_file_paths:
            return

        item_info = self.items_by_path.get(file_path)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        self.right_sidebar.inspector_label.setText(
            f"Inspector Properties:\n\n\u2022 File: {filename}\n\u2022 Size: {size_str}\n\u2022 Resolution: {dims_str}\n\u2022 Path: {file_path}"
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
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & Qt.KeyboardModifier.AltModifier:
            self.set_status(f"Opening pinned viewer for: {os.path.basename(file_path)}")
            if not hasattr(self, 'detached_viewers'):
                self.detached_viewers = []

            detached_viewer = DetachedImageViewerWindow(file_path, self.loaded_items, self)
            detached_viewer.show()
            self.detached_viewers.append(detached_viewer)
            return

        self.set_status(f"Opening viewer for: {os.path.basename(file_path)}")
        self.hide()
        self.viewer_window = ImageViewerWindow(file_path, self)
        self.viewer_window.show()

    def open_current_folder_in_explorer(self):
        data = load_settings(SETTINGS_FILE)
        folder = data.get("last_folder", "")
        if folder and os.path.isdir(folder):
            try:
                system_name = platform.system()
                if system_name == "Windows":
                    os.startfile(folder)
                elif system_name == "Darwin":
                    subprocess.run(["open", folder])
                else:
                    subprocess.run(["xdg-open", folder])
                self.set_status(f"Opened folder in Explorer: {os.path.basename(folder)}")
            except Exception as e:
                self.set_status(f"Could not open explorer: {e}")
        else:
            self.set_status("No active folder to open.")

    def get_target_file_paths(self):
        if hasattr(self.workspace_grid, 'selected_file_paths') and self.workspace_grid.selected_file_paths:
            paths = list(self.workspace_grid.selected_file_paths)
        else:
            paths = [item["file_path"] for item in self.loaded_items]
        return [os.path.abspath(p) for p in paths if p and os.path.exists(p)]

    def set_sort_criteria(self, criteria):
        self.current_sort_criteria = str(criteria or "name").lower()
        save_settings(SETTINGS_FILE, {"sort_criteria": self.current_sort_criteria})
        self.apply_sorting()

    def toggle_sort_reverse(self, is_reversed):
        self.sort_reverse = bool(is_reversed)
        save_settings(SETTINGS_FILE, {"sort_reverse": self.sort_reverse})
        self.apply_sorting()

    def sort_loaded_items_quietly(self):
        self._sync_sort_state_from_settings()
        criteria = self.current_sort_criteria
        is_rev = bool(self.sort_reverse)
        if criteria == "name":
            self.loaded_items.sort(key=self._item_name_sort_key, reverse=is_rev)
        elif criteria == "date":
            self.loaded_items.sort(key=lambda x: x.get("mtime", 0), reverse=not is_rev)
        elif criteria == "size":
            self.loaded_items.sort(key=lambda x: x.get("size", 0), reverse=not is_rev)

    def apply_sorting(self):
        if not self.loaded_items:
            return

        self._sync_sort_state_from_settings()
        criteria = self.current_sort_criteria
        is_rev = bool(self.sort_reverse)

        if criteria == "name":
            self.loaded_items.sort(key=self._item_name_sort_key, reverse=is_rev)
            self.set_status(f"Sorted by Name ({'Z-A' if is_rev else 'A-Z'})")
        elif criteria == "date":
            self.loaded_items.sort(key=lambda x: x.get("mtime", 0), reverse=not is_rev)
            self.set_status(f"Sorted by Date ({'Oldest first' if is_rev else 'Newest first'})")
        elif criteria == "size":
            self.loaded_items.sort(key=lambda x: x.get("size", 0), reverse=not is_rev)
            self.set_status(f"Sorted by Size ({'Smallest first' if is_rev else 'Largest first'})")

        if self.current_watched_dir and self.current_watched_dir in self.folder_scan_cache:
            self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)

        self.workspace_grid.set_cards(self.loaded_items)

    def select_all_items(self):
        if self.loaded_items:
            self.workspace_grid.select_all()

    def deselect_all_items(self):
        self.workspace_grid.deselect_all()

    def inverse_selection(self):
        if self.loaded_items:
            self.workspace_grid.invert_selection()

    def on_selection_changed(self, selected_paths):
        if self._is_closing:
            return
        count = len(selected_paths)
        total = len(self.loaded_items)

        if count == 0:
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n\u2022 Folder Selected\n\u2022 Total Images: {total}\n\u2022 Selected: None"
            )
            self.right_sidebar.exif_view.setHtml(
                "<i>No EXIF metadata available.<br>Select an image to inspect camera details.</i>"
            )
            if hasattr(self.right_sidebar, 'comfy_view'):
                self.right_sidebar.comfy_view.setPlainText("Select a PNG with ComfyUI workflow to view data.")
            self.set_status(self.build_full_gallery_status())
        elif count == 1:
            self.on_card_selected(selected_paths[0], os.path.basename(selected_paths[0]))
        else:
            total_bytes = sum(
                self.items_by_path[p].get("size", 0) for p in selected_paths if p in self.items_by_path
            )
            size_str = format_file_size(total_bytes)
            self.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n\u2022 Multiple Items Selected\n\u2022 Selected: {count} of {total}\n\u2022 Combined Size: {size_str}"
            )
            self.right_sidebar.exif_view.setHtml(
                f"<i>{count} images selected.<br>Select a single image to inspect EXIF metadata.</i>"
            )
            if hasattr(self.right_sidebar, 'comfy_view'):
                self.right_sidebar.comfy_view.setPlainText("Multiple items selected.")
            self.set_status(f"Selected {count} of {total} items ({size_str})")

    def _schedule_keep_selected_in_view(self):
        if self._is_closing:
            return
        if hasattr(self, "_keep_selected_timer") and self._keep_selected_timer is not None:
            self._keep_selected_timer.start(100)

    def _keep_selected_thumbnail_in_view(self):
        if self._is_closing or not hasattr(self, "workspace_grid") or self.workspace_grid is None:
            return
        if not getattr(self.workspace_grid, "selected_file_paths", None):
            return
        self.workspace_grid.scroll_to_selected(only_if_out_of_view=True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_keep_selected_in_view()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            selected_paths = self.get_target_file_paths()
            if selected_paths:
                self.file_ops.delete_selected_files(selected_paths)
                event.accept()
                return

        super().keyPressEvent(event)

    def scroll_to_selected_thumbnail_in_grid(self):
        if hasattr(self, "workspace_grid") and self.workspace_grid:
            moved = self.workspace_grid.scroll_to_selected(only_if_out_of_view=True)
            if moved:
                self.set_status("Scrolled to selected image.")

    def center_current_folder_in_tree(self):
        if hasattr(self, "left_browser") and self.left_browser and hasattr(self.left_browser, "scroll_to_selected_folder"):
            active_folder = getattr(self, "current_watched_dir", None)
            moved_tree = self.left_browser.scroll_to_selected_folder(
                folder_path=active_folder,
                only_if_out_of_view=False,
                center_in_view=True
            )
            if moved_tree:
                self.set_status("Centered current folder in tree.")

    def open_selected_or_first_image_in_viewer(self):
        if not self.isActiveWindow() or self.isHidden():
            return

        target_path = None
        selected = getattr(self.workspace_grid, "selected_file_paths", None)
        if selected:
            selected_set = {os.path.abspath(p) for p in selected if p}
            for item in self.loaded_items:
                fp = item.get("file_path")
                if fp and os.path.abspath(fp) in selected_set and os.path.exists(fp):
                    target_path = fp
                    break
            if not target_path:
                for p in selected:
                    if p and os.path.exists(p):
                        target_path = p
                        break

        if not target_path:
            for item in self.loaded_items:
                fp = item.get("file_path")
                if fp and os.path.exists(fp):
                    target_path = fp
                    break

        if target_path:
            self.open_image_viewer(target_path)

    def jump_to_favorite_folder(self, folder_path):
        if folder_path and os.path.isdir(folder_path):
            abs_folder = os.path.abspath(folder_path)
            self.load_images_from_folder(abs_folder)

            if hasattr(self, 'left_browser') and self.left_browser:
                self.left_browser.set_selected_folder(abs_folder)
            self._center_folder_in_tree_for_path(abs_folder)
            for delay_ms in (60, 220, 500):
                QTimer.singleShot(delay_ms, lambda fp=abs_folder: self._center_folder_in_tree_for_path(fp))

            self.apply_sorting()
            self.set_status(f"Opened favorite: {os.path.basename(abs_folder)}")
        else:
            self.set_status(f"Favorite folder no longer exists: {folder_path}")

    def add_current_folder_to_favorites(self):
        settings = load_settings(SETTINGS_FILE)
        current_folder = settings.get("last_folder", "")
        if current_folder and os.path.isdir(current_folder):
            updated_favorites = add_folder_to_favorites(current_folder, SETTINGS_FILE)

            if hasattr(self.menu_bar, "update_favorites"):
                self.menu_bar.update_favorites(updated_favorites)
            else:
                self.menu_bar.favorites = updated_favorites
                self.menu_bar._populate_favorites_actions()
            folder_name = os.path.basename(os.path.normpath(current_folder))
            self.set_status(f"Added to favorites: {folder_name}")
        else:
            self.set_status("No active folder to add to favorites.")

    def open_settings_dialog(self):
        self.set_status("Opening Settings Window...")
        self._active_dialog = SettingsDialog(self)
        if self._active_dialog.exec() == QDialog.DialogCode.Accepted:
            updated_settings = load_settings(SETTINGS_FILE)
            if hasattr(self, "menu_bar") and self.menu_bar is not None:
                self.menu_bar.update_favorites(
                    updated_settings.get("favorites", []),
                    max_favorites=updated_settings.get("max_favorites", 15),
                )
            self._refresh_cached_jpeg_quality()
            self._apply_scroll_speed_setting()
            if hasattr(self.workspace_grid, 'update_cache_limit_from_settings'):
                self.workspace_grid.update_cache_limit_from_settings()
            self.apply_concurrency_settings()

            if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
                self.thumb_db_cache.purge_loose_jpeg_files()
            self.thread_pool.start(CacheCleanupTask(self.thumb_cache_dir))

            self.folder_scan_cache.clear()
            if self.current_watched_dir and os.path.isdir(self.current_watched_dir):
                self.folder_sync.sync_file_watcher_paths(self.current_watched_dir)
                self.folder_sync.perform_smooth_folder_sync()
            elif not getattr(self.workspace_grid, "selected_file_paths", None):
                self.set_footer_status(self.build_full_gallery_status())

        self.set_status("Ready")

    def show_about_dialog(self):
        self.set_status("Viewing About Dialog...")
        AboutDialog(self).exec()
        self.set_status("Ready")

    def closeEvent(self, event):
        self._is_closing = True
        self.scan_id += 1

        if hasattr(self, "folder_sync") and self.folder_sync is not None:
            self.folder_sync.prepare_for_close()

        if hasattr(self, "file_watcher") and self.file_watcher is not None:
            try:
                dirs = self.file_watcher.directories()
                if dirs:
                    self.file_watcher.removePaths(dirs)
            except Exception:
                pass

        for timer_attr in ("grid_repaint_timer", "_keep_selected_timer", "_status_throttle_timer"):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass

        settings = load_settings(SETTINGS_FILE)

        print("Closing the app, Wait for any active background thread tasks to finish cleanly")
        self.thread_pool.waitForDone()
        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None and not settings.get("purge_thumb_cache_on_exit", False):
            self.thumb_db_cache.flush()

        self.file_ops.flush_staged_trash_to_recycle_bin()

        if settings.get("purge_thumb_cache_on_exit", False):
            try:
                if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
                    self.thumb_db_cache.clear_all()
                    self.thumb_db_cache.close()
                if os.path.exists(self.thumb_cache_dir):
                    for fname in os.listdir(self.thumb_cache_dir):
                        fpath = os.path.join(self.thumb_cache_dir, fname)
                        try:
                            if os.path.isfile(fpath) or os.path.islink(fpath):
                                os.remove(fpath)
                            elif os.path.isdir(fpath):
                                shutil.rmtree(fpath)
                        except OSError:
                            pass
                    print("purge_thumb_cache_on_exit: DONE")
            except Exception as e:
                print(f"Error purging thumbnail cache: {e}")

        keep_alive = settings.get("keep_detached_viewers_alive", False)
        if hasattr(self, 'detached_viewers') and not keep_alive:
            for viewer in list(self.detached_viewers):
                if viewer:
                    try:
                        if not viewer.isHidden():
                            viewer.close()
                    except RuntimeError:
                        pass

        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
            self.thumb_db_cache.close()
        event.accept()

        if hasattr(self, "resize_handle") and self.resize_handle is not None:
            try:
                self.resize_handle.hide()
                self.resize_handle.setUpdatesEnabled(False)
            except Exception:
                pass