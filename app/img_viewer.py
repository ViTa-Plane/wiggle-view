import os
from collections import OrderedDict
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame
from PySide6.QtCore import Qt, Signal, QRect, QPoint, QSize, QRunnable, QThreadPool, QObject, QTimer, QPointF
from PySide6.QtGui import QColor, QPixmap, QImage, QPainter, QWheelEvent, QMouseEvent, QKeyEvent, QImageReader, QPen, QPainterPath
from .widgets import ResizeHandleWidget, WindowDragHandler
from .helpers import load_settings, save_settings


PIN_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"></path></svg>"""

UNPIN_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="2" y1="2" x2="22" y2="22"></line><line x1="12" y1="17" x2="12" y2="22"></line><path d="M9 9v1.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V17h12"></path><path d="M15 9.34V6h1a2 2 0 0 0 0-4H7.89"></path></svg>"""


class PinIconButton(QPushButton):
    """Custom button rendering pin/unpin icons cleanly on Windows frameless windows."""

    def __init__(self, pinned: bool = True, parent=None):
        super().__init__(parent)
        self._pinned = bool(pinned)
        self._hovered = False
        self.setFixedSize(32, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._sync_tooltip()

    def _sync_tooltip(self):
        if self._pinned:
            self.setToolTip("Always on Top: ON (Click to Unpin)")
        else:
            self.setToolTip("Always on Top: OFF (Click to Pin)")

    def set_pinned(self, pinned: bool):
        self._pinned = bool(pinned)
        self._sync_tooltip()
        self.update()

    def is_pinned(self) -> bool:
        return self._pinned

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        if not painter.isActive():
            return
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = self.rect().adjusted(2, 2, -2, -2)

            if self._hovered:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#1e293b"))
                painter.drawRoundedRect(rect, 6, 6)

            if self._pinned:
                color = QColor("#38bdf8") if self._hovered else QColor("#e2e8f0")
            else:
                color = QColor("#cbd5e1") if self._hovered else QColor("#94a3b8")

            pen = QPen(color, 1.8)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)

            cx = self.width() / 2.0
            cy = self.height() / 2.0

            # Pushpin head, collar, and needle
            painter.drawRoundedRect(QRect(int(cx - 4.5), int(cy - 8.5), 9, 3), 1.5, 1.5)
            body = QPainterPath()
            body.moveTo(cx - 3.0, cy - 5.5)
            body.lineTo(cx + 3.0, cy - 5.5)
            body.lineTo(cx + 5.0, cy + 1.5)
            body.lineTo(cx - 5.0, cy + 1.5)
            body.closeSubpath()
            if self._pinned:
                painter.setBrush(color)
            else:
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(body)

            painter.drawLine(QPointF(cx, cy + 1.5), QPointF(cx, cy + 8.0))

            if not self._pinned:
                slash_pen = QPen(QColor("#f87171") if self._hovered else QColor("#94a3b8"), 1.6)
                slash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(slash_pen)
                painter.drawLine(QPointF(cx - 6.5, cy - 6.5), QPointF(cx + 6.5, cy + 6.5))
        finally:
            if painter.isActive():
                painter.end()


class ImageLoaderSignals(QObject):
    image_loaded = Signal(str, QImage, QSize)  # file_path, decoded_qimage, full_size
    image_prefetched = Signal(str, QImage, QSize)  # file_path, decoded_qimage, full_size
    full_res_loaded = Signal(str, QPixmap)  # file_path, loaded_full_pixmap


def _decode_image_proxy(file_path):
    """Read and downsample (>2MP) images off the GUI thread using thread-safe QImage."""
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

    def __init__(self, file_path, signals, is_closing_getter=None):
        super().__init__()
        self.file_path = file_path
        self.signals = signals
        self.is_closing_getter = is_closing_getter

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def run(self):
        if self._is_closing() or not self.file_path or not os.path.exists(self.file_path):
            return
        image, orig_size = _decode_image_proxy(self.file_path)
        if not self._is_closing() and not image.isNull():
            try:
                self.signals.image_prefetched.emit(self.file_path, image, orig_size)
            except RuntimeError:
                pass


class ImageLoaderTask(QRunnable):
    """Background worker task to load and decode huge images off the main GUI thread."""

    def __init__(self, file_path, signals, is_closing_getter=None):
        super().__init__()
        self.file_path = file_path
        self.signals = signals
        self.is_closing_getter = is_closing_getter

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def run(self):
        if self._is_closing():
            return
        image, orig_size = _decode_image_proxy(self.file_path)
        if not self._is_closing():
            try:
                self.signals.image_loaded.emit(self.file_path, image, orig_size)
            except RuntimeError:
                pass


class FullResLoaderTask(QRunnable):
    """Background worker task to load full-resolution pixmaps asynchronously without freezing the UI."""

    def __init__(self, file_path, signals, is_closing_getter=None):
        super().__init__()
        self.file_path = file_path
        self.signals = signals
        self.is_closing_getter = is_closing_getter

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def run(self):
        if self._is_closing() or not self.file_path or not os.path.exists(self.file_path):
            return
        pixmap = QPixmap(self.file_path)
        if not self._is_closing() and not pixmap.isNull():
            try:
                self.signals.full_res_loaded.emit(self.file_path, pixmap)
            except RuntimeError:
                pass


class ImageViewerCanvas(QWidget):
    """Custom paint canvas handling high-performance rendering, zooming, panning, and lazy full-res switching."""
    double_clicked = Signal()
    key_pressed = Signal(QKeyEvent)
    navigate_requested = Signal(int)
    close_requested = Signal()
    zoom_changed = Signal(float)
    focus_mode_toggled = Signal()
    request_full_res = Signal(str)

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
        self._full_res_requested_for = None

        # Cached viewer preferences (updated by parent window without per-frame disk I/O)
        self.smooth_scaling = True
        self.invert_mouse_wheel_zoom = False
        self.mouse_zoom = True

        # Dragging state for right-click window movement
        self._is_right_dragging = False
        self._drag_start_pos = None

    def set_pixmap(self, pixmap, image_path):
        self.display_pixmap = pixmap
        self.image_path = image_path
        self.full_res_pixmap = None
        self._full_res_requested_for = None
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

        offset_f = QPointF(self.offset)
        self.offset = (center_pos - (center_pos - offset_f) * scale_ratio).toPoint()

        self.check_full_res_load()
        self.zoom_changed.emit(self.zoom)
        self.update()

    def check_full_res_load(self):
        if self.zoom > 1.2 and self.full_res_pixmap is None:
            if self.image_path and self._full_res_requested_for != self.image_path and os.path.exists(self.image_path):
                self._full_res_requested_for = self.image_path
                self.request_full_res.emit(self.image_path)

    def wheelEvent(self, event: QWheelEvent):
        angle = event.angleDelta().y()
        if angle == 0:
            return

        if not self.mouse_zoom:
            if angle < 0:
                self.navigate_requested.emit(1)
            elif angle > 0:
                self.navigate_requested.emit(-1)
            event.accept()
            return

        if self.invert_mouse_wheel_zoom:
            zoom_multiplier = 0.85 if angle > 0 else 1.15
        else:
            zoom_multiplier = 1.15 if angle > 0 else 0.85

        old_zoom = self.zoom
        new_zoom = max(0.1, min(20.0, old_zoom * zoom_multiplier))
        if new_zoom == old_zoom:
            return

        self.zoom = new_zoom
        mouse_pos = event.position()
        scale_ratio = self.zoom / old_zoom

        offset_f = QPointF(self.offset)
        self.offset = (mouse_pos - (mouse_pos - offset_f) * scale_ratio).toPoint()

        self.check_full_res_load()
        self.zoom_changed.emit(self.zoom)
        self.update()

    def keyPressEvent(self, event: QKeyEvent):
        self.key_pressed.emit(event)

    def mousePressEvent(self, event: QMouseEvent):
        window = self.window()

        if event.button() == Qt.MouseButton.RightButton:
            if window and window.isFullScreen():
                event.ignore()
                return

            self._is_right_dragging = True
            if window:
                self._drag_start_pos = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            event.accept()
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self.focus_mode_toggled.emit()
            event.accept()
            return

        if event.button() == Qt.MouseButton.BackButton:
            self.navigate_requested.emit(-1)
            event.accept()
            return
        elif event.button() == Qt.MouseButton.ForwardButton:
            self.navigate_requested.emit(1)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_start = event.position().toPoint() - self.offset
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.dragging:
            self.offset = event.position().toPoint() - self.drag_start
            self.update()
            event.accept()
            return

        window = self.window()
        if self._is_right_dragging and (event.buttons() & Qt.MouseButton.RightButton):
            if window and not window.isFullScreen() and self._drag_start_pos is not None:
                window.move(event.globalPosition().toPoint() - self._drag_start_pos)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()

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
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.smooth_scaling)
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
        if pixmap_size.width() <= 0 or pixmap_size.height() <= 0:
            return

        fit_scale = min(
            widget_size.width() / pixmap_size.width(),
            widget_size.height() / pixmap_size.height()
        )

        current_scale = fit_scale * self.zoom
        scaled_w = int(pixmap_size.width() * current_scale)
        scaled_h = int(pixmap_size.height() * current_scale)

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
        if widget_size.width() <= 1 or widget_size.height() <= 1:
            if self.parent() and hasattr(self.parent(), 'size') and self.parent().width() > 1:
                widget_size = self.parent().size()
            else:
                return 1.0

        pixmap_size = active_pixmap.size()
        if pixmap_size.width() == 0 or pixmap_size.height() == 0:
            return self.zoom

        fit_scale = min(
            widget_size.width() / pixmap_size.width(),
            widget_size.height() / pixmap_size.height()
        )
        return fit_scale * self.zoom

    def resizeEvent(self, event):
        old_size = event.oldSize()
        new_size = event.size()

        if getattr(self, '_is_toggling_focus', False):
            if old_size.isValid() and old_size.width() > 0 and old_size.height() > 0:
                old_center = QPointF(old_size.width() / 2.0, old_size.height() / 2.0)
                new_center = QPointF(new_size.width() / 2.0, new_size.height() / 2.0)
                delta = new_center - old_center
                self.offset = (QPointF(self.offset) + delta).toPoint()
            super().resizeEvent(event)
            return

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


class ImageViewerWindow(QWidget, WindowDragHandler):
    """
    Multithreaded image viewer window supporting persistent shared RAM caching,
    instant proxy loading for huge images (>2MP), background neighbor prefetching,
    cursor-centered zooming, click-and-drag panning, and navigation.
    """
    _SHARED_PIXMAP_CACHE = OrderedDict()
    _MAX_CACHE_ITEMS = 30

    def __init__(self, image_path, items_list=None, parent=None):
        if isinstance(items_list, QWidget):
            parent = items_list
            items_list = getattr(parent, "loaded_items", [])

        super().__init__(parent)
        WindowDragHandler.__init__(self)

        self._closing = False
        self._user_resized = False
        self._toggling_topmost = False
        self._has_positioned_window = False
        self._dragging_window = False
        self._programmatic_resizing = False
        self._anchor_center = None
        self.image_path = image_path
        self.items = list(items_list) if items_list else []
        self.main_parent = parent
        self._is_focus_mode = False
        self._resize_edge = None

        self.current_index = 0
        for i, item in enumerate(self.items):
            if item.get("file_path") == image_path:
                self.current_index = i
                break

        self.setWindowTitle("Image Viewer")
        self.resize(900, 650)
        self.setMinimumSize(300, 280)

        settings = load_settings()
        self.is_always_on_top = bool(settings.get("viewer_always_on_top", True))
        self._prefetch_enabled = settings.get("prefetch_images", True)
        self._loop_navigation = settings.get("loop_gallery_navigation", True)
        self._smooth_scaling = settings.get("smooth_scaling", True)
        self._invert_wheel_zoom = settings.get("invert_mouse_wheel_zoom", False)
        self._mouse_zoom = bool(settings.get("mouse_zoom", True))

        self._apply_window_flags(self.is_always_on_top, show_if_visible=False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.thread_pool = QThreadPool.globalInstance()
        self.loader_signals = ImageLoaderSignals()
        self.loader_signals.image_loaded.connect(self.on_image_loaded)
        self.loader_signals.image_prefetched.connect(self.on_image_prefetched)
        self.loader_signals.full_res_loaded.connect(self.on_full_res_loaded)

        self.in_flight_prefetches = set()
        self.full_pixmap_size = QSize(0, 0)

        self._setup_viewer_ui()
        self.canvas.smooth_scaling = self._smooth_scaling
        self.canvas.invert_mouse_wheel_zoom = self._invert_wheel_zoom
        self.canvas.mouse_zoom = self._mouse_zoom
        self.setFocus()

        if type(self) is ImageViewerWindow:
            if settings.get("default_zoom_mode", "") == "Fill Screen":
                QTimer.singleShot(50, self.toggle_fullscreen)

        self.load_image_threaded(self.image_path)
        QTimer.singleShot(30, self.update_title_zoom)

    def _build_window_flags(self, always_on_top: bool) -> Qt.WindowType:
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        return flags

    def _apply_window_flags(self, always_on_top: bool, show_if_visible: bool = False):
        was_visible = self.isVisible()
        pos = self.pos()
        size = self.size()

        self._toggling_topmost = True
        try:
            self.setWindowFlags(self._build_window_flags(always_on_top))
            if not self.isFullScreen():
                self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            if show_if_visible and was_visible:
                self.setGeometry(QRect(pos, size))
                self.show()
                self._position_corner_grip()
                self.raise_()
                self.activateWindow()
        finally:
            self._toggling_topmost = False

    def _is_closing(self):
        return self._closing

    def enterEvent(self, event):
        """Automatically activate window and grant keyboard focus when the mouse hovers over."""
        if not self._toggling_topmost:
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
        if self._closing:
            return

        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        if path in cache:
            pixmap, full_size = cache[path]
            cache.move_to_end(path)
            self._display_ready_pixmap(path, pixmap, full_size)
            self.prefetch_neighbors()
            return

        file_title = os.path.basename(path)
        self.title_label.setText(f"Loading: {file_title}...")

        task = ImageLoaderTask(path, self.loader_signals, is_closing_getter=self._is_closing)
        self.thread_pool.start(task)

    def on_image_loaded(self, file_path, image, full_size):
        if self._closing or image.isNull():
            return

        pixmap = QPixmap.fromImage(image)
        self._store_in_cache(file_path, pixmap, full_size)

        if file_path != self.image_path:
            return

        self._display_ready_pixmap(file_path, pixmap, full_size)
        self.prefetch_neighbors()

    def on_image_prefetched(self, file_path, image, full_size):
        self.in_flight_prefetches.discard(file_path)
        if self._closing or image.isNull():
            return

        cache = ImageViewerWindow._SHARED_PIXMAP_CACHE
        if file_path not in cache:
            pixmap = QPixmap.fromImage(image)
            self._store_in_cache(file_path, pixmap, full_size)

    def on_full_res_loaded(self, file_path, pixmap):
        if self._closing:
            return
        if file_path == self.image_path:
            self.canvas.full_res_pixmap = pixmap
            self.canvas.update()
            self.update_title_zoom()

    def request_async_full_res(self, file_path):
        """Starts background task to load full resolution image."""
        if not self._closing and file_path and os.path.exists(file_path):
            task = FullResLoaderTask(file_path, self.loader_signals, is_closing_getter=self._is_closing)
            self.thread_pool.start(task)

    def _compute_target_window_size(self, full_size, max_screen_fraction=1.0):
        """Computes the target window size for an image while respecting screen bounds."""
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
        max_w = (screen_geom.width() * max_screen_fraction) - 100
        max_h = (screen_geom.height() * max_screen_fraction) - 100

        target_w = img_w
        target_h = img_h

        if target_w > (max_w - padding_w) or target_h > (max_h - padding_w):
            if img_aspect > ((max_w - padding_w) / (max_h - padding_h)):
                target_w = max_w - padding_w
                target_h = int(target_w / img_aspect)
            else:
                target_h = max_h - padding_h
                target_w = int(target_h * img_aspect)

        final_w = max(400, int(target_w + padding_w))
        final_h = max(300, int(target_h + padding_h))
        return final_w, final_h, screen_geom

    def _apply_size_preserving_center(self, final_w, final_h, screen_geom, max_screen_fraction=1.0):
        """Resizes the window around a stable center point without drifting on repeated navigation."""
        if not self._has_positioned_window:
            self._programmatic_resizing = True
            try:
                self.resize(final_w, final_h)
                center_point = screen_geom.center()
                window_geom = self.frameGeometry()
                window_geom.moveCenter(center_point)
                self.move(window_geom.topLeft())
                self._anchor_center = self.frameGeometry().center()
                self._has_positioned_window = True
            finally:
                self._programmatic_resizing = False
            return

        if self._user_resized:
            return

        # Lock onto the established anchor center so integer half-width/half-height rounding
        # across repeated left/right arrow navigation never accumulates top-left drift.
        if self._anchor_center is None:
            self._anchor_center = self.frameGeometry().center()

        current_geom = self.frameGeometry()
        # Frame offset accounts for non-client/translucent margins if frameGeometry differs from geometry()
        frame_dx = self.geometry().x() - current_geom.x()
        frame_dy = self.geometry().y() - current_geom.y()

        new_top_left_x = round(self._anchor_center.x() - (final_w / 2.0)) + frame_dx
        new_top_left_y = round(self._anchor_center.y() - (final_h / 2.0)) + frame_dy

        self._programmatic_resizing = True
        try:
            self.setGeometry(int(new_top_left_x), int(new_top_left_y), final_w, final_h)
        finally:
            self._programmatic_resizing = False

    def _display_ready_pixmap(self, file_path, pixmap, full_size):
        if self._closing:
            return

        self.full_pixmap_size = full_size

        if full_size.isValid() and not self.isFullScreen():
            final_w, final_h, screen_geom = self._compute_target_window_size(full_size, max_screen_fraction=1.0)
            self._apply_size_preserving_center(final_w, final_h, screen_geom, max_screen_fraction=1.0)

        self.canvas.display_pixmap = pixmap
        self.canvas.image_path = file_path
        self.canvas.full_res_pixmap = None
        self.canvas._full_res_requested_for = None

        self.canvas.reset_fit()
        QTimer.singleShot(10, self.canvas.reset_fit)
        self.update_title_zoom()

    def update_title_zoom(self):
        if self._closing:
            return
        file_title = os.path.basename(self.image_path)
        dim_info = (
            f" ({self.full_pixmap_size.width()}x{self.full_pixmap_size.height()})"
            if self.full_pixmap_size.isValid() else ""
        )

        effective_scale = self.canvas.get_effective_zoom()
        zoom_pct = int(effective_scale * 100)
        self.title_label.setText(f"{file_title}{dim_info} ({zoom_pct}%)")

    def prefetch_neighbors(self):
        if self._closing or not self._prefetch_enabled or len(self.items) <= 1:
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
                self.thread_pool.start(
                    ImagePreloadTask(path, self.loader_signals, is_closing_getter=self._is_closing)
                )

    def closeEvent(self, event):
        self._closing = True
        if self.main_parent and hasattr(self.main_parent, 'workspace_grid'):
            self.main_parent.workspace_grid.selected_file_paths = {self.image_path}
            self.main_parent.workspace_grid.update()
            self.main_parent.on_card_selected(self.image_path, os.path.basename(self.image_path))
            self.main_parent.show()
            if hasattr(self.main_parent, 'scroll_to_selected_thumbnail_in_grid'):
                QTimer.singleShot(0, self.main_parent.scroll_to_selected_thumbnail_in_grid)
            elif hasattr(self.main_parent.workspace_grid, 'scroll_to_selected'):
                QTimer.singleShot(
                    0, lambda: self.main_parent.workspace_grid.scroll_to_selected(only_if_out_of_view=True)
                )
        event.accept()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self._is_focus_mode:
                self.toggle_focus_mode()
            else:
                self.close()
            event.accept()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Plus):
            self.canvas.zoom_by_step(1.15)
            event.accept()
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Minus):
            self.canvas.zoom_by_step(0.85)
            event.accept()
        elif key == Qt.Key.Key_Space:
            self.toggle_fullscreen()
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
        if not self.items or self._closing:
            return

        new_index = self.current_index + direction

        if not self._loop_navigation:
            if new_index < 0 or new_index >= len(self.items):
                return

        self.current_index = new_index % len(self.items)
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

        self.pin_btn = PinIconButton(self.is_always_on_top, self.title_container)
        self.pin_btn.clicked.connect(self.toggle_always_on_top)
        title_layout.addWidget(self.pin_btn)

        file_title = os.path.basename(self.image_path)
        self.title_label = QLabel(f"Loading: {file_title}...")
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setStyleSheet("color: #cbd5e1; font-size: 12px; font-weight: bold; background: transparent;")
        title_layout.addWidget(self.title_label)

        title_layout.addStretch()

        btn_close = QPushButton("x")
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
        self.canvas.zoom_changed.connect(lambda _z: self.update_title_zoom())
        self.canvas.focus_mode_toggled.connect(self.toggle_focus_mode)
        self.canvas.request_full_res.connect(self.request_async_full_res)
        self.container_layout.addWidget(self.canvas, stretch=1)

        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(0, 0, 2, 2)
        bottom_layout.addStretch()

        self.resize_grip = ResizeHandleWidget(self)
        bottom_layout.addWidget(self.resize_grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        self.container_layout.addLayout(bottom_layout)
        self.main_layout.addWidget(self.container)
        self.resize_grip.show()
        self.resize_grip.raise_()

    def _position_corner_grip(self):
        if not hasattr(self, "resize_grip") or self.resize_grip is None:
            return
        if self._is_focus_mode or self.isFullScreen():
            self.resize_grip.hide()
            return
        self.resize_grip.show()
        self.resize_grip.raise_()

    def moveEvent(self, event):
        super().moveEvent(event)
        # Keep the stored center in sync whenever the user drags or moves the window manually.
        if self._has_positioned_window and not self._programmatic_resizing and not self.isFullScreen():
            self._anchor_center = self.frameGeometry().center()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_corner_grip()
        if self._has_positioned_window and not self._programmatic_resizing and not self.isFullScreen():
            self._anchor_center = self.frameGeometry().center()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_corner_grip()

    def toggle_focus_mode(self):
        """Toggles visibility of window canvas elements for a clean borderless image view."""
        if self.isFullScreen():
            return

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
            self._position_corner_grip()

        self.update_title_zoom()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.main_layout.setContentsMargins(8, 8, 8, 8)
            self.container_layout.setContentsMargins(10, 10, 10, 10)
            self.container_layout.setSpacing(8)

            self.title_container.setVisible(True)
            self.resize_grip.setVisible(not self._is_focus_mode)
            self.container.setStyleSheet("""
                QFrame {
                    background-color: #0b0f19;
                    border: 1px solid #1e293b;
                    border-radius: 10px;
                }
            """)
            self.showNormal()
            if not self._is_focus_mode:
                self._position_corner_grip()
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
        if hasattr(self.pin_btn, "set_pinned"):
            self.pin_btn.set_pinned(self.is_always_on_top)
        else:
            self.pin_btn.setText("Pin" if self.is_always_on_top else "Unpin")

        self._apply_window_flags(self.is_always_on_top, show_if_visible=True)
        save_settings(new_values={"viewer_always_on_top": self.is_always_on_top})

    def _get_resize_edge(self, global_pos):
        """Determines which edge or corner the mouse is hovering over using global coordinates."""
        if self._is_focus_mode or self.isFullScreen():
            return None

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

        if top and left:
            return "top_left"
        if top and right:
            return "top_right"
        if bottom and left:
            return "bottom_left"
        if bottom and right:
            return "bottom_right"
        if left:
            return "left"
        if right:
            return "right"
        if top:
            return "top"
        if bottom:
            return "bottom"
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.isFullScreen():
            global_pos = event.globalPosition()
            self._resize_edge = self._get_resize_edge(global_pos)

            if self._resize_edge:
                self._dragging_window = False
                self._drag_pos = global_pos.toPoint()
                self._start_geometry = self.geometry()
                event.accept()
                return

            if not self._is_focus_mode:
                local_pos = self.mapFromGlobal(global_pos.toPoint())
                if local_pos.y() < 40:
                    self._dragging_window = True
                    self._resize_edge = None
                    self.handle_mouse_press(self, event)
                    return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isFullScreen() or self._is_focus_mode:
            super().mouseMoveEvent(event)
            return

        global_pos = event.globalPosition()

        if not self._resize_edge and not getattr(self, "_dragging_window", False):
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

        if self._resize_edge:
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

        if getattr(self, "_dragging_window", False) and (event.buttons() & Qt.MouseButton.LeftButton):
            self.handle_mouse_move(self, event)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = getattr(self, "_dragging_window", False)
            self._resize_edge = None
            self._dragging_window = False
            if hasattr(self, '_drag_pos'):
                del self._drag_pos
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if was_dragging:
                self.handle_mouse_release(event)
                event.accept()
                return

        super().mouseReleaseEvent(event)


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
        self._is_first_open = True
        super().__init__(image_path, items_list, parent=None)
        self.main_parent = parent

        file_title = os.path.basename(image_path)
        self.setWindowTitle(f"Pinned Viewer: {file_title}")

        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._position_corner_grip()

    def _display_ready_pixmap(self, file_path, pixmap, full_size):
        if self._closing:
            return

        self.full_pixmap_size = full_size

        if self._is_first_open and full_size.isValid() and not self.isFullScreen():
            self._is_first_open = False
            final_w, final_h, screen_geom = self._compute_target_window_size(full_size, max_screen_fraction=0.5)
            self._apply_size_preserving_center(final_w, final_h, screen_geom, max_screen_fraction=0.5)

        self.canvas.display_pixmap = pixmap
        self.canvas.image_path = file_path
        self.canvas.full_res_pixmap = None
        self.canvas._full_res_requested_for = None

        self.canvas.reset_fit()
        QTimer.singleShot(10, self.canvas.reset_fit)
        self.update_title_zoom()

    def closeEvent(self, event):
        self._closing = True
        if self.main_parent and hasattr(self.main_parent, 'detached_viewers'):
            if self in self.main_parent.detached_viewers:
                self.main_parent.detached_viewers.remove(self)
            if (
                hasattr(self.main_parent, 'workspace_grid')
                and getattr(self.main_parent.workspace_grid, 'selected_file_paths', None)
            ):
                if hasattr(self.main_parent, 'scroll_to_selected_thumbnail_in_grid'):
                    QTimer.singleShot(0, self.main_parent.scroll_to_selected_thumbnail_in_grid)
                elif hasattr(self.main_parent.workspace_grid, 'scroll_to_selected'):
                    QTimer.singleShot(
                        0, lambda: self.main_parent.workspace_grid.scroll_to_selected(only_if_out_of_view=True)
                    )
        event.accept()
