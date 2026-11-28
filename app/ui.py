import os
import platform
import re
import html
import shutil
import subprocess
import tempfile
import threading
import json
import struct
import zlib
from collections import OrderedDict
from PIL import Image

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QGraphicsDropShadowEffect, QFileDialog, QScrollArea, QSplitter, QDialog,
    QTextEdit
)
from PySide6.QtCore import Qt, QEvent, QThreadPool, QTimer, QRunnable, Signal, QObject, QFileSystemWatcher
from PySide6.QtGui import QColor, QImageReader, QGuiApplication, QShortcut, QKeySequence

from .img_viewer import ImageViewerWindow, DetachedImageViewerWindow
from .dialogs import AboutDialog, SettingsDialog, ShortcutsDialog
from .workers import (
    ThumbnailSignalEmitter, ThumbnailLoaderTask, FolderScannerSignals, FolderScannerTask,
    ComfyMetadataScannerSignals, ComfyMetadataScannerTask, CacheCleanupTask, AnimatedGifScannerSignals,
    AnimatedGifScannerTask, get_gif_animation_info
)
from .widgets import WindowDragHandler, TitleBar, ResponsiveGridWidget, ResizeHandleWidget
from .components import SidebarPanel, DirectoryBrowserPanel, TopMenuBar
from .helpers import (
    DEFAULT_SETTINGS_FILE, load_settings, save_settings, format_file_size, extract_exif_metadata,
    add_folder_to_favorites
)
from .file_ops import FileOperationsController
from .batch_tools import BatchToolsController
from .folder_sync import FolderSyncController

try:
    from .sqlite_thumb_cache import ThumbnailSQLiteCache
except ImportError:
    from sqlite_thumb_cache import ThumbnailSQLiteCache

SETTINGS_FILE = DEFAULT_SETTINGS_FILE

# Serialize Pillow image/PNG text/EXIF decoding across worker threads on Windows
# to prevent native decoder / plugin race conditions during rapid actions.
_PIL_METADATA_LOCK = threading.RLock()

try:
    from .crash_reporter import install_crash_reporter
except ImportError:
    from crash_reporter import install_crash_reporter

_CRASH_REPORTER = install_crash_reporter(app_name="WiggleView")


try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:
    _shiboken_is_valid = None


def _qt_alive(obj) -> bool:
    if obj is None:
        return False
    if _shiboken_is_valid is not None:
        try:
            return bool(_shiboken_is_valid(obj))
        except Exception:
            return False
    return True


