import os
from collections import OrderedDict
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QSizePolicy
from PySide6.QtCore import Qt, Signal, QRect, QPoint, QSize, QRunnable, QThreadPool, QObject, QTimer, QPointF
from PySide6.QtGui import QColor, QPixmap, QImage, QPainter, QFont, QPen, QWheelEvent, QMouseEvent, QKeyEvent, \
    QImageReader
from .widgets import WindowDragHandler, ResizeHandleWidget
from .helpers import load_settings, save_settings


class ImageLoaderSignals(QObject):
    image_loaded = Signal(str, QImage, QSize)  # file_path, decoded_qimage, full_size
    image_prefetched = Signal(str, QImage, QSize)  # file_path, decoded_qimage, full_size
    full_res_loaded = Signal(str, QPixmap)  # file_path, loaded_full_pixmap


def _decode_image_proxy(file_path):
    """Helper to read and downsample (>2MP) images off the GUI thread using thread-safe QImage."""
    reader = QImageReader(file_path)
    reader.setAutoTransform(True)
    orig_size = reader.size()

    if not orig_size.isValid():
        return QImage(file_path), QSize(0, 0)

    total_pixels = orig_size.width() * orig_size.height()

    # Downsample images >2MP to a crisp 2.5K proxy for fast RAM caching and smooth rendering
    if total_pixels > 2_000_000:
        max_dim = 2560
        scaled_size = QSize(orig_size)
        scaled_size.scale(max_dim, max_dim, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(scaled_size)

    image = reader.read()
    return image, orig_size


class ImagePreloadTask(QRunnable):
    """Background task to prefetch adjacent images into a thread-safe QImage for RAM caching."""

    def __init__(self, file_path, signals):
        super().__init__()
        self.file_path = file_path
        self.signals = signals

    def run(self):
        if self.file_path and os.path.exists(self.file_path):
            image, orig_size = _decode_image_proxy(self.file_path)
            if not image.isNull():
                self.signals.image_prefetched.emit(self.file_path, image, orig_size)


class ImageLoaderTask(QRunnable):
    """Background worker task to load and decode huge images off the main GUI thread."""

    def __init__(self, file_path, signals):
        super().__init__()
        self.file_path = file_path
        self.signals = signals

    def run(self):
        image, orig_size = _decode_image_proxy(self.file_path)
        self.signals.image_loaded.emit(self.file_path, image, orig_size)


class FullResLoaderTask(QRunnable):
    """Background worker task to load full-resolution pixmaps asynchronously without freezing the UI."""

    def __init__(self, file_path, signals):
        super().__init__()
        self.file_path = file_path
        self.signals = signals

    def run(self):
        if self.file_path and os.path.exists(self.file_path):
            pixmap = QPixmap(self.file_path)
            if not pixmap.isNull():
                self.signals.full_res_loaded.emit(self.file_path, pixmap)


class ImageViewerCanvas(QWidget):
    """Custom paint canvas handling high-performance rendering, zooming, panning, and lazy full-res switching."""
    double_clicked = Signal()
    key_pressed = Signal(QKeyEvent)
    navigate_requested = Signal(int)
    close_requested = Signal()
    zoom_changed = Signal(float)
    focus_mode_toggled = Signal()
    request_full_res = Signal(str)  # Signal to trigger async full-res loading

    def __init__(self, pixmap, image_path, parent=None):
        super().__init__(parent)
        self.display_pixmap = pixmap
        self.image_path = image_path
        self.full_res_pixmap = None
        self.zoom = 1.0
        self.offset = QPoint(0, 0)
        self.dragging = False
        self.drag_start = QPoint(0, 0)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._initial_fit_calculated = False

        # Dragging state for right-click window movement
        self._is_right_dragging = False
        self._drag_start_pos = None

    def set_pixmap(self, pixmap, image_path):
        self.display_pixmap = pixmap
        self.image_path = image_path
        self.full_res_pixmap = None
        self.reset_fit()

    def reset_fit(self):
        self.zoom = 1.0
        self.offset = QPoint(0, 0)
        self._initial_fit_calculated = False
        self.zoom_changed.emit(self.zoom)
        self.update()

    def zoom_by_step(self, zoom_multiplier):
        old_zoom = self.zoom
        new_zoom = max(0.1, min(20.0, old_zoom * zoom_multiplier))
        if new_zoom == old_zoom:
            return

        self.zoom = new_zoom
        center_pos = QPointF(self.width() / 2.0, self.height() / 2.0)
        scale_ratio = self.zoom / old_zoom

        # Use QPointF to safely multiply by float scale_ratio
        offset_f = QPointF(self.offset)
        self.offset = (center_pos - (center_pos - offset_f) * scale_ratio).toPoint()

        self.check_full_res_load()
        self.zoom_changed.emit(self.zoom)
        self.update()

    def check_full_res_load(self):
        if self.zoom > 1.2 and self.full_res_pixmap is None:
            if self.image_path and os.path.exists(self.image_path):
                # Trigger asynchronous background loading instead of synchronous blocking
                self.request_full_res.emit(self.image_path)

    def wheelEvent(self, event: QWheelEvent):
        angle = event.angleDelta().y()

        # Check settings for inverted zoom
        from .helpers import load_settings
        settings = load_settings()
        invert = settings.get("invert_mouse_wheel_zoom", False)

        # Invert step behavior if enabled
        if invert:
            zoom_multiplier = 0.85 if angle > 0 else 1.15
        else:
            zoom_multiplier = 1.15 if angle > 0 else 0.85

        old_zoom = self.zoom
        new_zoom = max(0.1, min(20.0, old_zoom * zoom_multiplier))
        if new_zoom == old_zoom:
            return

        self.zoom = new_zoom
        mouse_pos = event.position()  # QPointF
        scale_ratio = self.zoom / old_zoom

        # Use QPointF for float scaling math
        offset_f = QPointF(self.offset)
        self.offset = (mouse_pos - (mouse_pos - offset_f) * scale_ratio).toPoint()

        self.check_full_res_load()
        self.zoom_changed.emit(self.zoom)
        self.update()

    def keyPressEvent(self, event: QKeyEvent):
        self.key_pressed.emit(event)

    def mousePressEvent(self, event: QMouseEvent):
        window = self.window()

        # 1. Handle Right Button (Window Dragging / Fullscreen guard)
        if event.button() == Qt.MouseButton.RightButton:
            if window and window.isFullScreen():
                event.ignore()
                return

            self._is_right_dragging = True
            if window:
                self._drag_start_pos = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            event.accept()
            return

        # 2. Handle Middle Button (Toggle Focus Mode)
        if event.button() == Qt.MouseButton.MiddleButton:
            self.focus_mode_toggled.emit()
            event.accept()
            return

        # 3. Handle Mouse Side Buttons (Back / Forward navigation)
        if event.button() == Qt.MouseButton.BackButton:
            self.navigate_requested.emit(-1)
            event.accept()
            return
        elif event.button() == Qt.MouseButton.ForwardButton:
            self.navigate_requested.emit(1)
            event.accept()
            return

        # 4. Handle Left Button (Image Panning / Dragging)
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_start = event.position().toPoint() - self.offset
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        # Handle Left-button panning/dragging movement
        if self.dragging:
            self.offset = event.position().toPoint() - self.drag_start
            self.update()
            event.accept()
            return

        # Handle Right-button window dragging (ignored if full-screen)
        window = self.window()
        if self._is_right_dragging and (event.buttons() & Qt.MouseButton.RightButton):
            if window and not window.isFullScreen() and self._drag_start_pos:
                window.move(event.globalPosition().toPoint() - self._drag_start_pos)
                event.accept()
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        # Handle Left-button release
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()

        # Handle Right-button release
        if event.button() == Qt.MouseButton.RightButton and self._is_right_dragging:
            self._is_right_dragging = False
            self._drag_start_pos = None
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)

        # Check dynamic smooth scaling setting
        settings = load_settings()
        smooth_enabled = settings.get("smooth_scaling", True)

        render_hint = QPainter.RenderHint.SmoothPixmapTransform if smooth_enabled else QPainter.RenderHint(0)
        painter.setRenderHint(render_hint, smooth_enabled)

        painter.fillRect(self.rect(), QColor("#05070a"))

        active_pixmap = (
            self.full_res_pixmap
            if (self.zoom > 1.2 and self.full_res_pixmap is not None and not self.full_res_pixmap.isNull())
            else self.display_pixmap
        )

        if active_pixmap.isNull():
            return

        widget_size = self.size()
        pixmap_size = active_pixmap.size()

        fit_scale = min(
            widget_size.width() / pixmap_size.width(),
            widget_size.height() / pixmap_size.height()
        )

        current_scale = fit_scale * self.zoom
        scaled_w = int(pixmap_size.width() * current_scale)
        scaled_h = int(pixmap_size.height() * current_scale)

        # Recalculate true dead-center offset if fit hasn't been calculated or window resized
        if not self._initial_fit_calculated:
            x = (widget_size.width() - scaled_w) // 2
            y = (widget_size.height() - scaled_h) // 2
            self.offset = QPoint(x, y)
            self._initial_fit_calculated = True
            if hasattr(self.parent(), 'update_title_zoom'):
                self.parent().update_title_zoom()

        target_rect = QRect(int(self.offset.x()), int(self.offset.y()), scaled_w, scaled_h)
        painter.drawPixmap(target_rect, active_pixmap)

    def get_effective_zoom(self):
        active_pixmap = (
            self.full_res_pixmap
            if (self.zoom > 1.2 and self.full_res_pixmap is not None and not self.full_res_pixmap.isNull())
            else self.display_pixmap
        )
        if active_pixmap.isNull():
            return 1.0

        widget_size = self.size()
        # Fall back to parent or default dimensions if layout hasn't settled yet
        if widget_size.width() <= 1 or widget_size.height() <= 1:
            if self.parent() and hasattr(self.parent(), 'size') and self.parent().width() > 1:
                widget_size = self.parent().size()
            else:
                return 1.0

        pixmap_size = active_pixmap.size()
        if pixmap_size.width() == 0 or pixmap_size.height() == 0:
            return self.zoom

        # Calculate the true fit-to-window scale factor
        fit_scale = min(
            widget_size.width() / pixmap_size.width(),
            widget_size.height() / pixmap_size.height()
        )

        # When zoom is at baseline 1.0, the effective zoom IS the fit scale
        return fit_scale * self.zoom

    def resizeEvent(self, event):
        old_size = event.oldSize()
        new_size = event.size()

        # If toggling focus mode, anchor the pan offset using center delta
        if getattr(self, '_is_toggling_focus', False):
            if old_size.isValid() and old_size.width() > 0 and old_size.height() > 0:
                old_center = QPointF(old_size.width() / 2.0, old_size.height() / 2.0)
                new_center = QPointF(new_size.width() / 2.0, new_size.height() / 2.0)
                delta = new_center - old_center
                self.offset = (QPointF(self.offset) + delta).toPoint()
            super().resizeEvent(event)
            return

        # Keep the visual center anchored during window resize using QPointF
        if old_size.isValid() and old_size.width() > 0 and old_size.height() > 0 and not self.display_pixmap.isNull():
            active_pixmap = (
                self.full_res_pixmap
                if (self.zoom > 1.2 and self.full_res_pixmap is not None and not self.full_res_pixmap.isNull())
                else self.display_pixmap
            )
            p_w = active_pixmap.width()
            p_h = active_pixmap.height()
            if p_w > 0 and p_h > 0:
                old_fit = min(old_size.width() / p_w, old_size.height() / p_h)
                new_fit = min(new_size.width() / p_w, new_size.height() / p_h)
                if old_fit > 0:
                    scale_ratio = new_fit / old_fit
                    old_center = QPointF(old_size.width() / 2.0, old_size.height() / 2.0)
                    new_center = QPointF(new_size.width() / 2.0, new_size.height() / 2.0)

                    offset_f = QPointF(self.offset)
                    self.offset = (new_center - (old_center - offset_f) * scale_ratio).toPoint()

        if self._initial_fit_calculated:
            self.zoom_changed.emit(self.zoom)
        self.update()


