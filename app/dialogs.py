# version 0.3.1

from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QTextEdit, QFrame,
    QGraphicsDropShadowEffect, QCheckBox, QComboBox, QFormLayout
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPen, QColor
from .widgets import (
    WindowDragHandler, TitleBar,
    ResizeHandleWidget
                      )

class ChildTestDialog(QDialog, WindowDragHandler):
    """Standalone resizable child window class."""

    def __init__(self, parent=None, custom_title="✨ Child Test Dialog Window"):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.custom_title = custom_title
        self.resize(520, 420)
        self.setMinimumSize(400, 300)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._setup_ui()

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

        container_layout.addWidget(TitleBar(self.custom_title, self, show_min_max=False))

        self.label = QLabel("Test Slider Value: 50%")
        self.label.setStyleSheet("color: #f8fafc; font-family: sans-serif; font-size: 13px; background: transparent;")
        container_layout.addWidget(self.label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(50)
        self.slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #283548;
                height: 6px;
                background: #0b0f19;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #f8fafc;
                border: 1px solid #cbd5e1;
                width: 16px;
                height: 16px;
                margin: -6px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #60a5fa;
            }
        """)
        self.slider.valueChanged.connect(lambda v: self.label.setText(f"Test Slider Value: {v}%"))
        container_layout.addWidget(self.slider)

        self.text_input = QTextEdit()
        self.text_input.setPlaceholderText("Type some test content or notes here...")
        self.text_input.setStyleSheet("""
            QTextEdit {
                background-color: #05070a;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 8px;
                font-size: 12px;
            }
        """)
        container_layout.addWidget(self.text_input, stretch=1)

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

        apply_btn = QPushButton("Apply Changes")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.setStyleSheet("""
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
        apply_btn.clicked.connect(self.accept)

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(apply_btn)

        self.resize_handle = ResizeHandleWidget(self)
        btn_layout.addWidget(self.resize_handle, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)


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

        # Content widget with description and clickable HTML link
        self.content_label = QLabel()
        self.content_label.setWordWrap(True)
        self.content_label.setOpenExternalLinks(True)
        self.content_label.setText(
            "<b>WiggleView</b> alpha 0.2.0<br><br>"
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
        container_layout.setSpacing(12)

        container_layout.addWidget(TitleBar("Batch Convert Options", self, show_min_max=False))

        # Format Selection Row
        form_layout = QHBoxLayout()
        format_label = QLabel("Target Format:")
        format_label.setStyleSheet("color: #e2e8f0; font-size: 12px; font-weight: bold;")

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
        self.format_combo.currentTextChanged.connect(self._on_format_changed)
        form_layout.addWidget(format_label)
        form_layout.addWidget(self.format_combo, stretch=1)
        container_layout.addLayout(form_layout)

        # Quality Slider Row
        self.quality_layout = QVBoxLayout()
        qual_header_layout = QHBoxLayout()
        self.qual_title_label = QLabel("Quality (JPEG / WEBP):")
        self.qual_title_label.setStyleSheet("color: #e2e8f0; font-size: 12px; font-weight: bold;")
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

        # Toggle Checkboxes Row
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

        # Action Buttons
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
    """Standalone resizable settings window featuring checkboxes and dropdown lists."""

    def __init__(self, parent=None):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.resize(480, 380)
        self.setMinimumSize(400, 320)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._setup_ui()

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
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        self.container.setGraphicsEffect(shadow)

        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(16, 16, 16, 16)
        container_layout.setSpacing(14)

        container_layout.addWidget(TitleBar("⚙️ Application Settings", self, show_min_max=False))

        # Form layout for settings options
        form_layout = QFormLayout()
        form_layout.setContentsMargins(0, 10, 0, 10)
        form_layout.setSpacing(16)

        # Dropdown lists (ComboBoxes)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Cool Blue-Slate (Default)", "Midnight Charcoal", "Ocean Deep"])
        form_layout.addRow("Theme Style:", self.theme_combo)

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["English (US)", "Spanish", "French", "German"])
        form_layout.addRow("Language:", self.lang_combo)

        container_layout.addLayout(form_layout)

        # Checkboxes
        self.notifications_chk = QCheckBox("Enable desktop status notifications")
        self.notifications_chk.setChecked(True)
        container_layout.addWidget(self.notifications_chk)

        self.startup_chk = QCheckBox("Check for updates automatically on startup")
        self.startup_chk.setChecked(False)
        container_layout.addWidget(self.startup_chk)

        self.tray_chk = QCheckBox("Minimize application to system tray on close")
        self.tray_chk.setChecked(True)
        container_layout.addWidget(self.tray_chk)

        container_layout.addStretch()

        # Action Buttons & Grip
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
        save_btn.clicked.connect(self.accept)

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)

        self.resize_handle = ResizeHandleWidget(self)
        btn_layout.addWidget(self.resize_handle, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight)

        container_layout.addLayout(btn_layout)
        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)