class MetadataWorkerSignals(QObject):
    metadata_ready = Signal(int, str, str, str, str, str, str)


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

        settings_init = load_settings(SETTINGS_FILE)
        self._cached_settings = dict(settings_init) if isinstance(settings_init, dict) else {}

        self.apply_concurrency_settings(self._cached_settings)
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
        self.meta_signals.metadata_ready.connect(self.on_metadata_ready, Qt.ConnectionType.QueuedConnection)

        encrypt_thumbs = bool(self._cached_settings.get("encrypt_thumbnail_cache", True))
        self.debug_mode = bool(self._cached_settings.get("debug_mode", False))
        self._cached_encrypt_thumbnail_cache = encrypt_thumbs

        self.thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")
        os.makedirs(self.thumb_cache_dir, exist_ok=True)
        self.thumb_db_cache = ThumbnailSQLiteCache.get_shared(
            self.thumb_cache_dir,
            encrypt_enabled=encrypt_thumbs,
        )

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

        self.gif_signals = AnimatedGifScannerSignals()
        self.gif_signals.animated_gif_batch_ready.connect(self.on_animated_gif_batch_ready)

        self.folder_scan_cache = OrderedDict()
        self._persistent_child_scan_status = None
        self._MAX_FOLDER_CACHE = 10
        self._MAX_WATCHED_DIRS = 256
        self._MAX_CHILD_SCAN_THUMB_TASKS = 512

        save_settings(SETTINGS_FILE, {"include_subfolders": False})
        self._cached_settings["include_subfolders"] = False

        self._refresh_cached_settings(self._cached_settings)
        self._setup_ui(self._cached_settings)
        self._sync_maximized_frame()

        if self.debug_mode:
            self.startup_settings_print(self._cached_settings)

        if initial_image_path and os.path.exists(initial_image_path):
            abs_path = os.path.abspath(initial_image_path)
            if os.path.isdir(abs_path):
                folder_dir = abs_path
                self.initial_target_image = None
            else:
                folder_dir = os.path.dirname(abs_path)
                self.initial_target_image = abs_path

            self.current_watched_dir = folder_dir
            self.left_browser.set_selected_folder(folder_dir)
            self.path_label.setText(f"{folder_dir}")
            self.save_last_folder(folder_dir)
            QTimer.singleShot(15, lambda fp=folder_dir: self.folder_sync.sync_file_watcher_paths(fp))

            self.scan_id += 1
            scanner_task = FolderScannerTask(
                folder_dir,
                self.scan_id,
                self.scanner_signals,
                active_scan_getter=self.get_active_scan_id,
                settings_file=SETTINGS_FILE,
            )
            self.thread_pool.start(scanner_task)
        else:
            self.load_last_folder(self._cached_settings)

        self.crash_log_path = getattr(_CRASH_REPORTER, "log_path", None)
        if self.crash_log_path and self.debug_mode:
            print(f"Crash report log: {self.crash_log_path}")
        QTimer.singleShot(120, self.center_current_folder_in_tree)

    def _is_shutting_down(self):
        return bool(self._is_closing)

    def _refresh_cached_settings(self, settings=None):
        data = dict(settings) if isinstance(settings, dict) else load_settings(SETTINGS_FILE)
        if not isinstance(data, dict):
            data = {}
        self._cached_settings = data

        raw_q = data.get("thumbnail_jpeg_quality", 90)
        try:
            quality = int(raw_q)
        except (TypeError, ValueError):
            quality = 90
        self._cached_jpeg_quality = max(1, min(100, quality))

        encrypt_thumbs = bool(data.get("encrypt_thumbnail_cache", True))
        self._cached_encrypt_thumbnail_cache = encrypt_thumbs
        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
            if hasattr(self.thumb_db_cache, "set_encryption_enabled"):
                self.thumb_db_cache.set_encryption_enabled(encrypt_thumbs)

        criteria = data.get("sort_criteria")
        if isinstance(criteria, str) and criteria.strip():
            self.current_sort_criteria = criteria.strip().lower()
        if "sort_reverse" in data:
            self.sort_reverse = bool(data["sort_reverse"])

        self.debug_mode = bool(data.get("debug_mode", False))
        return data

    def _refresh_cached_jpeg_quality(self):
        self._refresh_cached_settings()

    def _refresh_cached_encrypt_thumbnail_cache(self):
        self._refresh_cached_settings()

    def start_thumbnail_task(self, file_path, scan_id):
        if self._is_closing or not _qt_alive(self):
            return
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid) and hasattr(grid, "mark_thumbnail_in_flight"):
            grid.mark_thumbnail_in_flight(file_path)
        quality = getattr(self, "_cached_jpeg_quality", 90)
        encrypt_enabled = getattr(self, "_cached_encrypt_thumbnail_cache", True)
        task = ThumbnailLoaderTask(
            file_path,
            getattr(self, "thumb_cache_dir", None),
            self.CARD_IMAGE_SIZE,
            self.thumbnail_signals,
            scan_id=scan_id,
            active_scan_getter=self.get_active_scan_id,
            jpeg_quality=quality,
            encrypt_thumbnail_cache=encrypt_enabled,
            db_cache=getattr(self, "thumb_db_cache", None),
            is_closing_getter=self._is_shutting_down,
        )
        self.thread_pool.start(task)

    def get_active_scan_id(self):
        if self._is_closing:
            return -1
        return self.scan_id

    def _schedule_grid_repaint(self, relayout=False):
        if self._is_closing or not _qt_alive(self):
            return
        if relayout:
            self.needs_layout_update = True
        timer = getattr(self, "grid_repaint_timer", None)
        if _qt_alive(timer) and not timer.isActive():
            timer.start()

    def _flush_grid_repaint(self):
        if self._is_closing or not _qt_alive(self):
            return
        grid = getattr(self, "workspace_grid", None)
        if not _qt_alive(grid):
            return
        if self.needs_layout_update:
            self.needs_layout_update = False
            self.sort_loaded_items_quietly()
            grid.set_cards(self.loaded_items)
        else:
            grid.update()

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

    def _item_type_sort_key(self, item):
        """Deterministic file-type sort key grouping by extension/format, then name."""
        fp = os.path.abspath(item.get("file_path", "")) if item.get("file_path") else ""
        raw_ext = item.get("ext") or item.get("extension") or os.path.splitext(fp)[1] or ""
        ext_norm = str(raw_ext).strip().lower().lstrip(".")
        return (ext_norm, self._item_name_sort_key(item))

    def _sync_sort_state_from_settings(self):
        """Synchronizes active sort criteria/reverse from in-memory cached settings."""
        settings_data = getattr(self, "_cached_settings", None) or {}
        criteria = settings_data.get("sort_criteria")
        if isinstance(criteria, str) and criteria.strip():
            self.current_sort_criteria = criteria.strip().lower()
        if "sort_reverse" in settings_data:
            self.sort_reverse = bool(settings_data["sort_reverse"])

    def _setup_ui(self, settings_data=None):
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

        if not isinstance(settings_data, dict):
            settings_data = getattr(self, "_cached_settings", None) or {}
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
        self.menu_bar.shortcuts_requested.connect(self.show_shortcuts_dialog)
        self.menu_bar.favorite_selected.connect(self.jump_to_favorite_folder)
        self.menu_bar.add_favorite_requested.connect(self.add_current_folder_to_favorites)
        self.menu_bar.rename_requested.connect(
            lambda: self.file_ops.rename_selected_files(self.get_target_file_paths())
        )
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
        self._apply_scroll_speed_setting(settings_data)
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

        self._main_layout = main_layout
        self._container_shadow = shadow
        self._normal_container_style = self.container.styleSheet()
        self._maximized_container_style = """
            QFrame {
                background-color: #0d1321;
                border: none;
                border-radius: 0px;
            }
        """

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

    def _apply_scroll_speed_setting(self, settings=None):
        data = settings if isinstance(settings, dict) else (getattr(self, "_cached_settings", None) or {})
        try:
            scroll_speed = int(data.get("scroll_speed", 15))
        except (TypeError, ValueError):
            scroll_speed = 15
        scroll_speed = max(1, min(29, scroll_speed))
        if hasattr(self, "scroll_area") and self.scroll_area is not None:
            self.scroll_area.verticalScrollBar().setSingleStep(scroll_speed)

    def apply_concurrency_settings(self, settings=None):
        data = settings if isinstance(settings, dict) else (getattr(self, "_cached_settings", None) or {})
        concurrency = data.get("thread_pool_concurrency", "Auto (Max Cores)")

        max_threads = QThreadPool.globalInstance().maxThreadCount()
        if "2 Threads" in concurrency:
            max_threads = 2
        elif "4 Threads" in concurrency:
            max_threads = 4
        elif "Auto" in concurrency:
            import multiprocessing
            max_threads = max(2, multiprocessing.cpu_count())

        QThreadPool.globalInstance().setMaxThreadCount(max_threads)

    def startup_settings_print(self, settings=None):
        data = settings if isinstance(settings, dict) else (getattr(self, "_cached_settings", None) or {})
        print(f"thumbnail_jpeg_quality: {data.get('thumbnail_jpeg_quality', None)}")
        print(f"encrypt_thumbnail_cache: {data.get('encrypt_thumbnail_cache', None)}")

    def _center_folder_in_tree_for_path(self, folder_path):
        if self._is_closing or not folder_path or not os.path.isdir(folder_path):
            return
        if hasattr(self, "left_browser") and self.left_browser and hasattr(self.left_browser, "scroll_to_selected_folder"):
            self.left_browser.scroll_to_selected_folder(
                folder_path=os.path.abspath(folder_path),
                only_if_out_of_view=False,
                center_in_view=True
            )

    def load_last_folder(self, settings=None):
        data = settings if isinstance(settings, dict) else (getattr(self, "_cached_settings", None) or {})
        folder = data.get("last_folder", "")

        if not folder or not os.path.isdir(folder):
            pictures_dir = os.path.join(os.path.expanduser("~"), "Pictures")
            folder = pictures_dir if os.path.isdir(pictures_dir) else os.path.expanduser("~")

        if folder and os.path.isdir(folder):
            abs_folder = os.path.abspath(folder)
            print(f"abs_folder: {abs_folder}")
            self.left_browser.set_selected_folder(abs_folder)
            self.load_images_from_folder(abs_folder)
            self._center_folder_in_tree_for_path(abs_folder)
            for delay_ms in (60, 220, 500):
                QTimer.singleShot(delay_ms, lambda fp=abs_folder: self._center_folder_in_tree_for_path(fp))

    def save_last_folder(self, folder_path):
        self._cached_settings["last_folder"] = folder_path
        save_settings(SETTINGS_FILE, {"last_folder": folder_path})

    def set_footer_status(self, message):
        if self._is_closing or not _qt_alive(self):
            return
        if _qt_alive(getattr(self, "status_label", None)):
            self.status_label.setText(f"Status: {message}")

    def set_status(self, message, log=True):
        if self._is_closing or not _qt_alive(self):
            return
        child_prefix = getattr(self, "_persistent_child_scan_status", None)
        if child_prefix and not message.startswith("Subfolder scan:"):
            footer_msg = f"{child_prefix} | {message}"
        else:
            footer_msg = message
        if _qt_alive(getattr(self, "status_label", None)):
            self.status_label.setText(f"Status: {footer_msg}")
        sidebar = getattr(self, "right_sidebar", None)
        if log and _qt_alive(sidebar) and hasattr(sidebar, 'log_text'):
            log_widget = sidebar.log_text
            if _qt_alive(log_widget):
                if hasattr(log_widget, "appendPlainText"):
                    log_widget.appendPlainText(f"\u2022 {message}")
                else:
                    current_text = log_widget.toPlainText()
                    prefix = f"{current_text}\n" if current_text else ""
                    log_widget.setPlainText(f"{prefix}\u2022 {message}")
                vbar = log_widget.verticalScrollBar()
                if _qt_alive(vbar):
                    vbar.setValue(vbar.maximum())

    def open_folder_dialog(self):
        data = getattr(self, "_cached_settings", None) or {}
        start_dir = data.get("last_folder", "")
        if not start_dir or not os.path.isdir(start_dir):
            start_dir = os.path.expanduser("~")

        folder_path = QFileDialog.getExistingDirectory(
            self, "Select Image Folder", start_dir, QFileDialog.Option.ShowDirsOnly
        )
        if folder_path:
            self.load_images_from_folder(folder_path)

    def load_images_from_folder(self, folder_path):
        if self._is_closing or not _qt_alive(self) or not folder_path or not os.path.exists(folder_path):
            return

        self._sync_sort_state_from_settings()
        self.current_watched_dir = folder_path
        self._persistent_child_scan_status = None
        if _qt_alive(getattr(self, "left_browser", None)):
            self.left_browser.set_selected_folder(folder_path)
        if _qt_alive(getattr(self, "path_label", None)):
            self.path_label.setText(f"{folder_path}")
        QTimer.singleShot(0, lambda fp=folder_path: self.save_last_folder(fp))
        if hasattr(self, "file_watcher") and self.file_watcher is not None and folder_path not in self.file_watcher.directories():
            QTimer.singleShot(15, lambda fp=folder_path: self.folder_sync.sync_file_watcher_paths(fp))

        self.scan_id += 1
        current_scan_id = self.scan_id
        self.total_thumbs_to_generate = 0
        self.thumbs_ready_count = 0
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid) and hasattr(grid, "clear_in_flight_thumbnails"):
            grid.clear_in_flight_thumbnails()

        if folder_path in self.folder_scan_cache:
            self.folder_scan_cache.move_to_end(folder_path)
            self.loaded_items = list(self.folder_scan_cache[folder_path])
            self.rebuild_path_index()
            self.sort_loaded_items_quietly()
            if _qt_alive(grid):
                grid.set_cards(self.loaded_items)

            for item in self.loaded_items:
                fp = item["file_path"]
                already_pending = (
                    grid.is_thumbnail_pending(fp)
                    if (_qt_alive(grid) and hasattr(grid, "is_thumbnail_pending"))
                    else (grid.has_cached_pixmap(fp) if _qt_alive(grid) else False)
                )
                if not already_pending:
                    self.total_thumbs_to_generate += 1
                    self.start_thumbnail_task(fp, current_scan_id)

            self.set_status(f"Ready (Cached) \u2014 Total images: {len(self.loaded_items)}")
            QTimer.singleShot(30, self.folder_sync.perform_smooth_folder_sync)
            return

        self.loaded_items.clear()
        self.items_by_path.clear()
        if _qt_alive(grid):
            grid.set_cards(self.loaded_items)
        if hasattr(grid, 'clear_cache_if_needed'):
            grid.clear_cache_if_needed(max_allowed=3000)

        self.set_status(f"Scanning folder: {os.path.basename(folder_path)}")
        sidebar = getattr(self, "right_sidebar", None)
        if _qt_alive(sidebar):
            inspector = getattr(sidebar, "inspector_label", None)
            if _qt_alive(inspector):
                inspector.setText(
                    f"Inspector Properties:\n\n\u2022 Folder: {os.path.basename(folder_path)}\n\u2022 Scanning..."
                )

        scanner_task = FolderScannerTask(
            folder_path,
            current_scan_id,
            self.scanner_signals,
            active_scan_getter=self.get_active_scan_id,
            settings_file=SETTINGS_FILE,
        )
        self.thread_pool.start(scanner_task)

    def on_batch_scanned(self, scan_id, batch):
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return

        grid = getattr(self, "workspace_grid", None)
        self._sync_sort_state_from_settings()
        for item_data in batch:
            file_path = item_data["file_path"]
            self.loaded_items.append(item_data)
            self.items_by_path[file_path] = item_data

            already_pending = (
                grid.is_thumbnail_pending(file_path)
                if (_qt_alive(grid) and hasattr(grid, "is_thumbnail_pending"))
                else (grid.has_cached_pixmap(file_path) if _qt_alive(grid) else False)
            )
            if not already_pending:
                self.total_thumbs_to_generate += 1
                self.start_thumbnail_task(file_path, scan_id)

        self.sort_loaded_items_quietly()
        if len(self.loaded_items) == len(batch):
            self.needs_layout_update = False
            if _qt_alive(grid):
                grid.set_cards(self.loaded_items)
        else:
            self._schedule_grid_repaint(relayout=True)

    def on_scan_complete(self, scan_id, total_count):
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return

        grid = getattr(self, "workspace_grid", None)
        self._sync_sort_state_from_settings()
        if total_count == 0:
            if _qt_alive(grid):
                grid.set_cards(self.loaded_items)
            self.set_status("No images found in folder")
        else:
            self.sort_loaded_items_quietly()
            self.needs_layout_update = False
            if _qt_alive(grid):
                grid.set_cards(self.loaded_items)
            self.set_status(f"Ready \u2014 Total images in gallery: {total_count}")

        if self.current_watched_dir:
            self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)
            if len(self.folder_scan_cache) > self._MAX_FOLDER_CACHE:
                self.folder_scan_cache.popitem(last=False)

        items_snapshot = list(self.loaded_items)

        # Pass a list snapshot in on_scan_complete
        comfy_task = ComfyMetadataScannerTask(
            items_snapshot,
            scan_id,
            self.comfy_signals,
            active_scan_getter=self.get_active_scan_id,
        )
        self.thread_pool.start(comfy_task)

        gif_task = AnimatedGifScannerTask(
            items_snapshot,
            scan_id,
            self.gif_signals,
            active_scan_getter=self.get_active_scan_id,
            is_closing_getter=self._is_shutting_down,
        )
        self.thread_pool.start(gif_task)

        if hasattr(self, 'thumb_db_cache') and self.thumb_db_cache is not None:
            self.thumb_db_cache.flush()

        if hasattr(self, "initial_target_image") and self.initial_target_image:
            target = self.initial_target_image
            self.initial_target_image = None
            self.open_image_viewer(target)
        else:
            self._sync_viewer_items_order()
            self._reload_active_viewer_if_needed()

    def on_comfy_batch_ready(self, scan_id, file_paths):
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return
        for fp in file_paths:
            item = self.items_by_path.get(fp)
            if item is not None:
                item["has_comfy"] = True
        self._schedule_grid_repaint(relayout=False)

    def on_thumbnail_ready(self, scan_id, file_path, thumb_path, qimage):
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid) and hasattr(grid, "unmark_thumbnail_in_flight"):
            grid.unmark_thumbnail_in_flight(file_path)
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return
        item = self.items_by_path.get(file_path)
        if item is not None:
            item["thumb_path"] = thumb_path
        if _qt_alive(grid):
            grid.cache_thumbnail_image(file_path, qimage)
        self._schedule_grid_repaint(relayout=False)

        if self.thumbs_ready_count < self.total_thumbs_to_generate:
            self.thumbs_ready_count += 1

        if not hasattr(self, '_status_throttle_timer'):
            self._status_throttle_timer = QTimer(self)
            self._status_throttle_timer.setSingleShot(True)
            self._status_throttle_timer.timeout.connect(self._flush_thumbnail_status)

        if _qt_alive(self._status_throttle_timer) and not self._status_throttle_timer.isActive():
            self._status_throttle_timer.start(150)

    def on_animated_gif_batch_ready(self, scan_id, file_paths):
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return
        updated = False
        for fp in file_paths:
            item = self.items_by_path.get(fp)
            if item is not None and not item.get("is_animated_gif"):
                item["is_animated_gif"] = True
                updated = True
        if updated:
            self._schedule_grid_repaint(relayout=False)

    def build_full_gallery_status(self):
        processed_count = min(self.thumbs_ready_count, self.total_thumbs_to_generate)
        left_to_gen = max(0, self.total_thumbs_to_generate - processed_count)

        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid):
            cache_bytes = grid.get_cache_memory_usage()
            cache_size_str = format_file_size(cache_bytes)
            cache_count = len(grid.pixmap_cache)
            max_cache = grid._MAX_PIXMAP_CACHE_ITEMS
        else:
            cache_size_str = "0 B"
            cache_count = 0
            max_cache = 0

        disk_suffix = ""
        if not self._is_closing and self.folder_sync.is_disk_cache_enabled():
            cached_settings = getattr(self, "_cached_settings", None) or {}
            limit_str = self.folder_sync.get_disk_cache_limit_str(cached_settings)
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
                f"Ready \u2014 Total images in gallery: {len(self.loaded_items)} "
                f"| RAM Cache: {cache_count} items ({cache_size_str}){disk_suffix}"
            )

        child_prefix = getattr(self, "_persistent_child_scan_status", None)
        if child_prefix:
            return f"{child_prefix} | {base_msg}"
        return base_msg

    def _flush_thumbnail_status(self):
        if self._is_closing or not _qt_alive(self):
            return
        self.set_footer_status(self.build_full_gallery_status())

    def on_card_selected(self, file_path, display_name):
        if self._is_closing or not _qt_alive(self):
            return
        filename = os.path.basename(file_path)
        item_info = self.items_by_path.get(file_path)
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        detailed_status = f"Selected: {filename} | Size: {size_str}"
        if _qt_alive(getattr(self, "status_label", None)):
            self.status_label.setText(f"Status: {detailed_status}")

        gif_loading_line = (
            "\n\u2022 Animation: Loading (BG)..."
            if str(file_path).lower().endswith(".gif")
            else ""
        )

        sidebar = getattr(self, "right_sidebar", None)
        if _qt_alive(sidebar):
            inspector = getattr(sidebar, "inspector_label", None)
            if _qt_alive(inspector):
                inspector.setText(
                    f"Inspector Properties:\n\n\u2022 File: {filename}\n\u2022 Size: {size_str}\n\u2022 Resolution: Loading (BG)...{gif_loading_line}\n\u2022 Path: {file_path}"
                )
            exif_view = getattr(sidebar, "exif_view", None)
            if _qt_alive(exif_view):
                exif_view.setHtml("<i>Loading metadata in background thread...</i>")
            comfy_view = getattr(sidebar, "comfy_view", None)
            if _qt_alive(comfy_view):
                comfy_view.setPlainText("Loading ComfyUI workflow...")

        self._dispatch_metadata_task(file_path, filename, size_str)

    def _dispatch_metadata_task(self, file_path, filename, size_str):
        if self._is_closing or not _qt_alive(self):
            return
        task = _MetadataTask(
            file_path,
            filename,
            size_str,
            self.meta_signals,
            scan_id=self.scan_id,
            active_scan_getter=self.get_active_scan_id,
            is_closing_getter=self._is_shutting_down,
        )
        self.thread_pool.start(task)

    def on_metadata_ready(self, scan_id, file_path, filename, dims_str, exif_text, comfy_text, gif_info_text=""):
        if self._is_closing or not _qt_alive(self) or scan_id != self.scan_id:
            return
        grid = getattr(self, "workspace_grid", None)
        if not _qt_alive(grid):
            return
        selected = getattr(grid, "selected_file_paths", None) or ()
        if file_path not in selected:
            return

        item_info = self.items_by_path.get(file_path) if isinstance(self.items_by_path, dict) else None
        size_str = format_file_size(item_info.get("size", 0)) if item_info else "Unknown size"

        sidebar = getattr(self, "right_sidebar", None)
        if _qt_alive(sidebar):
            inspector = getattr(sidebar, "inspector_label", None)
            if _qt_alive(inspector):
                inspector.setText(
                    f"Inspector Properties:\n\n"
                    f"\u2022 File: {filename}\n"
                    f"\u2022 Size: {size_str}\n"
                    f"\u2022 Resolution: {dims_str}"
                    f"{gif_info_text or ''}\n"
                    f"\u2022 Path: {file_path}"
                )

            exif_view = getattr(sidebar, "exif_view", None)
            if _qt_alive(exif_view):
                safe_exif = exif_text if isinstance(exif_text, str) else html.escape(str(exif_text))
                exif_view.setHtml(safe_exif)

            comfy_view = getattr(sidebar, "comfy_view", None)
            if _qt_alive(comfy_view):
                if comfy_text:
                    comfy_view.setPlainText(comfy_text)
                elif str(file_path).lower().endswith(".png"):
                    comfy_view.setPlainText("No ComfyUI workflow or prompt found in this PNG.")
                else:
                    comfy_view.setPlainText("ComfyUI workflows are only stored in PNG metadata.")

        status_lbl = getattr(self, "status_label", None)
        if _qt_alive(status_lbl):
            status_lbl.setText(
                f"Status: Selected: {filename} | Size: {size_str} | Dimensions: {dims_str}"
            )

    def _ensure_item_in_gallery(self, file_path):
        """Ensures a valid image path is registered in loaded_items/items_by_path even mid-scan."""
        if not file_path or not os.path.isfile(file_path):
            return None
        abs_path = os.path.abspath(file_path)
        existing = self.items_by_path.get(abs_path) or self.items_by_path.get(file_path)
        if existing is not None:
            return existing

        try:
            st = os.stat(abs_path)
            size_val = st.st_size
            mtime_val = st.st_mtime
        except OSError:
            size_val = 0
            mtime_val = 0

        fname = os.path.basename(abs_path)
        entry = {
            "file_path": abs_path,
            "display_name": (fname[:16] + '..') if len(fname) > 18 else fname,
            "thumb_path": None,
            "size": size_val,
            "mtime": mtime_val,
            "filename": fname.lower(),
            "dirname_lower": os.path.dirname(abs_path).lower(),
            "has_comfy": False,
            "is_animated_gif": False,
        }
        self.loaded_items.append(entry)
        self.items_by_path[abs_path] = entry
        return entry

    def _reload_active_viewer_if_needed(self, target_path=None):
        """Reloads the active image viewer once a folder scan finishes if it was waiting."""
        viewer = getattr(self, "viewer_window", None)
        if viewer is None or not _qt_alive(viewer):
            return
        try:
            if viewer.isHidden():
                return
        except Exception:
            return

        current_fp = (
            target_path
            or getattr(viewer, "file_path", None)
            or getattr(viewer, "current_image_path", None)
            or getattr(viewer, "image_path", None)
        )
        if not current_fp or not os.path.isfile(current_fp):
            return

        self._ensure_item_in_gallery(current_fp)
        for method_name in ("reload_image", "load_image", "refresh_image", "set_image_path", "open_image"):
            fn = getattr(viewer, method_name, None)
            if callable(fn):
                try:
                    fn(current_fp)
                    return
                except TypeError:
                    try:
                        fn()
                        return
                    except Exception:
                        pass
                except Exception:
                    pass

    def open_image_viewer(self, file_path):
        if self._is_closing or not _qt_alive(self):
            return
        if file_path and os.path.isfile(file_path):
            file_path = os.path.abspath(file_path)
            self._ensure_item_in_gallery(file_path)

        # If a folder scan is still running, prioritize thumbnail generation for this image
        # and ensure it is present in loaded_items immediately.
        if file_path and os.path.isfile(file_path):
            self.start_thumbnail_task(file_path, self.scan_id)

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
        data = getattr(self, "_cached_settings", None) or {}
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
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid) and hasattr(grid, 'selected_file_paths') and grid.selected_file_paths:
            paths = list(grid.selected_file_paths)
        else:
            paths = [item["file_path"] for item in self.loaded_items]
        return [os.path.abspath(p) for p in paths if p and os.path.exists(p)]

    def set_sort_criteria(self, criteria):
        self.current_sort_criteria = str(criteria or "name").lower()
        self._cached_settings["sort_criteria"] = self.current_sort_criteria
        save_settings(SETTINGS_FILE, {"sort_criteria": self.current_sort_criteria})
        self.apply_sorting()

    def toggle_sort_reverse(self, is_reversed):
        self.sort_reverse = bool(is_reversed)
        self._cached_settings["sort_reverse"] = self.sort_reverse
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
        elif criteria == "type":
            self.loaded_items.sort(key=self._item_type_sort_key, reverse=is_rev)

    def _sync_viewer_items_order(self):
        """Keeps any open viewer's item list and current_index aligned with self.loaded_items."""
        viewer = getattr(self, "viewer_window", None)
        detached = getattr(self, "detached_viewers", None) or []
        if viewer is None and not detached:
            return

        if viewer is not None and _qt_alive(viewer) and hasattr(viewer, "sync_items_order"):
            try:
                viewer.sync_items_order(self.loaded_items)
            except Exception:
                pass

        for dv in list(detached):
            if dv is not None and _qt_alive(dv) and hasattr(dv, "sync_items_order"):
                try:
                    dv.sync_items_order(self.loaded_items)
                except Exception:
                    pass

    def apply_sorting(self):
        if self._is_closing or not _qt_alive(self) or not self.loaded_items:
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
        elif criteria == "type":
            self.loaded_items.sort(key=self._item_type_sort_key, reverse=is_rev)
            self.set_status(f"Sorted by File Type ({'Z-A' if is_rev else 'A-Z'})")

        if self.current_watched_dir and self.current_watched_dir in self.folder_scan_cache:
            self.folder_scan_cache[self.current_watched_dir] = list(self.loaded_items)

        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid):
            grid.set_cards(self.loaded_items)

        self._sync_viewer_items_order()

    def select_all_items(self):
        grid = getattr(self, "workspace_grid", None)
        if self.loaded_items and _qt_alive(grid):
            grid.select_all()

    def deselect_all_items(self):
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid):
            grid.deselect_all()

    def inverse_selection(self):
        grid = getattr(self, "workspace_grid", None)
        if self.loaded_items and _qt_alive(grid):
            grid.invert_selection()

    def on_selection_changed(self, selected_paths):
        if self._is_closing or not _qt_alive(self):
            return
        safe_paths = list(selected_paths or ())
        count = len(safe_paths)
        total = len(self.loaded_items)
        sidebar = getattr(self, "right_sidebar", None)

        if count == 0:
            if _qt_alive(sidebar):
                inspector = getattr(sidebar, "inspector_label", None)
                if _qt_alive(inspector):
                    inspector.setText(
                        f"Inspector Properties:\n\n\u2022 Folder Selected\n\u2022 Total Images: {total}\n\u2022 Selected: None"
                    )
                exif_view = getattr(sidebar, "exif_view", None)
                if _qt_alive(exif_view):
                    exif_view.setHtml(
                        "<i>No EXIF metadata available.<br>Select an image to inspect camera details.</i>"
                    )
                comfy_view = getattr(sidebar, "comfy_view", None)
                if _qt_alive(comfy_view):
                    comfy_view.setPlainText("Select a PNG with ComfyUI workflow to view data.")
            self.set_status(self.build_full_gallery_status())
        elif count == 1:
            self.on_card_selected(safe_paths[0], os.path.basename(safe_paths[0]))
        else:
            total_bytes = sum(
                self.items_by_path[p].get("size", 0) for p in safe_paths if p in self.items_by_path
            )
            size_str = format_file_size(total_bytes)
            if _qt_alive(sidebar):
                inspector = getattr(sidebar, "inspector_label", None)
                if _qt_alive(inspector):
                    inspector.setText(
                        f"Inspector Properties:\n\n\u2022 Multiple Items Selected\n\u2022 Selected: {count} of {total}\n\u2022 Combined Size: {size_str}"
                    )
                exif_view = getattr(sidebar, "exif_view", None)
                if _qt_alive(exif_view):
                    exif_view.setHtml(
                        f"<i>{count} images selected.<br>Select a single image to inspect EXIF metadata.</i>"
                    )
                comfy_view = getattr(sidebar, "comfy_view", None)
                if _qt_alive(comfy_view):
                    comfy_view.setPlainText("Multiple items selected.")
            self.set_status(f"Selected {count} of {total} items ({size_str})")

    def _schedule_keep_selected_in_view(self):
        if self._is_closing or not _qt_alive(self):
            return
        timer = getattr(self, "_keep_selected_timer", None)
        if _qt_alive(timer):
            timer.start(100)

    def _keep_selected_thumbnail_in_view(self):
        if self._is_closing or not _qt_alive(self):
            return
        grid = getattr(self, "workspace_grid", None)
        if not _qt_alive(grid):
            return
        if not getattr(grid, "selected_file_paths", None):
            return
        grid.scroll_to_selected(only_if_out_of_view=True)

    def _sync_maximized_frame(self):
        is_max = self.isMaximized()
        if hasattr(self, "_main_layout") and self._main_layout is not None:
            m = 0 if is_max else 12
            self._main_layout.setContentsMargins(m, m, m, m)

        if hasattr(self, "_container_shadow") and self._container_shadow is not None:
            self._container_shadow.setEnabled(not is_max)

        if hasattr(self, "container") and self.container is not None:
            if is_max:
                self.container.setStyleSheet(self._maximized_container_style)
            elif hasattr(self, "_normal_container_style"):
                self.container.setStyleSheet(self._normal_container_style)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync_maximized_frame()

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
        grid = getattr(self, "workspace_grid", None)
        if _qt_alive(grid):
            moved = grid.scroll_to_selected(only_if_out_of_view=True)
            if moved:
                self.set_status("Scrolled to selected image.")

    def center_current_folder_in_tree(self):
        browser = getattr(self, "left_browser", None)
        if _qt_alive(browser) and hasattr(browser, "scroll_to_selected_folder"):
            active_folder = getattr(self, "current_watched_dir", None)
            moved_tree = browser.scroll_to_selected_folder(
                folder_path=active_folder,
                only_if_out_of_view=False,
                center_in_view=True
            )
            if moved_tree:
                self.set_status("Centered current folder in tree.")

    def open_selected_or_first_image_in_viewer(self):
        if self._is_closing or not _qt_alive(self) or not self.isActiveWindow() or self.isHidden():
            return

        target_path = None
        grid = getattr(self, "workspace_grid", None)
        selected = getattr(grid, "selected_file_paths", None) if _qt_alive(grid) else None
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

            browser = getattr(self, "left_browser", None)
            if _qt_alive(browser):
                browser.set_selected_folder(abs_folder)
                self._center_folder_in_tree_for_path(abs_folder)
                for delay_ms in (60, 220, 500):
                    QTimer.singleShot(delay_ms, lambda fp=abs_folder: self._center_folder_in_tree_for_path(fp))

            self.apply_sorting()
            self.set_status(f"Opened favorite: {os.path.basename(abs_folder)}")
        else:
            self.set_status(f"Favorite folder no longer exists: {folder_path}")

    def add_current_folder_to_favorites(self):
        data = getattr(self, "_cached_settings", None) or {}
        current_folder = data.get("last_folder", "")
        if current_folder and os.path.isdir(current_folder):
            updated_favorites = add_folder_to_favorites(current_folder, SETTINGS_FILE)
            self._cached_settings["favorites"] = list(updated_favorites)

            menu = getattr(self, "menu_bar", None)
            if _qt_alive(menu):
                if hasattr(menu, "update_favorites"):
                    menu.update_favorites(updated_favorites)
                else:
                    menu.favorites = updated_favorites
                    menu._populate_favorites_actions()
            folder_name = os.path.basename(os.path.normpath(current_folder))
            self.set_status(f"Added to favorites: {folder_name}")
        else:
            self.set_status("No active folder to add to favorites.")

    def open_settings_dialog(self):
        self.set_status("Opening Settings Window...")
        self._active_dialog = SettingsDialog(self)
        if self._active_dialog.exec() == QDialog.DialogCode.Accepted:
            updated_settings = self._refresh_cached_settings()
            menu = getattr(self, "menu_bar", None)
            if _qt_alive(menu):
                menu.update_favorites(
                    updated_settings.get("favorites", []),
                    max_favorites=updated_settings.get("max_favorites", 15),
                )
            self._apply_scroll_speed_setting(updated_settings)
            grid = getattr(self, "workspace_grid", None)
            if _qt_alive(grid) and hasattr(grid, 'update_cache_limit_from_settings'):
                grid.update_cache_limit_from_settings()
            self.apply_concurrency_settings(updated_settings)

            if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
                self.thumb_db_cache.purge_loose_jpeg_files()
                self.thread_pool.start(CacheCleanupTask(self.thumb_cache_dir))

            self.folder_scan_cache.clear()
            if self.current_watched_dir and os.path.isdir(self.current_watched_dir):
                self.folder_sync.sync_file_watcher_paths(self.current_watched_dir)
                self.folder_sync.perform_smooth_folder_sync()
            elif not getattr(grid, "selected_file_paths", None):
                self.set_footer_status(self.build_full_gallery_status())

            self.set_status("Ready")

    def show_about_dialog(self):
        self.set_status("Viewing About Dialog...")
        AboutDialog(self).exec()
        self.set_status("Ready")

    def show_shortcuts_dialog(self):
        self.set_status("Viewing Shortcuts Dialog...")
        ShortcutsDialog(self).exec()
        self.set_status("Ready")

    def closeEvent(self, event):
        self._is_closing = True
        self.scan_id += 1

        if hasattr(self, "folder_sync") and self.folder_sync is not None:
            try:
                self.folder_sync.prepare_for_close()
            except Exception:
                pass

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
                    if _qt_alive(timer):
                        timer.stop()
                except Exception:
                    pass

        for sig_attr, slot_attr in (
            ("thumbnail_signals", "on_thumbnail_ready"),
            ("scanner_signals", None),
            ("comfy_signals", "on_comfy_batch_ready"),
            ("gif_signals", "on_animated_gif_batch_ready"),
            ("meta_signals", "on_metadata_ready"),
        ):
            sig_obj = getattr(self, sig_attr, None)
            if sig_obj is not None:
                try:
                    sig_obj.blockSignals(True)
                except Exception:
                    pass
                if slot_attr and hasattr(self, slot_attr):
                    try:
                        sig_obj.disconnect(getattr(self, slot_attr))
                    except Exception:
                        pass

        pool = getattr(self, "thread_pool", None)
        if pool is not None:
            try:
                pool.clear()
            except Exception:
                pass

        handle = getattr(self, "resize_handle", None)
        if handle is not None:
            try:
                if _qt_alive(handle):
                    handle.hide()
                    handle.setUpdatesEnabled(False)
            except Exception:
                pass

        settings = getattr(self, "_cached_settings", None) or load_settings(SETTINGS_FILE)

        print("Closing the app, Wait for any active background thread tasks to finish cleanly")
        if pool is not None:
            try:
                pool.waitForDone()
            except Exception:
                pass

        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None and not settings.get("purge_thumb_cache_on_exit", False):
            try:
                self.thumb_db_cache.flush()
            except Exception:
                pass

        if hasattr(self, "file_ops") and self.file_ops is not None:
            try:
                self.file_ops.flush_staged_trash_to_recycle_bin()
            except Exception:
                pass

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
                        if _qt_alive(viewer) and not viewer.isHidden():
                            viewer.close()
                    except RuntimeError:
                        pass

        if hasattr(self, "thumb_db_cache") and self.thumb_db_cache is not None:
            try:
                self.thumb_db_cache.close()
            except Exception:
                pass
        event.accept()


