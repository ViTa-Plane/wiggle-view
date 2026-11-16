# version 0.1.9

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
    """Standalone fixed-size About dialog class with an Okay button."""

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

        self.text_input = QTextEdit()
        self.text_input.setPlainText(
            "WiggleView alpha 0.0.18"
        )
        self.text_input.setReadOnly(True)
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