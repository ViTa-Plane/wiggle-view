from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QSpinBox, QLabel, QPushButton,
    QSlider, QFrame, QGraphicsDropShadowEffect, QCheckBox, QComboBox, QFormLayout,
    QTabWidget, QPlainTextEdit, QMessageBox
)
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QColor
from .widgets import (
    WindowDragHandler, TitleBar,
    ResizeHandleWidget
)

from .helpers import load_settings, save_settings
from . import __version__


def _configure_stable_combobox(combo, dialog=None):
    """Keeps default QComboBox popup view intact while anchoring dialog position on popup show/hide."""
    combo.setMaxVisibleItems(8)
    if dialog is not None:
        view = combo.view()
        popup_win = view.window() if view is not None else None
        if popup_win is not None:
            popup_win.installEventFilter(dialog)


class AboutDialog(QDialog, WindowDragHandler):
    """Standalone fixed-size About dialog class with description and clickable GitHub link."""

    def __init__(self, parent=None):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.setFixedSize(440, 340)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._setup_ui()

    def done(self, result):
        if hasattr(self, "container") and self.container is not None:
            self.container.setGraphicsEffect(None)
        super().done(result)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.container = QFrame(self)
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
        container_layout.setContentsMargins(16, 16, 16, 16)
        container_layout.setSpacing(14)

        container_layout.addWidget(TitleBar("About WiggleView", self, show_min_max=False))

        self.content_label = QLabel()
        self.content_label.setWordWrap(True)
        self.content_label.setOpenExternalLinks(True)
        self.content_label.setText(
            f"<b>WiggleView</b> alpha {__version__}<br><br>"
            "image gallery and ref image viewer with "
            "high-performance multithreaded scanning, custom window controls, "
            "smooth canvas zooming & panning.<br><br>"
            "<b>GitHub Repository:</b><br>"
            "<a href='https://github.com/ViTa-Plane/wiggle-view' style='color: #3b82f6; text-decoration: none;'>https://github.com/ViTa-Plane/wiggle-view</a>"
        )
        self.content_label.setStyleSheet("""
            QLabel {
                background-color: #05070a;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 12px;
                font-size: 12px;
                font-family: sans-serif;
            }
        """)
        container_layout.addWidget(self.content_label, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        okay_btn = QPushButton("Okay")
        okay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        okay_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 24px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
        """)
        okay_btn.clicked.connect(self.accept)
        btn_layout.addWidget(okay_btn)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)


class BatchCompleteDialog(QDialog, WindowDragHandler):
    """Borderless dark-theme styled confirmation dialog for completed batch operations."""

    def __init__(self, parent=None, title="Batch Operation Complete", message="Batch processing completed successfully!"):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.setFixedSize(400, 200)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.title_text = title
        self.message_text = message
        self._setup_ui()

    def done(self, result):
        if hasattr(self, "container") and self.container is not None:
            self.container.setGraphicsEffect(None)
        super().done(result)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.container = QFrame(self)
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
        container_layout.setContentsMargins(16, 16, 16, 16)
        container_layout.setSpacing(14)

        container_layout.addWidget(TitleBar(self.title_text, self, show_min_max=False))

        self.msg_label = QLabel(self.message_text)
        self.msg_label.setWordWrap(True)
        self.msg_label.setStyleSheet("""
            QLabel {
                background-color: #05070a; color: #e2e8f0; border: 1px solid #1e293b;
                border-radius: 6px; padding: 12px; font-size: 12px; font-family: sans-serif;
            }
        """)
        container_layout.addWidget(self.msg_label, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        ok_btn = QPushButton("OK")
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white; border: none;
                border-radius: 6px; padding: 8px 24px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background-color: #2563eb; }
        """)
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)