class _MetadataTask(QRunnable):
    """Background worker task to load image dimensions, EXIF, ComfyUI, and GIF animation metadata."""

    def __init__(
        self,
        file_path,
        filename,
        size_str,
        signals,
        scan_id=0,
        active_scan_getter=None,
        is_closing_getter=None,
    ):
        super().__init__()
        self.file_path = file_path
        self.filename = filename
        self.size_str = size_str
        self.signals = signals
        self.scan_id = scan_id
        self.active_scan_getter = active_scan_getter
        self.is_closing_getter = is_closing_getter

    def _is_stale(self):
        if callable(self.is_closing_getter) and self.is_closing_getter():
            return True
        return callable(self.active_scan_getter) and self.scan_id != self.active_scan_getter()

    def _extract_png_comfy_text_safe(self, file_path):
        """Pure-Python PNG chunk reader for 'workflow'/'prompt' (avoids C PIL ImageFile.load())."""
        png_sig = b"\x89PNG\r\n\x1a\n"
        try:
            with open(file_path, "rb") as f:
                if f.read(8) != png_sig:
                    return ""
                found = {}
                while True:
                    if self._is_stale():
                        return ""
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        break
                    chunk_len, chunk_type = struct.unpack(">I4s", hdr)
                    if chunk_type in (b"IDAT", b"IEND"):
                        break
                    if chunk_len > 32 * 1024 * 1024:
                        f.seek(chunk_len + 4, os.SEEK_CUR)
                        continue
                    data = f.read(chunk_len)
                    crc = f.read(4)
                    if len(data) < chunk_len or len(crc) < 4:
                        break
                    if chunk_type == b"tEXt":
                        nul = data.find(b"\x00")
                        if nul != -1:
                            key = data[:nul].decode("latin-1", errors="ignore")
                            if key in ("workflow", "prompt"):
                                found[key] = data[nul + 1:].decode("utf-8", errors="replace")
                    elif chunk_type == b"zTXt":
                        nul = data.find(b"\x00")
                        if nul != -1 and nul + 2 <= len(data):
                            key = data[:nul].decode("latin-1", errors="ignore")
                            if key in ("workflow", "prompt") and data[nul + 1] == 0:
                                raw = zlib.decompress(data[nul + 2:])
                                found[key] = raw.decode("utf-8", errors="replace")
                    elif chunk_type == b"iTXt":
                        nul = data.find(b"\x00")
                        if nul != -1 and nul + 3 <= len(data):
                            key = data[:nul].decode("latin-1", errors="ignore")
                            if key in ("workflow", "prompt"):
                                comp_flag = data[nul + 1]
                                rest = data[nul + 3:]
                                lang_end = rest.find(b"\x00")
                                if lang_end != -1:
                                    rest = rest[lang_end + 1:]
                                    trans_end = rest.find(b"\x00")
                                    if trans_end != -1:
                                        text_bytes = rest[trans_end + 1:]
                                        if comp_flag == 1:
                                            text_bytes = zlib.decompress(text_bytes)
                                        found[key] = text_bytes.decode("utf-8", errors="replace")
                    if "workflow" in found:
                        break
                for k in ("workflow", "prompt"):
                    if k in found and found[k]:
                        try:
                            return json.dumps(json.loads(found[k]), indent=4)
                        except Exception:
                            return found[k]
        except Exception as e:
            print(f"Error scanning ComfyUI metadata: {e}")
        return ""

    def run(self):
        try:
            if self._is_stale():
                return

            dims_str = "Unknown dims"
            if self.file_path and os.path.isfile(self.file_path):
                try:
                    reader = QImageReader(self.file_path)
                    sz = reader.size()
                    if sz.isValid() and sz.width() > 0 and sz.height() > 0:
                        dims_str = f"{sz.width()} \u00d7 {sz.height()} px"
                    else:
                        with _PIL_METADATA_LOCK:
                            if self._is_stale() or not os.path.isfile(self.file_path):
                                return
                            with Image.open(self.file_path) as pil_img:
                                w, h = pil_img.size
                                if w > 0 and h > 0:
                                    dims_str = f"{w} \u00d7 {h} px"
                except Exception:
                    pass

            if self._is_stale():
                return

            if not self.file_path or not os.path.isfile(self.file_path):
                exif_text = "File no longer exists."
            else:
                try:
                    with _PIL_METADATA_LOCK:
                        if self._is_stale() or not os.path.isfile(self.file_path):
                            return
                        exif_text = extract_exif_metadata(self.file_path)
                except Exception as e:
                    exif_text = f"EXIF read error: {e}"

            if self._is_stale():
                return

            comfy_text = ""
            if self.file_path and self.file_path.lower().endswith(".png") and os.path.isfile(self.file_path):
                comfy_text = self._extract_png_comfy_text_safe(self.file_path)

            if self._is_stale():
                return

            gif_info_text = ""
            if self.file_path and self.file_path.lower().endswith(".gif") and os.path.isfile(self.file_path):
                try:
                    with _PIL_METADATA_LOCK:
                        if self._is_stale() or not os.path.isfile(self.file_path):
                            return
                        gif_info = get_gif_animation_info(self.file_path)
                        if gif_info:
                            gif_info_text = (
                                f"\n\u2022 Animated GIF: Yes"
                                f"\n\u2022 Frames: {gif_info['frames']}"
                                f"\n\u2022 Duration: {gif_info['duration_sec']:.2f} s"
                                f"\n\u2022 FPS: {gif_info['fps']:.2f}"
                                f"\n\u2022 Loop: {gif_info['loop_str']}"
                            )
                except Exception:
                    gif_info_text = ""

            if self._is_stale():
                return

            try:
                if not self._is_stale() and self.signals is not None and _qt_alive(self.signals):
                    self.signals.metadata_ready.emit(
                        self.scan_id, self.file_path, self.filename, dims_str, exif_text, comfy_text, gif_info_text
                    )
            except RuntimeError:
                pass
        except Exception:
            return
        finally:
            self.signals = None
            self.active_scan_getter = None
            self.is_closing_getter = None
