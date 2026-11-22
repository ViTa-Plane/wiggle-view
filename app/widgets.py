import os

from collections import OrderedDict
from PySide6.QtCore import Qt, QSize, Signal, QRect, QPoint, QTimer, QMimeData, QUrl
from PySide6.QtGui import QColor, QPixmap, QPainter, QFont, QPen, QMouseEvent, QPolygon, QDrag, QBrush
from PySide6.QtWidgets import QWidget, QSizePolicy, QLabel, QPushButton, QHBoxLayout, QVBoxLayout, QApplication
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
        try:
            if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
                return
            if QApplication.instance() is None or QApplication.closingDown():
                return
            parent_win = getattr(self, "parent_window", None)
            if parent_win is not None and bool(getattr(parent_win, "_is_closing", False)):
                return
            if not self.paintEngine() or not self.paintEngine().isActive() and False:
                pass
        except RuntimeError:
            return

        painter = None
        try:
            painter = QPainter(self)
            if not painter.isActive():
                return

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
        except RuntimeError:
            return
        finally:
            if painter is not None and painter.isActive():
                try:
                    painter.end()
                except RuntimeError:
                    pass


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

        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setStyleSheet("""
                    color: #94a3b8; font-family: sans-serif; font-size: 12px; font-weight: bold;border: none;background-color: transparent;
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

    def mouseReleaseEvent(self, event):
        if self.parent_window is not None:
            self.parent_window._dragging_window = False
            if hasattr(self.parent_window, "_locked_pos"):
                self.parent_window._locked_pos = self.parent_window.pos()
        self.handle_mouse_release(event)


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

        self._drag_start_pos = None
        self._pending_click_index = -1
        self._pending_click_path = None
        self.pixmap_cache = OrderedDict()
        self._in_flight_thumbs = set()
        self._queued_thumb_items = {}
        self._thumb_flush_scheduled = False

        self._MAX_PIXMAP_CACHE_ITEMS = 2000
        self.update_cache_limit_from_settings()

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StaticContents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)

    def cache_thumbnail_image(self, file_path, qimage):
        self._in_flight_thumbs.discard(file_path)
        if qimage is None or qimage.isNull():
            return
        safe_qimage = qimage.copy()
        px = QPixmap.fromImage(safe_qimage)
        if not px.isNull():
            if file_path in self.pixmap_cache:
                self.pixmap_cache.move_to_end(file_path)
            self.pixmap_cache[file_path] = px
            if len(self.pixmap_cache) > self._MAX_PIXMAP_CACHE_ITEMS:
                self.pixmap_cache.popitem(last=False)

    def has_cached_pixmap(self, file_path):
        return file_path in self.pixmap_cache

    def mark_thumbnail_in_flight(self, file_path):
        if file_path:
            self._in_flight_thumbs.add(file_path)

    def unmark_thumbnail_in_flight(self, file_path):
        self._in_flight_thumbs.discard(file_path)

    def clear_in_flight_thumbnails(self):
        self._in_flight_thumbs.clear()
        self._queued_thumb_items.clear()
        self._thumb_flush_scheduled = False

    def is_thumbnail_pending(self, file_path):
        return bool(file_path) and (file_path in self.pixmap_cache or file_path in self._in_flight_thumbs)

    def set_cards(self, cards_data):
        self.cards = cards_data
        valid_paths = {item["file_path"] for item in cards_data}
        self.selected_file_paths.intersection_update(valid_paths)
        self.last_clicked_index = None
        self._pending_click_index = -1
        self._pending_click_path = None
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
        self._pending_click_index = -1
        self._pending_click_path = None

        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = event.position().toPoint()

        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            index = self._get_index_at_pos(event.position().toPoint())
            if 0 <= index < len(self.cards):
                clicked_item = self.cards[index]
                path = clicked_item["file_path"]
                modifiers = event.modifiers()

                is_shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
                is_ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
                is_alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
                has_any_modifier = is_shift or is_ctrl or is_alt

                if (
                    event.button() == Qt.MouseButton.LeftButton
                    and not has_any_modifier
                    and path in self.selected_file_paths
                    and len(self.selected_file_paths) > 1
                ):
                    self._pending_click_index = index
                    self._pending_click_path = path
                    self.last_clicked_index = index
                    self.update()
                    super().mousePressEvent(event)
                    return

                if is_shift and self.last_clicked_index is not None:
                    start = min(self.last_clicked_index, index)
                    end = max(self.last_clicked_index, index)
                    if not is_ctrl:
                        self.selected_file_paths.clear()
                    for i in range(start, end + 1):
                        self.selected_file_paths.add(self.cards[i]["file_path"])
                    self.selection_changed.emit(list(self.selected_file_paths))
                elif is_ctrl:
                    if path in self.selected_file_paths:
                        self.selected_file_paths.remove(path)
                    else:
                        self.selected_file_paths.add(path)
                    self.last_clicked_index = index
                    self.selection_changed.emit(list(self.selected_file_paths))
                    if len(self.selected_file_paths) == 1:
                        only_path = next(iter(self.selected_file_paths))
                        only_item = next((c for c in self.cards if c["file_path"] == only_path), clicked_item)
                        self.item_selected.emit(only_path, only_item["display_name"])
                elif not has_any_modifier:
                    self.selected_file_paths = {path}
                    self.last_clicked_index = index
                    self.selection_changed.emit(list(self.selected_file_paths))
                    self.item_selected.emit(path, clicked_item["display_name"])
                else:
                    self.last_clicked_index = index
                    self.selection_changed.emit(list(self.selected_file_paths))
                self.update()
            else:
                if event.button() == Qt.MouseButton.LeftButton:
                    self.deselect_all()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        self._pending_click_index = -1
        self._pending_click_path = None
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

    def mouseMoveEvent(self, event):
        if (
            not (event.buttons() & Qt.MouseButton.LeftButton)
            or self._drag_start_pos is None
        ):
            return super().mouseMoveEvent(event)

        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)

        self._pending_click_index = -1
        self._pending_click_path = None

        clicked_idx = self._get_index_at_pos(self._drag_start_pos)
        drag_paths = []
        if 0 <= clicked_idx < len(self.cards):
            clicked_path = self.cards[clicked_idx]["file_path"]
            if clicked_path in self.selected_file_paths:
                drag_paths = [item["file_path"] for item in self.cards if item["file_path"] in self.selected_file_paths]
                for p in self.selected_file_paths:
                    if p not in drag_paths:
                        drag_paths.append(p)
            else:
                drag_paths = [clicked_path]
        elif self.selected_file_paths:
            drag_paths = [item["file_path"] for item in self.cards if item["file_path"] in self.selected_file_paths]
            for p in self.selected_file_paths:
                if p not in drag_paths:
                    drag_paths.append(p)

        drag_paths = [p for p in drag_paths if p and os.path.exists(p)]
        if not drag_paths:
            return

        self._drag_start_pos = None

        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(p) for p in drag_paths])
        mime_data.setData("application/x-wiggleview-cards", b"1")

        drag = QDrag(self)
        drag.setMimeData(mime_data)

        preview = self.pixmap_cache.get(drag_paths[0]) if hasattr(self, "pixmap_cache") else None
        if isinstance(preview, QPixmap) and not preview.isNull():
            drag.setPixmap(
                preview.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            )

        copy_pixmap = self._build_drag_cursor_pixmap("copy")
        if copy_pixmap is not None and not copy_pixmap.isNull():
            drag.setDragCursor(copy_pixmap, Qt.DropAction.CopyAction)

        move_pixmap = self._build_drag_cursor_pixmap("move")
        if move_pixmap is not None and not move_pixmap.isNull():
            drag.setDragCursor(move_pixmap, Qt.DropAction.MoveAction)

        ignore_pixmap = self._build_drag_cursor_pixmap("ignore")
        if ignore_pixmap is not None and not ignore_pixmap.isNull():
            drag.setDragCursor(ignore_pixmap, Qt.DropAction.IgnoreAction)

        initial_action = (
            Qt.DropAction.CopyAction
            if (event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            else Qt.DropAction.MoveAction
        )
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, initial_action)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pending_click_path is not None:
            pending_path = self._pending_click_path
            pending_idx = self._pending_click_index
            self._pending_click_index = -1
            self._pending_click_path = None
            self._drag_start_pos = None
            if 0 <= pending_idx < len(self.cards) and self.cards[pending_idx]["file_path"] == pending_path:
                clicked_item = self.cards[pending_idx]
                self.selected_file_paths = {pending_path}
                self.last_clicked_index = pending_idx
                self.selection_changed.emit(list(self.selected_file_paths))
                self.item_selected.emit(pending_path, clicked_item["display_name"])
                self.update()
            super().mouseReleaseEvent(event)
            return

        self._pending_click_index = -1
        self._pending_click_path = None
        self._drag_start_pos = None
        super().mouseReleaseEvent(event)

    def _build_drag_cursor_pixmap(self, mode="copy"):
        """Builds custom 32x32 drag cursor QPixmaps ('copy', 'move', 'ignore') with explicit QPoint instances."""
        cache = getattr(self, "_cached_drag_cursor_pixmaps", None)
        if cache is None:
            cache = {}
            self._cached_drag_cursor_pixmaps = cache
        if mode in cache:
            return cache[mode]

        pm = QPixmap(32, 32)
        pm.fill(Qt.GlobalColor.transparent)

        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            arrow = QPolygon([
                QPoint(1, 1),
                QPoint(1, 19),
                QPoint(5, 15),
                QPoint(8, 22),
                QPoint(11, 21),
                QPoint(8, 14),
                QPoint(14, 14),
            ])
            p.setPen(QPen(QColor("#0f172a"), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.setBrush(QBrush(QColor("#f8fafc")))
            p.drawPolygon(arrow)

            badge_x, badge_y, badge_size = 14, 15, 15
            cx = badge_x + badge_size // 2
            cy = badge_y + badge_size // 2

            if mode == "copy":
                # Green badge with '+'
                p.setPen(QPen(QColor("#ffffff"), 1.5))
                p.setBrush(QBrush(QColor("#16a34a")))
                p.drawRoundedRect(badge_x, badge_y, badge_size, badge_size, 3.5, 3.5)

                p.setPen(QPen(QColor("#ffffff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(cx - 4, cy, cx + 4, cy)
                p.drawLine(cx, cy - 4, cx, cy + 4)

            elif mode == "move":
                # Blue badge with right arrow '->'
                p.setPen(QPen(QColor("#ffffff"), 1.5))
                p.setBrush(QBrush(QColor("#2563eb")))
                p.drawRoundedRect(badge_x, badge_y, badge_size, badge_size, 3.5, 3.5)

                p.setPen(QPen(QColor("#ffffff"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawLine(cx - 4, cy, cx + 4, cy)
                p.drawLine(cx + 1, cy - 3, cx + 4, cy)
                p.drawLine(cx + 1, cy + 3, cx + 4, cy)

            else:
                # Red circular denial badge with diagonal slash
                p.setPen(QPen(QColor("#ffffff"), 1.5))
                p.setBrush(QBrush(QColor("#dc2626")))
                p.drawEllipse(badge_x, badge_y, badge_size, badge_size)

                p.setPen(QPen(QColor("#ffffff"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(cx - 4, cy + 4, cx + 4, cy - 4)
        finally:
            if p.isActive():
                p.end()

        cache[mode] = pm
        return pm

    def _get_grid_metrics(self, width=None):
        if width is None:
            width = self.width()
        cols = max(1, width // self.cell_width)
        used_width = cols * self.cell_width
        left_margin = max(0, (width - used_width) // 2)
        return cols, left_margin

    def _get_index_at_pos(self, pos: QPoint):
        cols, left_margin = self._get_grid_metrics()
        rel_x = pos.x() - left_margin
        if rel_x < 0 or rel_x >= cols * self.cell_width:
            return -1
        col = int(rel_x // self.cell_width)
        row = int(pos.y() // self.cell_height)
        return row * cols + col

    def paintEvent(self, event):
        try:
            if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
                return
        except RuntimeError:
            return

        painter = QPainter(self)
        if not painter.isActive():
            return

        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

            painter.fillRect(self.rect(), QColor("#0b0f19"))

            width = self.width()
            cols, left_margin = self._get_grid_metrics(width)
            if cols <= 0 or not self.cards:
                return

            parent_widget = self.parent()
            scroll_area = parent_widget.parent() if isinstance(parent_widget, QWidget) else None
            if scroll_area is not None and hasattr(scroll_area, "viewport") and hasattr(scroll_area, "verticalScrollBar"):
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

            main_window = self.window()
            has_worker_pool = (
                main_window is not None
                and hasattr(main_window, "thread_pool")
                and hasattr(main_window, "thumbnail_signals")
            )

            for row in range(start_row, end_row):
                for col in range(cols):
                    index = row * cols + col
                    if index >= len(self.cards):
                        break

                    item = self.cards[index]
                    file_path = item["file_path"]
                    x = left_margin + col * self.cell_width + 6
                    y = row * self.cell_height + 6

                    card_rect = QRect(x, y, card_w, card_h)
                    is_selected = file_path in self.selected_file_paths

                    painter.setBrush(card_bg_selected if is_selected else card_bg_normal)
                    painter.setPen(border_selected if is_selected else border_normal)
                    painter.drawRoundedRect(card_rect, 8, 8)

                    img_x = x + (card_w - self.image_size) // 2
                    img_y = y + 8

                    px = self.pixmap_cache.get(file_path)
                    if isinstance(px, QPixmap) and not px.isNull():
                        self.pixmap_cache.move_to_end(file_path)
                        draw_x = img_x + (self.image_size - px.width()) // 2
                        draw_y = img_y + (self.image_size - px.height()) // 2
                        painter.drawPixmap(draw_x, draw_y, px)
                    else:
                        painter.fillRect(QRect(img_x, img_y, self.image_size, self.image_size), placeholder_bg)

                        if has_worker_pool and not self.is_thumbnail_pending(file_path):
                            if not hasattr(self, "_queued_thumb_items"):
                                self._queued_thumb_items = {}
                            if not hasattr(self, "_thumb_flush_scheduled"):
                                self._thumb_flush_scheduled = False

                            if file_path not in self._queued_thumb_items:
                                self._queued_thumb_items[file_path] = item.get("thumb_path")

                            if not self._thumb_flush_scheduled:
                                self._thumb_flush_scheduled = True
                                QTimer.singleShot(0, self._flush_queued_thumbnail_tasks)

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
        finally:
            if painter.isActive():
                painter.end()

    def _flush_queued_thumbnail_tasks(self):
        self._thumb_flush_scheduled = False
        queued = getattr(self, "_queued_thumb_items", None)
        if not queued:
            return
        pending = dict(queued)
        queued.clear()

        try:
            if not self.isVisible():
                return
            main_window = self.window()
        except RuntimeError:
            return

        if main_window is None or not hasattr(main_window, "thread_pool") or not hasattr(main_window, "thumbnail_signals"):
            return

        active_scan_getter = (
            getattr(main_window, "get_active_scan_id", None)
            or getattr(main_window, "_get_active_scan_id", None)
        )
        current_scan_id = getattr(main_window, "scan_id", 0)

        for fp, existing_tp in pending.items():
            if self.is_thumbnail_pending(fp):
                continue
            self.mark_thumbnail_in_flight(fp)
            task = ThumbnailLoaderTask(
                fp,
                getattr(main_window, "thumb_cache_dir", None),
                self.image_size,
                main_window.thumbnail_signals,
                scan_id=current_scan_id,
                active_scan_getter=active_scan_getter,
                existing_thumb_path=existing_tp,
            )
            main_window.thread_pool.start(task)

    def _find_scroll_area(self):
        parent = self.parent()
        while parent is not None:
            if hasattr(parent, "verticalScrollBar") and hasattr(parent, "viewport"):
                return parent
            parent = parent.parent()
        return None

    def scroll_to_selected(self, only_if_out_of_view=False):
        if not self.selected_file_paths or not self.cards:
            return False

        cols, _ = self._get_grid_metrics()
        if cols <= 0:
            return False

        target_index = None
        for idx, item in enumerate(self.cards):
            if item.get("file_path") in self.selected_file_paths:
                target_index = idx
                break

        if target_index is None:
            return False

        row = target_index // cols
        card_top = row * self.cell_height
        card_bottom = card_top + self.cell_height

        scroll_area = self._find_scroll_area()
        if scroll_area is None:
            return False

        v_bar = scroll_area.verticalScrollBar()
        viewport_h = scroll_area.viewport().height()
        view_top = v_bar.value()
        view_bottom = view_top + viewport_h

        is_visible = (card_top >= view_top) and (card_bottom <= view_bottom)
        if only_if_out_of_view and is_visible:
            return False

        if card_top < view_top:
            v_bar.setValue(max(0, card_top - 6))
        elif card_bottom > view_bottom:
            v_bar.setValue(min(v_bar.maximum(), max(0, card_bottom - viewport_h + 6)))

        return True

    def clear_cache_if_needed(self, max_allowed=3000):
        while len(self.pixmap_cache) > max_allowed:
            self.pixmap_cache.popitem(last=False)

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
        total_bytes = 0
        for px in self.pixmap_cache.values():
            if not px.isNull():
                total_bytes += (px.width() * px.height() * 4) + 512
        return total_bytes