class BatchConvertDialog(QDialog, WindowDragHandler):
    """Borderless dark-theme styled dialog for selecting batch conversion formats, quality, and options."""

    def __init__(self, parent=None):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.setFixedSize(420, 320)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._setup_ui()

    def done(self, result):
        if hasattr(self, "container") and self.container is not None:
            self.container.setGraphicsEffect(None)
        super().done(result)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.container = QFrame(self)
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
        container_layout.setContentsMargins(16, 0, 16, 16)
        container_layout.setSpacing(12)

        container_layout.addWidget(TitleBar("Batch Convert Options", self, show_min_max=False))

        form_layout = QHBoxLayout()
        format_label = QLabel("Target Format:")
        format_label.setStyleSheet("color: #e2e8f0; font-size: 12px; font-weight: bold; border: none;")

        self.format_combo = QComboBox()
        self.format_combo.addItems(["JPG", "PNG", "WEBP"])
        self.format_combo.setCurrentText("JPG")
        self.format_combo.setStyleSheet("""
            QComboBox {
                background-color: #05070a; color: #e2e8f0; border: 1px solid #1e293b;
                border-radius: 6px; padding: 6px 12px; font-size: 12px;
            }
            QComboBox::drop-down { border: 0px; }
            QComboBox QAbstractItemView {
                background-color: #0d1321; color: #e2e8f0; selection-background-color: #3b82f6;
            }
        """)
        _configure_stable_combobox(self.format_combo, self)
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        form_layout.addWidget(format_label)
        form_layout.addWidget(self.format_combo, stretch=1)
        container_layout.addLayout(form_layout)

        self.quality_layout = QVBoxLayout()
        qual_header_layout = QHBoxLayout()
        self.qual_title_label = QLabel("Quality (JPEG / WEBP):")
        self.qual_title_label.setStyleSheet("color: #e2e8f0; font-size: 12px; font-weight: bold; border: none;")
        self.qual_val_label = QLabel("85")
        self.qual_val_label.setStyleSheet("color: #3b82f6; font-size: 12px; font-weight: bold;")
        qual_header_layout.addWidget(self.qual_title_label)
        qual_header_layout.addStretch()
        qual_header_layout.addWidget(self.qual_val_label)
        self.quality_layout.addLayout(qual_header_layout)

        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(85)
        self.quality_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #1e293b; height: 6px; background: #05070a; border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #3b82f6; border: none; width: 14px; margin: -4px 0; border-radius: 7px;
            }
            QSlider::handle:horizontal:hover { background: #2563eb; }
        """)
        self.quality_slider.valueChanged.connect(lambda v: self.qual_val_label.setText(str(v)))
        self.quality_layout.addWidget(self.quality_slider)
        container_layout.addLayout(self.quality_layout)

        checkbox_layout = QVBoxLayout()
        checkbox_layout.setSpacing(8)

        checkbox_style = """
            QCheckBox {
                color: #e2e8f0; font-size: 12px; spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px; height: 16px; border-radius: 4px;
                border: 1px solid #1e293b; background-color: #05070a;
            }
            QCheckBox::indicator:checked {
                background-color: #3b82f6; border-color: #3b82f6;
            }
        """

        self.transfer_exif_cb = QCheckBox("Transfer EXIF data")
        self.transfer_exif_cb.setChecked(True)
        self.transfer_exif_cb.setStyleSheet(checkbox_style)
        checkbox_layout.addWidget(self.transfer_exif_cb)

        self.remove_original_cb = QCheckBox("Remove original")
        self.remove_original_cb.setChecked(False)
        self.remove_original_cb.setStyleSheet(checkbox_style)
        checkbox_layout.addWidget(self.remove_original_cb)

        container_layout.addLayout(checkbox_layout)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #1e293b; color: #cbd5e1; border: none;
                border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background-color: #334155; }
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        convert_btn = QPushButton("Convert")
        convert_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        convert_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6; color: white; border: none;
                border-radius: 6px; padding: 8px 24px; font-weight: bold; font-size: 12px;
            }
            QPushButton:hover { background-color: #2563eb; }
        """)
        convert_btn.clicked.connect(self.accept)
        btn_layout.addWidget(convert_btn)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def _on_format_changed(self, fmt):
        is_lossy = fmt in ["JPG", "WEBP"]
        self.qual_title_label.setEnabled(is_lossy)
        self.qual_val_label.setEnabled(is_lossy)
        self.quality_slider.setEnabled(is_lossy)
        if not is_lossy:
            self.qual_val_label.setText("N/A (Lossless)")
        else:
            self.qual_val_label.setText(str(self.quality_slider.value()))

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)