class ImageViewerWindow(QWidget):
    """
    Multithreaded image viewer window supporting persistent shared RAM caching,
    instant proxy loading for huge images (>2MP), background neighbor prefetching,
    cursor-centered zooming, click-and-drag panning, and navigation.
    """
    # Persistent shared cache across viewer instances so closed/prefetched images stay cached
    _SHARED_PIXMAP_CACHE = OrderedDict()
    _MAX_CACHE_ITEMS = 30

    def __init__(self, image_path, items_list=None, parent=None):
        if isinstance(items_list, QWidget):
            parent = items_list
            items_list = getattr(parent, "loaded_items", [])

        QWidget.__init__(self, parent)
        WindowDragHandler.__init__(self)  # Initialize the drag handler mixin

        self.image_path = image_path
        self.items = items_list or []
        self.main_parent = parent
        self._is_focus_mode = False  # Track middle-click focus mode state

        self.current_index = 0
        for i, item in enumerate(self.items):
            if item.get("file_path") == image_path:
                self.current_index = i
                break

        self.setWindowTitle("Image Viewer")
        self.resize(900, 650)
        self.setMinimumSize(300, 280)

        # Check settings for always on top, default to True
        settings = load_settings()
        self.is_always_on_top = settings.get("viewer_always_on_top", True)

        flags = (
                Qt.WindowType.Window |
                Qt.WindowType.FramelessWindowHint
        )
        if self.is_always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint

        self.setWindowFlags(flags)
        
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)  # Required for edge hovering
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.thread_pool = QThreadPool.globalInstance()
        self.loader_signals = ImageLoaderSignals()
        self.loader_signals.image_loaded.connect(self.on_image_loaded)
        self.loader_signals.image_prefetched.connect(self.on_image_prefetched)
        self.loader_signals.full_res_loaded.connect(self.on_full_res_loaded)

        self.in_flight_prefetches = set()
        self.full_pixmap_size = QSize(0, 0)

        self._setup_viewer_ui()
        self.setFocus()

        # Only trigger startup fullscreen for main ImageViewerWindow, NOT detached pinned viewers
        if type(self) is ImageViewerWindow:
            settings = load_settings()
            if settings.get("default_zoom_mode", "") == "Fill Screen":
                QTimer.singleShot(50, self.toggle_fullscreen)

        # Start loading immediately (pulls instantly from shared cache if previously viewed)
        self.load_image_threaded(self.image_path)

        # Guarantee initial zoom reads correctly right after layout settles
        QTimer.singleShot(30, self.update_title_zoom)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def enterEvent(self, event):
        """Automatically activate window and grant keyboard focus when the mouse hovers over."""
        self.activateWindow()
        self.setFocus()
        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.setFocus()
        super().enterEvent(event)

    def _store_in_cache(self, file_path, pixmap, full_size):
        """Stores a QPixmap in the persistent shared LRU cache."""
        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        if file_path in cache:
            cache.move_to_end(file_path)
        cache[file_path] = (pixmap, full_size)

        while len(cache) > ImageViewerWindow._MAX_CACHE_ITEMS:
            cache.popitem(last=False)

    def load_image_threaded(self, path):
        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        if path in cache:
            pixmap, full_size = cache[path]
            cache.move_to_end(path)
            self._display_ready_pixmap(path, pixmap, full_size)
            self.prefetch_neighbors()
            return

        file_title = os.path.basename(path)
        self.title_label.setText(f"🔍 Loading: {file_title}...")

        task = ImageLoaderTask(path, self.loader_signals)
        self.thread_pool.start(task)

    def on_image_loaded(self, file_path, image, full_size):
        pixmap = QPixmap.fromImage(image)
        self._store_in_cache(file_path, pixmap, full_size)

        if file_path != self.image_path:
            return

        self._display_ready_pixmap(file_path, pixmap, full_size)
        self.prefetch_neighbors()

    def on_image_prefetched(self, file_path, image, full_size):
        self.in_flight_prefetches.discard(file_path)
        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        if file_path not in cache:
            pixmap = QPixmap.fromImage(image)
            self._store_in_cache(file_path, pixmap, full_size)

    def on_full_res_loaded(self, file_path, pixmap):
        if file_path == self.image_path:
            self.canvas.full_res_pixmap = pixmap
            self.canvas.update()
            self.update_title_zoom()

    def request_async_full_res(self, file_path):
        """Starts background task to load full resolution image."""
        if file_path and os.path.exists(file_path):
            task = FullResLoaderTask(file_path, self.loader_signals)
            self.thread_pool.start(task)

    def _display_ready_pixmap(self, file_path, pixmap, full_size):
        self.full_pixmap_size = full_size

        # Update window dimensions to snugly fit the image aspect ratio with dynamic padding
        if full_size.isValid() and not self.isFullScreen():
            # Dynamically measure actual widget and layout overheads instead of hardcoding
            # 1. Horizontal padding: container layout left/right margins + frame borders
            h_margins = self.container_layout.contentsMargins().left() + self.container_layout.contentsMargins().right()
            main_margins = self.main_layout.contentsMargins().left() + self.main_layout.contentsMargins().right()
            padding_w = h_margins + main_margins + 4  # 4px safety buffer for splitter/borders

            # 2. Vertical padding: title container height + bottom layout + all margins & spacing
            title_h = self.title_container.sizeHint().height() if hasattr(self, 'title_container') else 30
            bottom_h = 30  # Height allocated for the resize handle / bottom row
            v_spacing = self.container_layout.spacing() * 2
            v_margins = self.container_layout.contentsMargins().top() + self.container_layout.contentsMargins().bottom()
            main_v_margins = self.main_layout.contentsMargins().top() + self.main_layout.contentsMargins().bottom()

            padding_h = title_h + bottom_h + v_spacing + v_margins + main_v_margins

            img_w = full_size.width()
            img_h = full_size.height()
            img_aspect = img_w / img_h if img_h > 0 else 1.0

            screen_geom = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)

            # Max allowed screen bounds
            max_w = screen_geom.width() - 100
            max_h = screen_geom.height() - 100

            # Scale down proportionally if image exceeds screen dimensions
            target_w = img_w
            target_h = img_h

            if target_w > (max_w - padding_w) or target_h > (max_h - padding_h):
                if img_aspect > ((max_w - padding_w) / (max_h - padding_h)):
                    target_w = max_w - padding_w
                    target_h = int(target_w / img_aspect)
                else:
                    target_h = max_h - padding_h
                    target_w = int(target_h * img_aspect)

            final_w = max(400, target_w + padding_w)
            final_h = max(300, target_h + padding_h)

            self.resize(final_w, final_h)

            center_point = screen_geom.center()
            window_geom = self.frameGeometry()
            window_geom.moveCenter(center_point)
            self.move(window_geom.topLeft())

        # Assign pixmap to canvas
        self.canvas.display_pixmap = pixmap
        self.canvas.image_path = file_path
        self.canvas.full_res_pixmap = None

        # Reset fit immediately, and schedule a safety re-center to guarantee layout is settled
        self.canvas.reset_fit()
        QTimer.singleShot(10, self.canvas.reset_fit)

        self.update_title_zoom()

    def update_title_zoom(self):
        file_title = os.path.basename(self.image_path)
        dim_info = f" ({self.full_pixmap_size.width()}x{self.full_pixmap_size.height()})" if self.full_pixmap_size.isValid() else ""

        effective_scale = self.canvas.get_effective_zoom()
        zoom_pct = int(effective_scale * 100)
        self.title_label.setText(f"{file_title}{dim_info} ({zoom_pct}%)")

    def prefetch_neighbors(self):
        # Respect dynamic prefetch setting
        settings = load_settings()  # make sure load_settings is imported in img_viewer.py if not already
        if not settings.get("prefetch_images", True):
            return

        if len(self.items) <= 1:
            return

        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        steps_ahead = 3
        steps_behind = 2
        neighbor_indices = []

        for step in range(1, steps_ahead + 1):
            neighbor_indices.append((self.current_index + step) % len(self.items))

        for step in range(1, steps_behind + 1):
            neighbor_indices.append((self.current_index - step) % len(self.items))

        for idx in neighbor_indices:
            path = self.items[idx].get("file_path")
            if path and path != self.image_path and path not in cache and path not in self.in_flight_prefetches:
                self.in_flight_prefetches.add(path)
                self.thread_pool.start(ImagePreloadTask(path, self.loader_signals))

    def closeEvent(self, event):
        if self.main_parent and hasattr(self.main_parent, 'workspace_grid'):
            self.main_parent.workspace_grid.selected_file_paths = {self.image_path}
            self.main_parent.workspace_grid.update()
            self.main_parent.on_card_selected(self.image_path, os.path.basename(self.image_path))
            self.main_parent.show()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._is_focus_mode:
                self.toggle_focus_mode()  # Exit focus mode first on Esc if active
            else:
                self.close()
            event.accept()
        elif key == Qt.Key.Key_Up:
            self.canvas.zoom_by_step(1.15)
            event.accept()
        elif key == Qt.Key.Key_Down:
            self.canvas.zoom_by_step(0.85)
            event.accept()
        elif key == Qt.Key.Key_Right:
            self.navigate(1)
            event.accept()
        elif key == Qt.Key.Key_Left:
            self.navigate(-1)
            event.accept()
        else:
            super().keyPressEvent(event)

    def navigate(self, direction):
        if not self.items:
            return

        new_index = self.current_index + direction

        # --- Check setting for gallery navigation looping ---
        from .helpers import load_settings  # Ensure load_settings is imported
        settings = load_settings()
        loop_enabled = settings.get("loop_gallery_navigation", True)

        if not loop_enabled:
            # Clamp index at boundaries if looping is disabled
            if new_index < 0 or new_index >= len(self.items):
                return

        # Apply wrapping if looping is enabled
        self.current_index = new_index % len(self.items)
        # ---------------------------------------------------

        next_item = self.items[self.current_index]
        self.image_path = next_item["file_path"]

        self.load_image_threaded(self.image_path)

        if self.main_parent and hasattr(self.main_parent, 'workspace_grid'):
            self.main_parent.workspace_grid.selected_file_paths = {self.image_path}
            self.main_parent.workspace_grid.update()
            file_title = os.path.basename(self.image_path)
            self.main_parent.on_card_selected(self.image_path, file_title)
            self.main_parent.set_status(f"Viewing: {file_title}")

    def _setup_viewer_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(8, 8, 8, 8)

        self.container = QFrame(self)
        self.container.setStyleSheet("""
            QFrame {
                background-color: #0b0f19;
                border: 1px solid #1e293b;
                border-radius: 10px;
            }
        """)

        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(10, 10, 10, 10)
        self.container_layout.setSpacing(8)

        self.title_container = QWidget(self.container)
        title_layout = QHBoxLayout(self.title_container)
        title_layout.setContentsMargins(4, 0, 4, 0)

        # Always-on-top toggle button
        self.pin_btn = QPushButton("📍" if self.is_always_on_top else "📌")
        self.pin_btn.setFixedSize(28, 28)
        self.pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pin_btn.setToolTip("Toggle Always on Top")
        self.pin_btn.setStyleSheet("""
                           QPushButton { background-color: transparent; color: #94a3b8; border: none; border-radius: 6px; font-size: 13px; }
                           QPushButton:hover { background-color: #1e293b; color: #ffffff; }
                       """)
        self.pin_btn.clicked.connect(self.toggle_always_on_top)

        # Insert pin button before close/minimize buttons in the title layout
        title_layout.addWidget(self.pin_btn)


        file_title = os.path.basename(self.image_path)
        self.title_label = QLabel(f"🔍 Loading: {file_title}...")
        self.title_label.setStyleSheet("color: #cbd5e1; font-size: 12px; font-weight: bold; background: transparent;")
        title_layout.addWidget(self.title_label)

        title_layout.addStretch()


        btn_close = QPushButton("✕")
        btn_close.setFixedSize(26, 26)
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet("""
            QPushButton {
                background-color: #131b2e;
                color: #94a3b8;
                border: 1px solid #1e293b;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ef4444;
                color: #ffffff;
                border-color: #ef4444;
            }
        """)
        btn_close.clicked.connect(self.close)
        title_layout.addWidget(btn_close)

        self.container_layout.addWidget(self.title_container)

        self.canvas = ImageViewerCanvas(QPixmap(), self.image_path, self)
        self.canvas.key_pressed.connect(self.keyPressEvent)
        self.canvas.double_clicked.connect(self.toggle_fullscreen)
        self.canvas.navigate_requested.connect(self.navigate)
        self.canvas.close_requested.connect(self.close)
        self.canvas.zoom_changed.connect(lambda z: self.update_title_zoom())
        self.canvas.focus_mode_toggled.connect(self.toggle_focus_mode)
        self.canvas.request_full_res.connect(self.request_async_full_res)
        self.container_layout.addWidget(self.canvas, stretch=1)

        # --- Add Resize Handle at the bottom right corner ---
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addStretch()

        # Instantiate your custom ResizeHandleWidget from widgets.py
        self.resize_grip = ResizeHandleWidget(self)
        bottom_layout.addWidget(self.resize_grip)

        self.container_layout.addLayout(bottom_layout)
        self.main_layout.addWidget(self.container)

    def toggle_focus_mode(self):
        """Toggles visibility of window canvas elements for a clean borderless image view."""
        if self.isFullScreen():
            return  # Focus mode is designed for windowed mode

        self._is_focus_mode = not self._is_focus_mode

        if self._is_focus_mode:
            self.title_container.setVisible(False)
            self.resize_grip.setVisible(False)
            self.main_layout.setContentsMargins(0, 0, 0, 0)
            self.container_layout.setContentsMargins(0, 0, 0, 0)
            self.container_layout.setSpacing(0)
            self.container.setStyleSheet("""
                QFrame {
                    background-color: #05070a;
                    border: none;
                    border-radius: 0px;
                }
            """)
        else:
            self.title_container.setVisible(True)
            self.resize_grip.setVisible(True)
            self.main_layout.setContentsMargins(8, 8, 8, 8)
            self.container_layout.setContentsMargins(10, 10, 10, 10)
            self.container_layout.setSpacing(8)
            self.container.setStyleSheet("""
                QFrame {
                    background-color: #0b0f19;
                    border: 1px solid #1e293b;
                    border-radius: 10px;
                }
            """)

        self.update_title_zoom()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.main_layout.setContentsMargins(8, 8, 8, 8)
            self.container_layout.setContentsMargins(10, 10, 10, 10)
            self.container_layout.setSpacing(8)

            self.title_container.setVisible(True)
            self.resize_grip.setVisible(True)
            self.container.setStyleSheet("""
                QFrame {
                    background-color: #0b0f19;
                    border: 1px solid #1e293b;
                    border-radius: 10px;
                }
            """)
            self.showNormal()
        else:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
            self.main_layout.setContentsMargins(0, 0, 0, 0)
            self.container_layout.setContentsMargins(0, 0, 0, 0)
            self.container_layout.setSpacing(0)

            self.title_container.setVisible(False)
            self.resize_grip.setVisible(False)
            self.container.setStyleSheet("""
                QFrame {
                    background-color: #05070a;
                    border: none;
                    border-radius: 0px;
                }
            """)
            self.showFullScreen()

        self.canvas.reset_fit()

    def toggle_always_on_top(self):
        self.is_always_on_top = not self.is_always_on_top
        self.pin_btn.setText("📍" if self.is_always_on_top else "📌")

        # Preserve geometry and visibility state when changing window flags
        pos = self.pos()
        size = self.size()
        was_visible = self.isVisible()

        flags = (
                Qt.WindowType.Window |
                Qt.WindowType.FramelessWindowHint
        )
        if self.is_always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint

        self.setWindowFlags(flags)
        if was_visible:
            self.show()
            self.setGeometry(QRect(pos, size))
            # Bring window back to the front and ensure it retains active focus
            self.raise_()
            self.activateWindow()

        # Persist to settings.json properly
        save_settings(new_values={"viewer_always_on_top": self.is_always_on_top})

    def _get_resize_edge(self, global_pos):
        """Determines which edge or corner the mouse is hovering over using global coordinates."""
        if self._is_focus_mode or self.isFullScreen():
            return None

        # Map global position to local window coordinates safely
        local_pos = self.mapFromGlobal(global_pos.toPoint())
        margin = 8
        w = self.width()
        h = self.height()
        x = local_pos.x()
        y = local_pos.y()

        left = x < margin
        right = x >= w - margin
        top = y < margin
        bottom = y >= h - margin

        if top and left: return "top_left"
        if top and right: return "top_right"
        if bottom and left: return "bottom_left"
        if bottom and right: return "bottom_right"
        if left: return "left"
        if right: return "right"
        if top: return "top"
        if bottom: return "bottom"
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.isFullScreen():
            global_pos = event.globalPosition()
            self._resize_edge = self._get_resize_edge(global_pos)

            if self._resize_edge:
                self._drag_pos = global_pos.toPoint()
                self._start_geometry = self.geometry()
                event.accept()
                return

            # Check title bar local position for moving window (disabled in focus mode)
            if not self._is_focus_mode:
                local_pos = self.mapFromGlobal(global_pos.toPoint())
                if local_pos.y() < 40:
                    self._drag_pos = global_pos.toPoint() - self.frameGeometry().topLeft()
                    self._resize_edge = None
                    event.accept()

    def mouseMoveEvent(self, event):
        if self.isFullScreen() or self._is_focus_mode:
            return

        global_pos = event.globalPosition()

        # Update cursor shape based on hover position when not actively resizing
        if not hasattr(self, '_resize_edge') or not self._resize_edge:
            edge = self._get_resize_edge(global_pos)
            if edge in ("top_left", "bottom_right"):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            elif edge in ("top_right", "bottom_left"):
                self.setCursor(Qt.CursorShape.SizeBDiagCursor)
            elif edge in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edge in ("top", "bottom"):
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

        # Handle active resizing
        if hasattr(self, '_resize_edge') and self._resize_edge:
            diff = global_pos.toPoint() - self._drag_pos
            geom = QRect(self._start_geometry)

            if "left" in self._resize_edge:
                geom.setLeft(geom.left() + diff.x())
            if "right" in self._resize_edge:
                geom.setRight(geom.right() + diff.x())
            if "top" in self._resize_edge:
                geom.setTop(geom.top() + diff.y())
            if "bottom" in self._resize_edge:
                geom.setBottom(geom.bottom() + diff.y())

            if geom.width() >= self.minimumWidth() and geom.height() >= self.minimumHeight():
                self.setGeometry(geom)
            event.accept()
            return

        # Handle active window dragging from title bar
        if hasattr(self, '_drag_pos') and self.mapFromGlobal(global_pos.toPoint()).y() < 40:
            self.move(global_pos.toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._resize_edge = None
            if hasattr(self, '_drag_pos'):
                del self._drag_pos
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()


class DetachedImageViewerCanvas(ImageViewerCanvas):
    """Custom canvas for pinned viewers (shares base canvas drag and focus mode behavior)."""
    pass


class DetachedImageViewerWindow(ImageViewerWindow):
    """
    Special independent viewer instance opened via Alt + Double-Click.
    Allows multiple concurrent windows, stays visible when main window minimizes,
    and supports right-click dragging.
    """

    def __init__(self, image_path, items_list=None, parent=None):
        self._is_first_open = True  # Initialize before super().__init__() because loading starts immediately
        super().__init__(image_path, items_list, parent=None)

        file_title = os.path.basename(image_path)
        self.setWindowTitle(f"Pinned Viewer: {file_title}")

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

    def _display_ready_pixmap(self, file_path, pixmap, full_size):
        self.full_pixmap_size = full_size

        # Center and resize using half screen bounds ONLY on first opening
        if self._is_first_open and full_size.isValid() and not self.isFullScreen():
            self._is_first_open = False
            h_margins = self.container_layout.contentsMargins().left() + self.container_layout.contentsMargins().right()
            main_margins = self.main_layout.contentsMargins().left() + self.main_layout.contentsMargins().right()
            padding_w = h_margins + main_margins + 4

            title_h = self.title_container.sizeHint().height() if hasattr(self, 'title_container') else 30
            bottom_h = 30
            v_spacing = self.container_layout.spacing() * 2
            v_margins = self.container_layout.contentsMargins().top() + self.container_layout.contentsMargins().bottom()
            main_v_margins = self.main_layout.contentsMargins().top() + self.main_layout.contentsMargins().bottom()

            padding_h = title_h + bottom_h + v_spacing + v_margins + main_v_margins

            img_w = full_size.width()
            img_h = full_size.height()
            img_aspect = img_w / img_h if img_h > 0 else 1.0

            screen_geom = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)

            # Max allowed screen bounds (half screen)
            max_w = screen_geom.width() / 2
            max_h = screen_geom.height() / 2

            target_w = img_w
            target_h = img_h

            if target_w > (max_w - padding_w) or target_h > (max_h - padding_h):
                if img_aspect > ((max_w - padding_w) / (max_h - padding_h)):
                    target_w = max_w - padding_w
                    target_h = int(target_w / img_aspect)
                else:
                    target_h = max_h - padding_h
                    target_w = int(target_h * img_aspect)

            final_w = max(400, target_w + padding_w)
            final_h = max(300, target_h + padding_h)

            self.resize(final_w, final_h)

            center_point = screen_geom.center()
            window_geom = self.frameGeometry()
            window_geom.moveCenter(center_point)
            self.move(window_geom.topLeft())

        # Assign pixmap to canvas without changing position/size on subsequent image navigations
        self.canvas.display_pixmap = pixmap
        self.canvas.image_path = file_path
        self.canvas.full_res_pixmap = None

        self.canvas.reset_fit()
        QTimer.singleShot(10, self.canvas.reset_fit)

        self.update_title_zoom()

    def closeEvent(self, event):
        if self.main_parent and hasattr(self.main_parent, 'detached_viewers'):
            if self in self.main_parent.detached_viewers:
                self.main_parent.detached_viewers.remove(self)
        event.accept()