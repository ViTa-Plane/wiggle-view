from collections import OrderedDict
from PySide6.QtCore import Qt, QSize, Signal, QRect, QPoint, QTimer
from PySide6.QtGui import QColor, QPixmap, QPainter, QFont, QPen, QMouseEvent, QPolygon
from PySide6.QtWidgets import QWidget, QSizePolicy, QLabel, QPushButton, QHBoxLayout, QVBoxLayout
from .helpers import load_settings
from .workers import ThumbnailLoaderTask

class WindowDragHandler:
    def __init__(self):
        self._drag_pos = QPoint()

    def handle_mouse_press(self, window, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            event.accept()
            return True
        return False

    def handle_mouse_move(self, window, event):
        if event.buttons() == Qt.MouseButton.LeftButton and not window.isMaximized():
            window.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def handle_mouse_release(self, event):
        pass


class ResizeHandleWidget(QWidget):
    def __init__(self, parent_window):
        super().__init__()
        self.parent_window = parent_window
        self.setFixedSize(16, 16)
        self.hovered = False

        self._resizing = False
        self._resize_start_geometry = None
        self._resize_start_global_pos = None

    def enterEvent(self, event):
        self.hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self.parent_window.isMaximized():
            self._resizing = True
            self._resize_start_geometry = self.parent_window.frameGeometry()
            self._resize_start_global_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._resizing and event.buttons() == Qt.MouseButton.LeftButton:
            diff = event.globalPosition().toPoint() - self._resize_start_global_pos
            geom = self._resize_start_geometry

            w = max(self.parent_window.minimumWidth(), geom.width() + diff.x())
            h = max(self.parent_window.minimumHeight(), geom.height() + diff.y())

            self.parent_window.setGeometry(geom.x(), geom.y(), w, h)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._resizing = False
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = QColor("#38bdf8") if self.hovered else QColor("#64748b")
        pen = QPen(color, 1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)

        triangle = QPolygon([
            QPoint(15, 15),
            QPoint(15, 5),
            QPoint(5, 15)
        ])
        painter.drawPolygon(triangle)


class TitleBar(QWidget, WindowDragHandler):
    def __init__(self, title_text, parent_window, show_min_max=False):
        super().__init__()
        WindowDragHandler.__init__(self)
        self.parent_window = parent_window
        self.show_min_max = show_min_max

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 0)

        h_layout = QHBoxLayout()
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(4)

        self.title_label = QLabel(title_text)
        self.title_label.setObjectName("TitleLabel")
        self.title_label.setStyleSheet("""
            color: #94a3b8; font-family: sans-serif; font-size: 12px; font-weight: bold; background-color: transparent;
        """)
        h_layout.addWidget(self.title_label)
        h_layout.addStretch()

        if self.show_min_max:
            self.min_btn = QPushButton("—")
            self.min_btn.setFixedSize(28, 28)
            self.min_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.min_btn.setStyleSheet("""
                QPushButton { background-color: transparent; color: #94a3b8; border: none; border-radius: 6px; font-weight: bold; }
                QPushButton:hover { background-color: #1e293b; color: #ffffff; }
            """)
            self.min_btn.clicked.connect(self.parent_window.showMinimized)
            h_layout.addWidget(self.min_btn)

            self.max_btn = QPushButton("□")
            self.max_btn.setFixedSize(28, 28)
            self.max_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.max_btn.setStyleSheet("""
                QPushButton { background-color: transparent; color: #94a3b8; border: none; border-radius: 6px; font-weight: bold; }
                QPushButton:hover { background-color: #1e293b; color: #ffffff; }
            """)
            self.max_btn.clicked.connect(self.toggle_maximize)
            h_layout.addWidget(self.max_btn)

        self.close_btn = QPushButton("x")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton { background-color: transparent; color: #94a3b8; border: none; border-radius: 6px; font-weight: bold; }
            QPushButton:hover { background-color: #c42b1c; color: #ffffff; }
        """)
        self.close_btn.clicked.connect(self.parent_window.close)
        h_layout.addWidget(self.close_btn)

        layout.addLayout(h_layout)

    def toggle_maximize(self):
        if self.parent_window.isMaximized():
            self.parent_window.showNormal()
            self.max_btn.setText("□")
        else:
            self.parent_window.showMaximized()
            self.max_btn.setText("❐")

    def mousePressEvent(self, event):
        self.handle_mouse_press(self.parent_window, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self.parent_window, event)

    def mouseDoubleClickEvent(self, event):
        if self.show_min_max and event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()


class ResponsiveGridWidget(QWidget):
    item_selected = Signal(str, str)
    selection_changed = Signal(list)
    item_double_clicked = Signal(str)

    def __init__(self, cell_width=200, cell_height=220, image_size=170, parent=None):
        super().__init__(parent)
        self.cell_width = cell_width
        self.cell_height = cell_height
        self.image_size = image_size
        self.cards = []
        self.selected_file_paths = set()
        self.last_clicked_index = None

        # Keyed by file_path -> QPixmap (pre-decoded in worker threads, zero disk I/O in paintEvent)
        self.pixmap_cache = OrderedDict()

        # Initialize max cache limit from settings on startup instead of hardcoding
        self._MAX_PIXMAP_CACHE_ITEMS = 2000
        self.update_cache_limit_from_settings()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StaticContents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)

    def cache_thumbnail_image(self, file_path, qimage):
        """Converts a thread-decoded QImage into a QPixmap on the GUI thread and stores it in the LRU cache."""
        if qimage is None or qimage.isNull():
            return
        px = QPixmap.fromImage(qimage)
        if not px.isNull():
            if file_path in self.pixmap_cache:
                self.pixmap_cache.move_to_end(file_path)
            self.pixmap_cache[file_path] = px
            if len(self.pixmap_cache) > self._MAX_PIXMAP_CACHE_ITEMS:
                self.pixmap_cache.popitem(last=False)

    def has_cached_pixmap(self, file_path):
        return file_path in self.pixmap_cache

    def set_cards(self, cards_data):
        self.cards = cards_data
        valid_paths = {item["file_path"] for item in cards_data}
        self.selected_file_paths.intersection_update(valid_paths)
        self.last_clicked_index = None
        self.updateGeometry()
        self.update()

    def select_all(self):
        self.selected_file_paths = {item["file_path"] for item in self.cards}
        self.last_clicked_index = 0 if self.cards else None
        self.update()
        self.selection_changed.emit(list(self.selected_file_paths))

    def deselect_all(self):
        self.selected_file_paths.clear()
        self.last_clicked_index = None
        self.update()
        self.selection_changed.emit([])

    def invert_selection(self):
        all_paths = {item["file_path"] for item in self.cards}
        self.selected_file_paths = all_paths - self.selected_file_paths
        self.last_clicked_index = None
        self.update()
        self.selection_changed.emit(list(self.selected_file_paths))

    def sizeHint(self):
        parent_width = self.parent().width() if self.parent() else 800
        cols = max(1, parent_width // self.cell_width)
        rows = (len(self.cards) + cols - 1) // cols
        total_height = rows * self.cell_height
        return QSize(parent_width, max(total_height, 100))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updateGeometry()

    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            index = self._get_index_at_pos(event.position().toPoint())
            if 0 <= index < len(self.cards):
                clicked_item = self.cards[index]
                path = clicked_item["file_path"]
                modifiers = event.modifiers()

                is_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                is_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

                # If right-clicking an item that is already part of an existing multi-selection,
                # preserve the selection so context actions apply to all selected items.
                if event.button() == Qt.MouseButton.RightButton and path in self.selected_file_paths:
                    pass
                elif is_shift and self.last_clicked_index is not None:
                    start = min(self.last_clicked_index, index)
                    end = max(self.last_clicked_index, index)
                    if not is_ctrl:
                        self.selected_file_paths.clear()
                    for i in range(start, end + 1):
                        self.selected_file_paths.add(self.cards[i]["file_path"])
                elif is_ctrl:
                    if path in self.selected_file_paths:
                        self.selected_file_paths.remove(path)
                    else:
                        self.selected_file_paths.add(path)
                    self.last_clicked_index = index
                else:
                    self.selected_file_paths = {path}
                    self.last_clicked_index = index

                self.selection_changed.emit(list(self.selected_file_paths))
                if len(self.selected_file_paths) == 1 and not is_shift and not is_ctrl:
                    self.item_selected.emit(path, clicked_item["display_name"])
                self.update()
            else:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.deselect_all()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._get_index_at_pos(event.position().toPoint())
            if 0 <= index < len(self.cards):
                clicked_item = self.cards[index]
                path = clicked_item["file_path"]
                self.selected_file_paths = {path}
                self.last_clicked_index = index
                self.selection_changed.emit(list(self.selected_file_paths))
                self.update()
                self.item_double_clicked.emit(path)
        super().mouseDoubleClickEvent(event)

    def _get_index_at_pos(self, pos: QPoint):
        width = self.width()
        cols = max(1, width // self.cell_width)
        col = int(pos.x() // self.cell_width)
        row = int(pos.y() // self.cell_height)
        return row * cols + col

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        painter.fillRect(self.rect(), QColor("#0b0f19"))

        width = self.width()
        cols = max(1, width // self.cell_width)
        if cols <= 0 or not self.cards:
            return

        scroll_area = self.parent().parent() if isinstance(self.parent(), QWidget) else None
        if hasattr(scroll_area, "viewport"):
            viewport_rect = scroll_area.viewport().rect()
            visible_top = scroll_area.verticalScrollBar().value()
            visible_bottom = visible_top + viewport_rect.height()
        else:
            visible_top = 0
            visible_bottom = self.height()

        card_w = self.cell_width - 12
        card_h = self.cell_height - 12

        start_row = max(0, int(visible_top // self.cell_height) - 1)
        end_row = min((len(self.cards) + cols - 1) // cols, int(visible_bottom // self.cell_height) + 2)

        card_bg_normal = QColor("#131b2e")
        card_bg_selected = QColor("#1e293b")
        border_normal = QPen(QColor("#1e293b"), 1)
        border_selected = QPen(QColor("#3b82f6"), 2)
        placeholder_bg = QColor("#0d1321")

        font_normal = QFont("sans-serif", 10, QFont.Weight.Medium)
        font_bold = QFont("sans-serif", 10, QFont.Weight.Bold)
        badge_font = QFont("sans-serif", 8, QFont.Weight.Bold)

        for row in range(start_row, end_row):
            for col in range(cols):
                index = row * cols + col
                if index >= len(self.cards):
                    break

                item = self.cards[index]
                file_path = item["file_path"]
                x = col * self.cell_width + 6
                y = row * self.cell_height + 6

                card_rect = QRect(x, y, card_w, card_h)
                is_selected = file_path in self.selected_file_paths

                painter.setBrush(card_bg_selected if is_selected else card_bg_normal)
                painter.setPen(border_selected if is_selected else border_normal)
                painter.drawRoundedRect(card_rect, 8, 8)

                img_x = x + (card_w - self.image_size) // 2
                img_y = y + 8

                px = self.pixmap_cache.get(file_path)
                if px is not None and not px.isNull():
                    self.pixmap_cache.move_to_end(file_path)
                    painter.drawPixmap(img_x, img_y, px)
                else:
                    # Lightweight placeholder while background worker decodes/generates thumbnail
                    painter.fillRect(QRect(img_x, img_y, self.image_size, self.image_size), placeholder_bg)

                    # --- Auto-reload evicted or missing thumbnail on viewport exposure ---
                    main_window = self.window()
                    if hasattr(main_window, 'thread_pool') and hasattr(main_window, 'thumbnail_signals'):
                        if not hasattr(self, '_pending_reloads'):
                            self._pending_reloads = set()

                        if file_path not in self._pending_reloads:
                            self._pending_reloads.add(file_path, )
                            # Clear pending flag after scheduling
                            QTimer.singleShot(100, lambda fp=file_path: self._pending_reloads.discard(fp))

                            task = ThumbnailLoaderTask(
                                file_path,
                                main_window.thumb_cache_dir,
                                self.image_size,
                                main_window.thumbnail_signals,
                                scan_id=getattr(main_window, 'scan_id', 0),
                                active_scan_getter=getattr(main_window, '_get_active_scan_id', None),
                                existing_thumb_path=item.get("thumb_path")
                            )
                            main_window.thread_pool.start(task)
                    # ------------------------------------------------------------------

                # ComfyUI Badge Overlay
                if item.get("has_comfy"):
                    badge_text = "ComfyUI"
                    painter.setFont(badge_font)
                    text_width = painter.fontMetrics().horizontalAdvance(badge_text)
                    badge_w = text_width + 12
                    badge_h = 18
                    badge_x = img_x + self.image_size - badge_w - 6
                    badge_y = img_y + 6
                    badge_rect = QRect(badge_x, badge_y, badge_w, badge_h)

                    painter.setBrush(QColor(15, 23, 42, 220))
                    painter.setPen(QPen(QColor("#38bdf8"), 1))
                    painter.drawRoundedRect(badge_rect, 4, 4)

                    painter.setPen(QColor("#f8fafc"))
                    painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

                painter.setPen(QColor("#ffffff" if is_selected else "#cbd5e1"))
                painter.setFont(font_bold if is_selected else font_normal)

                text_rect = QRect(x + 6, y + card_h - 32, card_w - 12, 26)
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, item.get("display_name", ""))

    def clear_cache_if_needed(self, max_allowed=3000):
        """Trims the pixmap cache if it exceeds safety limits during heavy usage."""
        while len(self.pixmap_cache) > max_allowed:
            self.pixmap_cache.popitem(last=False)

    # RAM cache limits
    def update_cache_limit_from_settings(self):
        settings = load_settings()
        cache_setting = settings.get("max_thumbnail_cache", "Auto (System RAM)")

        if "Auto" in cache_setting or not cache_setting:
            from .helpers import get_optimal_thumbnail_cache_limit
            self._MAX_PIXMAP_CACHE_ITEMS = get_optimal_thumbnail_cache_limit()
        elif "250" in cache_setting:
            self._MAX_PIXMAP_CACHE_ITEMS = 250
        elif "500" in cache_setting:
            self._MAX_PIXMAP_CACHE_ITEMS = 500
        elif "1,000" in cache_setting:
            self._MAX_PIXMAP_CACHE_ITEMS = 1000
        elif "5,000" in cache_setting:
            self._MAX_PIXMAP_CACHE_ITEMS = 5000
        elif "10,000" in cache_setting:
            self._MAX_PIXMAP_CACHE_ITEMS = 10000
        else:
            self._MAX_PIXMAP_CACHE_ITEMS = 2000

        print(f"MAX_PIXMAP_CACHE_ITEMS: {self._MAX_PIXMAP_CACHE_ITEMS}")


    def get_cache_memory_usage(self):
        """Calculates the total approximate RAM usage of all cached QPixmaps in bytes."""
        total_bytes = 0
        for px in self.pixmap_cache.values():
            if not px.isNull():
                # width * height * 4 bytes per pixel (ARGB32) + basic QPixmap overhead
                total_bytes += (px.width() * px.height() * 4) + 512
        return total_bytes