class SettingsDialog(QDialog, WindowDragHandler):
    """Standalone resizable settings window featuring categorized tabs and persistent JSON storage."""

    def __init__(self, parent=None):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.resize(580, 580)
        self.setMinimumSize(580, 580)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._locked_pos = None
        self._setup_ui()
        self.load_current_settings()

    def showEvent(self, event):
        super().showEvent(event)
        self._locked_pos = self.pos()

    # Keep self._locked_pos updated during/after a drag and only restore _locked_pos when a combobox popup is actually open
    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            if not getattr(self, "_dragging_window", False) and self._locked_pos is not None:
                QTimer.singleShot(0, self._restore_locked_pos)
        return super().eventFilter(watched, event)

    def _restore_locked_pos(self):
        if getattr(self, "_dragging_window", False) or self._locked_pos is None:
            return
        if self.isVisible() and self.pos() != self._locked_pos:
            self.move(self._locked_pos)

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "_dragging_window", False):
            self._locked_pos = self.pos()
    # --------------------------------------------------------

    def done(self, result):
        self._locked_pos = None
        if hasattr(self, "container") and self.container is not None:
            self.container.setGraphicsEffect(None)
        super().done(result)

    def _on_disk_limit_changed(self, text):
        """Disables disk-dependent cache checkboxes when disk cache is disabled."""
        is_enabled = (text != "Disabled")
        self.purge_thumb_chk.setEnabled(is_enabled)
        self.encrypt_thumb_chk.setEnabled(is_enabled)

        disabled_style = """
            QCheckBox:disabled { color: #475569; }
            QCheckBox::indicator:disabled { background-color: #0b0f19; border-color: #1e293b; }
        """
        style = "" if is_enabled else disabled_style
        self.purge_thumb_chk.setStyleSheet(style)
        self.encrypt_thumb_chk.setStyleSheet(style)

    def _on_include_subfolders_toggled(self, checked):
        self.folder_depth_spin.setEnabled(checked)

    def _on_delete_favorites_list_clicked(self):
        """Clears the favorites paths text box in the Favorites settings tab after confirmation."""
        reply = QMessageBox.question(
            self,
            "Delete Favorites List",
            "Are you sure you want to clear all favorite folder paths from the list?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.favorites_paths_edit.clear()

    def _parse_favorites_text(self):
        """Returns deduplicated non-empty folder paths from the Favorites text editor."""
        cleaned = []
        seen = set()
        for raw_line in self.favorites_paths_edit.toPlainText().splitlines():
            path_str = raw_line.strip()
            if not path_str:
                continue
            dedup_key = path_str.lower()
            if dedup_key not in seen:
                seen.add(dedup_key)
                cleaned.append(path_str)
        return cleaned

    def load_current_settings(self):
        """Loads latest settings from settings.json and maps them to UI controls."""
        settings = load_settings()

        theme_map = {"Cool Blue-Slate (Default)": 0}
        self.theme_combo.setCurrentIndex(theme_map.get(settings.get("theme_style"), 0))

        lang_map = {"English (US)": 0}
        self.lang_combo.setCurrentIndex(lang_map.get(settings.get("language"), 0))

        self.keep_alive_chk.setChecked(settings.get("keep_detached_viewers_alive", False))
        self.prefetch_chk.setChecked(settings.get("prefetch_images", True))
        self.hw_accel_chk.setChecked(settings.get("smooth_scaling", True))
        self.purge_thumb_chk.setChecked(settings.get("purge_thumb_cache_on_exit", False))
        self.encrypt_thumb_chk.setChecked(settings.get("encrypt_thumbnail_cache", True))

        cache_map = {
            "Auto (System RAM)": 0,
            "250 Thumbnails": 1,
            "500 Thumbnails": 2,
            "1,000 Thumbnails": 3,
            "2,000 Thumbnails": 4,
            "5,000 Thumbnails": 5,
            "10,000 Thumbnails": 6
        }
        self.max_cache_combo.setCurrentIndex(cache_map.get(settings.get("max_thumbnail_cache"), 0))

        include_children = settings.get("include_subfolders", False)
        self.include_subfolders_chk.setChecked(include_children)
        self.folder_depth_spin.setValue(settings.get("subfolder_depth", 1))
        self._on_include_subfolders_toggled(include_children)
        scroll_speed_val = max(1, min(29, int(settings.get("scroll_speed", 15))))
        self.scroll_speed_slider.setValue(scroll_speed_val)
        self.scroll_speed_val_label.setText(f"{scroll_speed_val} px")

        q_val = settings.get("thumbnail_jpeg_quality", 88)
        self.thumb_quality_slider.setValue(q_val)
        self.thumb_quality_val_label.setText(f"{q_val}%")

        self.confirm_delete_chk.setChecked(settings.get("confirm_on_delete", True))
        self.loop_nav_chk.setChecked(settings.get("loop_gallery_navigation", True))

        disk_limit_map = {"Disabled": 0, "500 MB": 1, "1 GB": 2, "2 GB": 3, "Unlimited": 4}
        current_limit = settings.get("disk_cache_limit_mb", "Disabled")
        self.disk_limit_combo.setCurrentIndex(disk_limit_map.get(current_limit, 0))
        self._on_disk_limit_changed(current_limit)

        thread_map = {"2 Threads": 0, "4 Threads": 1, "Auto (Max Cores)": 2}
        self.thread_combo.setCurrentIndex(thread_map.get(settings.get("thread_pool_concurrency"), 2))

        zoom_mode_map = {"Fit to Window (Default)": 0, "Fill Screen": 1}
        self.default_zoom_combo.setCurrentIndex(zoom_mode_map.get(settings.get("default_zoom_mode"), 0))
        self.invert_zoom_chk.setChecked(settings.get("invert_mouse_wheel_zoom", False))
        self.mouse_zoom_chk.setChecked(settings.get("mouse_zoom", True))

        favorites_list = settings.get("favorites", [])
        if isinstance(favorites_list, list):
            fav_lines = [str(p).strip() for p in favorites_list if p and str(p).strip()]
        else:
            fav_lines = []
        self.favorites_paths_edit.setPlainText("\n".join(fav_lines))

        try:
            max_fav_val = int(settings.get("max_favorites", 15))
        except (TypeError, ValueError):
            max_fav_val = 15
        self.max_favorites_spin.setValue(max(1, min(100, max_fav_val)))

    def save_current_settings(self):
        """Extracts values from widgets and persists them to settings.json."""
        new_values = {
            "theme_style": self.theme_combo.currentText(),
            "language": self.lang_combo.currentText(),
            "keep_detached_viewers_alive": self.keep_alive_chk.isChecked(),
            "prefetch_images": self.prefetch_chk.isChecked(),
            "smooth_scaling": self.hw_accel_chk.isChecked(),
            "purge_thumb_cache_on_exit": self.purge_thumb_chk.isChecked(),
            "encrypt_thumbnail_cache": self.encrypt_thumb_chk.isChecked(),
            "max_thumbnail_cache": self.max_cache_combo.currentText(),
            "thumbnail_jpeg_quality": self.thumb_quality_slider.value(),
            "confirm_on_delete": self.confirm_delete_chk.isChecked(),
            "loop_gallery_navigation": self.loop_nav_chk.isChecked(),
            "include_subfolders": self.include_subfolders_chk.isChecked(),
            "subfolder_depth": self.folder_depth_spin.value(),
            "scroll_speed": self.scroll_speed_slider.value(),
            "disk_cache_limit_mb": self.disk_limit_combo.currentText(),
            "thread_pool_concurrency": self.thread_combo.currentText(),
            "default_zoom_mode": self.default_zoom_combo.currentText(),
            "invert_mouse_wheel_zoom": self.invert_zoom_chk.isChecked(),
            "mouse_zoom": self.mouse_zoom_chk.isChecked(),
            "favorites": self._parse_favorites_text(),
            "max_favorites": self.max_favorites_spin.value(),
        }
        save_settings(new_values=new_values)

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        self.container = QFrame(self)
        self.container.setStyleSheet("""
            QFrame {
                background-color: #0d1321;
                border: 1px solid #1e293b;
                border-radius: 12px;
            }
            QLabel {
                color: #f8fafc;
                font-family: sans-serif;
                font-size: 13px;
                border: none;
                background: transparent;
            }
            QCheckBox {
                color: #cbd5e1;
                font-family: sans-serif;
                font-size: 13px;
                spacing: 8px;
                background: transparent;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #1e293b;
                border-radius: 4px;
                background: #131b2e;
            }
            QCheckBox::indicator:checked {
                background: #3b82f6;
                border-color: #3b82f6;
            }
            QComboBox {
                background-color: #131b2e;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 12px;
            }
            QComboBox:hover {
                border-color: #334155;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 20px;
                border-left-width: 0px;
            }
            QComboBox QAbstractItemView {
                background-color: #0d1321;
                color: #cbd5e1;
                selection-background-color: #1e293b;
                selection-color: #ffffff;
                border: 1px solid #1e293b;
            }
            QTabWidget::pane {
                border: 1px solid #1e293b;
                background-color: #0b0f19;
                border-radius: 8px;
                padding: 6px;
            }
            QTabBar::tab {
                background-color: #131b2e;
                color: #94a3b8;
                border: 1px solid #1e293b;
                padding: 6px 14px;
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

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(16, 16, 16, 16)
        container_layout.setSpacing(14)

        container_layout.addWidget(TitleBar("Application Settings", self, show_min_max=False))

        self.tab_widget = QTabWidget()

        # 1. General Tab
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.setContentsMargins(10, 10, 10, 10)
        general_layout.setSpacing(14)

        form_layout = QFormLayout()
        form_layout.setSpacing(12)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Cool Blue-Slate (Default)"])
        _configure_stable_combobox(self.theme_combo, self)
        form_layout.addRow("Theme Style:", self.theme_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["English (US)"])
        _configure_stable_combobox(self.lang_combo, self)
        form_layout.addRow("Language:", self.lang_combo)

        general_layout.addLayout(form_layout)
        self.tab_widget.addTab(general_tab, "General")

        # 2. Browsing Tab
        browsing_tab = QWidget()
        browsing_layout = QVBoxLayout(browsing_tab)
        browsing_layout.setContentsMargins(10, 10, 10, 10)
        browsing_layout.setSpacing(14)

        self.include_subfolders_chk = QCheckBox("Include Subfolders")
        self.include_subfolders_chk.setToolTip("Scan subfolders inside the selected directory for images.")
        self.include_subfolders_chk.setChecked(False)
        self.include_subfolders_chk.toggled.connect(self._on_include_subfolders_toggled)
        browsing_layout.addWidget(self.include_subfolders_chk)

        depth_row = QHBoxLayout()
        depth_label = QLabel("Subfolder Depth:")
        depth_label.setToolTip(
            "Maximum subfolder levels below the selected folder to scan (1 = direct subfolders only).")
        self.folder_depth_spin = QSpinBox()
        self.folder_depth_spin.setRange(1, 10)
        self.folder_depth_spin.setValue(1)
        self.folder_depth_spin.setEnabled(False)
        self.folder_depth_spin.setStyleSheet("""
            QSpinBox {
                background-color: #131b2e;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }
            QSpinBox:disabled {
                color: #475569;
                background-color: #0b0f19;
            }
        """)
        depth_row.addWidget(depth_label)
        depth_row.addWidget(self.folder_depth_spin)
        depth_row.addStretch()
        browsing_layout.addLayout(depth_row)

        scroll_header_layout = QHBoxLayout()
        self.scroll_speed_title_label = QLabel("Set Scroll Speed:")
        self.scroll_speed_title_label.setToolTip(
            "Adjusts the vertical scroll step size in pixels when scrolling the gallery grid (default: 15)."
        )
        self.scroll_speed_val_label = QLabel("15 px")
        self.scroll_speed_val_label.setStyleSheet("color: #3b82f6; font-weight: bold;")
        scroll_header_layout.addWidget(self.scroll_speed_title_label)
        scroll_header_layout.addStretch()
        scroll_header_layout.addWidget(self.scroll_speed_val_label)
        browsing_layout.addLayout(scroll_header_layout)

        self.scroll_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.scroll_speed_slider.setRange(1, 29)
        self.scroll_speed_slider.setValue(15)
        self.scroll_speed_slider.setToolTip("Set vertical scroll step speed (1 = slowest, 15 = default, 29 = fastest).")
        self.scroll_speed_slider.setStyleSheet("""
            QSlider::groove:horizontal { border: 1px solid #283548; height: 6px; background: #0b0f19; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 3px; }
            QSlider::handle:horizontal { background: #f8fafc; border: 1px solid #cbd5e1; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
        """)
        self.scroll_speed_slider.valueChanged.connect(lambda v: self.scroll_speed_val_label.setText(f"{v} px"))
        browsing_layout.addWidget(self.scroll_speed_slider)

        browsing_layout.addStretch()
        self.tab_widget.addTab(browsing_tab, "Browsing")

        # 3. Viewer Tab
        viewer_tab = QWidget()
        viewer_layout = QVBoxLayout(viewer_tab)
        viewer_layout.setContentsMargins(10, 10, 10, 10)
        viewer_layout.setSpacing(14)

        self.keep_alive_chk = QCheckBox("Keep detached image viewers alive on exit")
        self.keep_alive_chk.setChecked(False)
        viewer_layout.addWidget(self.keep_alive_chk)

        self.mouse_zoom_chk = QCheckBox("Mouse zoom")
        self.mouse_zoom_chk.setToolTip(
            "When enabled, the mouse wheel zooms the image. When disabled, scrolling navigates to the next or previous image."
        )
        self.mouse_zoom_chk.setChecked(True)
        viewer_layout.addWidget(self.mouse_zoom_chk)

        self.invert_zoom_chk = QCheckBox("Invert mouse wheel zoom direction (Natural Zoom)")
        self.invert_zoom_chk.setToolTip("Inverts scroll wheel direction so scrolling up zooms out and down zooms in.")
        viewer_layout.addWidget(self.invert_zoom_chk)

        self.loop_nav_chk = QCheckBox("Loop gallery navigation at folder boundaries")
        self.loop_nav_chk.setToolTip("When reaching the last image, arrow keys loop back to the first item.")
        viewer_layout.addWidget(self.loop_nav_chk)

        self.default_zoom_combo = QComboBox()
        self.default_zoom_combo.addItems(["Fit to Window (Default)", "Fill Screen"])
        self.default_zoom_combo.setToolTip("Sets the initial zoom and scaling behavior when opening an image.")
        _configure_stable_combobox(self.default_zoom_combo, self)

        zoom_info_lbl = QLabel("<b>Default Zoom Mode</b>")
        zoom_info_lbl.setStyleSheet("color: #64748b; font-size: 12px;")
        viewer_layout.addWidget(zoom_info_lbl)
        viewer_layout.addWidget(self.default_zoom_combo)
        viewer_layout.addStretch()

        self.tab_widget.addTab(viewer_tab, "Viewer")

        # 4. Thumbnail Tab
        thumb_tab = QWidget()
        thumb_layout = QVBoxLayout(thumb_tab)
        thumb_layout.setContentsMargins(10, 10, 10, 10)
        thumb_layout.setSpacing(14)

        thumb_header_layout = QHBoxLayout()
        self.thumb_quality_title_label = QLabel("Thumbnails JPEG Quality:")
        self.thumb_quality_title_label.setStyleSheet("font-weight: bold;")
        self.thumb_quality_val_label = QLabel("88%")
        self.thumb_quality_val_label.setStyleSheet("color: #3b82f6; font-weight: bold;")
        thumb_header_layout.addWidget(self.thumb_quality_title_label)
        thumb_header_layout.addStretch()
        thumb_header_layout.addWidget(self.thumb_quality_val_label)
        thumb_layout.addLayout(thumb_header_layout)

        self.thumb_quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.thumb_quality_slider.setRange(10, 100)
        self.thumb_quality_slider.setValue(88)
        self.thumb_quality_slider.setStyleSheet("""
            QSlider::groove:horizontal { border: 1px solid #283548; height: 6px; background: #0b0f19; border-radius: 3px; }
            QSlider::sub-page:horizontal { background: #3b82f6; border-radius: 3px; }
            QSlider::handle:horizontal { background: #f8fafc; border: 1px solid #cbd5e1; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
        """)
        self.thumb_quality_slider.valueChanged.connect(lambda v: self.thumb_quality_val_label.setText(f"{v}%"))
        thumb_layout.addWidget(self.thumb_quality_slider)

        quality_info_lbl = QLabel(
            "<i>Note: Adjusting thumbnails quality affects newly generated thumbnails. "
            "Clear or purge your thumbnail cache if you want older cached items re-encoded.</i>"
        )
        quality_info_lbl.setWordWrap(True)
        quality_info_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        thumb_layout.addWidget(quality_info_lbl)

        self.max_cache_combo = QComboBox()
        self.max_cache_combo.addItems([
            "Auto (System RAM)",
            "250 Thumbnails",
            "500 Thumbnails",
            "1,000 Thumbnails",
            "2,000 Thumbnails",
            "5,000 Thumbnails",
            "10,000 Thumbnails"
        ])
        self.max_cache_combo.setCurrentIndex(0)
        self.max_cache_combo.setToolTip(
            "Sets the maximum number of thumbnails held in RAM. Requires an application restart to take full effect.")
        _configure_stable_combobox(self.max_cache_combo, self)
        thumb_layout.addWidget(self.max_cache_combo)

        cache_info_lbl = QLabel(
            "<i>Sets the maximum number of thumbnails held in RAM. Requires an application restart to take full effect.</i>"
        )
        cache_info_lbl.setWordWrap(True)
        cache_info_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        thumb_layout.addWidget(cache_info_lbl)

        thumb_layout.addStretch()
        self.tab_widget.addTab(thumb_tab, "Thumbnails")

        # 5. Favorites Tab
        favorites_tab = QWidget()
        favorites_layout = QVBoxLayout(favorites_tab)
        favorites_layout.setContentsMargins(10, 10, 10, 10)
        favorites_layout.setSpacing(12)

        fav_paths_label = QLabel("Favorite Folder Paths (one path per line):")
        fav_paths_label.setStyleSheet("font-weight: bold;")
        favorites_layout.addWidget(fav_paths_label)

        self.favorites_paths_edit = QPlainTextEdit()
        self.favorites_paths_edit.setPlaceholderText("Enter folder paths, one per line...")
        self.favorites_paths_edit.setStyleSheet("""
            QPlainTextEdit {
                background-color: #05070a;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 8px;
                font-family: monospace;
                font-size: 11px;
            }
        """)
        favorites_layout.addWidget(self.favorites_paths_edit, stretch=1)

        fav_controls_row = QHBoxLayout()
        fav_controls_row.setSpacing(10)

        max_fav_label = QLabel("Max Favorites in Top Bar Menu:")
        max_fav_label.setToolTip("Maximum number of favorite folders displayed in the Favorites menu.")
        fav_controls_row.addWidget(max_fav_label)

        self.max_favorites_spin = QSpinBox()
        self.max_favorites_spin.setRange(1, 100)
        self.max_favorites_spin.setValue(15)
        self.max_favorites_spin.setStyleSheet("""
            QSpinBox {
                background-color: #131b2e;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
            }
        """)
        fav_controls_row.addWidget(self.max_favorites_spin)
        fav_controls_row.addStretch()

        self.delete_favorites_btn = QPushButton("Delete List")
        self.delete_favorites_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_favorites_btn.setToolTip("Clear all favorite paths from the list above.")
        self.delete_favorites_btn.setStyleSheet("""
            QPushButton {
                background-color: #7f1d1d;
                color: #fef2f2;
                border: 1px solid #991b1b;
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: bold;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #991b1b;
            }
        """)
        self.delete_favorites_btn.clicked.connect(self._on_delete_favorites_list_clicked)
        fav_controls_row.addWidget(self.delete_favorites_btn)

        favorites_layout.addLayout(fav_controls_row)
        self.tab_widget.addTab(favorites_tab, "Favorites")

        # 6. Advanced Tab
        advanced_tab = QWidget()
        advanced_layout = QVBoxLayout(advanced_tab)
        advanced_layout.setContentsMargins(10, 10, 10, 10)
        advanced_layout.setSpacing(14)

        perf_label = QLabel("<b>Performance & Memory Management</b>")
        advanced_layout.addWidget(perf_label)

        advanced_form = QFormLayout()
        advanced_form.setSpacing(10)

        self.disk_limit_combo = QComboBox()
        self.disk_limit_combo.addItems(["Disabled", "500 MB", "1 GB", "2 GB", "Unlimited"])
        self.disk_limit_combo.setToolTip("Sets the maximum disk footprint allowed for cached thumbnail files (disable falls back to ram disk caching).")
        _configure_stable_combobox(self.disk_limit_combo, self)
        self.disk_limit_combo.currentTextChanged.connect(self._on_disk_limit_changed)
        advanced_form.addRow("Thumbnail Disk Cache", self.disk_limit_combo)

        self.thread_combo = QComboBox()
        self.thread_combo.addItems(["2 Threads", "4 Threads", "Auto (Max Cores)"])
        self.thread_combo.setToolTip("Configures background worker concurrency for folder scanning and decoding.")
        _configure_stable_combobox(self.thread_combo, self)
        advanced_form.addRow("Background Tasks:", self.thread_combo)

        self.prefetch_chk = QCheckBox("Prefetch adjacent images")
        self.prefetch_chk.setToolTip("speeds-up image browsing")
        self.prefetch_chk.setChecked(True)
        advanced_form.addRow(self.prefetch_chk)

        self.hw_accel_chk = QCheckBox("Enable smooth pixmap scaling (High quality)")
        self.hw_accel_chk.setToolTip("reduces the blockiness in high zoom-in")
        self.hw_accel_chk.setChecked(True)
        advanced_form.addRow(self.hw_accel_chk)

        self.purge_thumb_chk = QCheckBox("Purge thumbnail disk cache on exit")
        self.purge_thumb_chk.setToolTip("Removes the cached thumbnails in system temp folder")
        self.purge_thumb_chk.setChecked(False)
        advanced_form.addRow(self.purge_thumb_chk)

        self.encrypt_thumb_chk = QCheckBox("Encrypt thumbnail disk cache")
        self.encrypt_thumb_chk.setToolTip("Encrypts the cached thumbnails")
        self.encrypt_thumb_chk.setChecked(True)
        advanced_form.addRow(self.encrypt_thumb_chk)

        advanced_layout.addLayout(advanced_form)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #1e293b; max-height: 1px;")
        advanced_layout.addWidget(sep)

        safety_label = QLabel("<b>File Operations & Safety</b>")
        advanced_layout.addWidget(safety_label)

        self.confirm_delete_chk = QCheckBox("Confirm before moving files to Recycle Bin")
        self.confirm_delete_chk.setToolTip("Displays a confirmation dialog when pressing Delete/Backspace.")
        advanced_layout.addWidget(self.confirm_delete_chk)

        advanced_layout.addStretch()
        self.tab_widget.addTab(advanced_tab, "Advanced")

        container_layout.addWidget(self.tab_widget, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #131b2e;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #1a233a;
                color: #f1f5f9;
                border-color: #334155;
            }
        """)
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save Settings")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
        """)
        save_btn.clicked.connect(lambda: (self.save_current_settings(), self.accept()))

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)

        self.resize_handle = ResizeHandleWidget(self)
        btn_layout.addWidget(self.resize_handle, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging_window = True
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_dragging_window", False):
            self.handle_mouse_move(self, event)
            self._locked_pos = self.pos()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if getattr(self, "_dragging_window", False):
            self._dragging_window = False
            self._locked_pos = self.pos()
            self.handle_mouse_release(event)
            event.accept()
            return
        super().mouseReleaseEvent(event)