import os
import re
import shutil
import uuid
from PIL import Image
from PySide6.QtCore import Qt, QMimeData, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QMenu, QMessageBox, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QSpinBox, QCheckBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QPushButton, QLabel, QFrame, QGraphicsDropShadowEffect
)

from .helpers import load_settings
from .widgets import WindowDragHandler, TitleBar

_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_filename_component(name: str, fallback: str = "file") -> str:
    """Sanitizes a filename so path separators, traversal sequences, or invalid chars cannot escape target dirs."""
    if not isinstance(name, str) or not name:
        return fallback
    safe = os.path.basename(name)
    safe = safe.replace("..", "_")
    safe = "".join("_" if (ch in _INVALID_FILENAME_CHARS or ord(ch) < 32) else ch for ch in safe)
    safe = safe.strip(" .")
    return safe if safe else fallback


def build_rename_plan(paths, pattern: str, start_index: int = 1, pad_width: int = 0, lowercase_ext: bool = False):
    """Computes collision-safe rename operations for single or multiple files.

    Returns a list of dicts:
    {
        "src": src_abs,
        "old_name": orig_name,
        "dst": candidate_abs,
        "new_name": os.path.basename(candidate_abs),
        "changed": bool,
    }
    """
    unique_paths = []
    seen = set()
    for p in (paths or []):
        if not p:
            continue
        abs_p = os.path.abspath(p)
        if abs_p not in seen and os.path.isfile(abs_p):
            seen.add(abs_p)
            unique_paths.append(abs_p)

    if not unique_paths:
        return []

    is_batch = len(unique_paths) > 1
    raw_pattern = (pattern or "").strip()
    if not raw_pattern:
        raw_pattern = "image_{n}" if is_batch else os.path.splitext(os.path.basename(unique_paths[0]))[0]

    effective_pad = max(1, int(pad_width)) if int(pad_width or 0) > 0 else (max(2, len(str(len(unique_paths) + int(start_index)))) if is_batch else 1)
    all_selected_lower = {os.path.normcase(p) for p in unique_paths}
    reserved_targets = set()
    plan = []

    for offset, src_abs in enumerate(unique_paths):
        idx = int(start_index) + offset
        num_str = f"{idx:0{effective_pad}d}"
        parent_dir = os.path.dirname(src_abs)
        orig_name = os.path.basename(src_abs)
        orig_stem, raw_ext = os.path.splitext(orig_name)
        ext = raw_ext.lower() if lowercase_ext else raw_ext
        clean_ext = raw_ext.lstrip(".")
        safe_orig = sanitize_filename_component(orig_stem, fallback="image")

        if not is_batch:
            entered_stem, entered_ext = os.path.splitext(raw_pattern)
            if entered_ext.lower() == raw_ext.lower() and entered_stem:
                target_stem = sanitize_filename_component(entered_stem, fallback=orig_stem)
            else:
                target_stem = sanitize_filename_component(raw_pattern, fallback=orig_stem)
        else:
            has_counter = bool(re.search(r"\{(n|index)\}", raw_pattern, flags=re.IGNORECASE))
            rendered = re.sub(r"\{n\}|\{index\}", num_str, raw_pattern, flags=re.IGNORECASE)
            rendered = re.sub(r"\{name\}", safe_orig, rendered, flags=re.IGNORECASE)
            rendered = re.sub(r"\{ext\}", clean_ext, rendered, flags=re.IGNORECASE)
            if not has_counter:
                rendered = f"{rendered}_{num_str}"
            target_stem = sanitize_filename_component(rendered, fallback=f"image_{num_str}")

        candidate_name = f"{target_stem}{ext}"
        candidate_abs = os.path.abspath(os.path.join(parent_dir, candidate_name))
        if os.path.commonpath([parent_dir, candidate_abs]) != parent_dir:
            candidate_abs = os.path.abspath(os.path.join(parent_dir, f"{sanitize_filename_component(target_stem, fallback='image')}_{num_str}{ext}"))

        # If the computed target matches this exact source file, keep it without
        # appending a collision suffix; only suffix when colliding with another file.
        if os.path.normcase(candidate_abs) != os.path.normcase(src_abs):
            counter = 1
            while True:
                norm_cand = os.path.normcase(candidate_abs)
                exists_outside_selection = os.path.exists(candidate_abs) and (norm_cand not in all_selected_lower)
                already_used = norm_cand in reserved_targets
                if not exists_outside_selection and not already_used:
                    break
                candidate_abs = os.path.join(parent_dir, f"{target_stem}_{counter:03d}{ext}")
                counter += 1

        reserved_targets.add(os.path.normcase(candidate_abs))
        final_name = os.path.basename(candidate_abs)
        changed = os.path.normcase(src_abs) != os.path.normcase(candidate_abs)
        plan.append({
            "src": src_abs,
            "old_name": orig_name,
            "dst": candidate_abs,
            "new_name": final_name,
            "changed": changed,
        })

    return plan


class RenamePreviewDialog(QDialog, WindowDragHandler):
    """Borderless rename & batch-rename dialog matching SettingsDialog styling with live Old Name -> New Name preview."""

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        WindowDragHandler.__init__(self)
        self.paths = [os.path.abspath(p) for p in (paths or []) if p and os.path.isfile(p)]
        self.is_batch = len(self.paths) > 1
        self._result_plan = []

        self.setWindowTitle("Batch Rename Preview" if self.is_batch else "Rename File")
        self.resize(780 if self.is_batch else 640, 500 if self.is_batch else 440)
        self.setMinimumSize(540 if self.is_batch else 540, 420 if self.is_batch else 410)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowSystemMenuHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._setup_ui()
        self._refresh_preview()

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
            QLabel {
                color: #f8fafc;
                font-family: sans-serif;
                font-size: 12px;
                border: none;
                background: transparent;
            }
            QLineEdit, QSpinBox {
                background-color: #05070a;
                color: #f8fafc;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 6px 8px;
                font-size: 12px;
            }
            QLineEdit:focus, QSpinBox:focus {
                border-color: #3b82f6;
            }
            QCheckBox {
                color: #cbd5e1;
                font-family: sans-serif;
                font-size: 12px;
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
            QTableWidget {
                background-color: #05070a;
                color: #e2e8f0;
                border: 1px solid #1e293b;
                border-radius: 6px;
                gridline-color: #1e293b;
                font-size: 12px;
            }
            QHeaderView::section {
                background-color: #131b2e;
                color: #94a3b8;
                border: 1px solid #1e293b;
                padding: 6px;
                font-weight: 600;
            }
            QPushButton {
                background-color: #131b2e;
                color: #cbd5e1;
                border: 1px solid #1e293b;
                border-radius: 6px;
                padding: 8px 16px;
                font-weight: 600;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #1a233a;
                color: #f1f5f9;
                border-color: #334155;
            }
            QPushButton#primaryBtn {
                background-color: #3b82f6;
                border: none;
                color: #ffffff;
                padding: 8px 18px;
                font-weight: bold;
            }
            QPushButton#primaryBtn:hover {
                background-color: #2563eb;
            }
            QPushButton#primaryBtn:disabled {
                background-color: #1e293b;
                color: #64748b;
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

        title_text = "Batch Rename Preview" if self.is_batch else "Rename File"
        container_layout.addWidget(TitleBar(title_text, self, show_min_max=False))

        if self.is_batch:
            help_lbl = QLabel(
                "Tokens: <b>{n}</b> (sequence number), <b>{name}</b> (original stem), <b>{ext}</b> (extension). "
                "If <b>{n}</b> is omitted, a counter is appended automatically."
            )
            help_lbl.setWordWrap(True)
            help_lbl.setStyleSheet("color: #cbd5e1;")
            container_layout.addWidget(help_lbl)

        form = QFormLayout()
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        if not self.is_batch:
            stem, _ = os.path.splitext(os.path.basename(self.paths[0]))
            default_pattern = stem
            label_text = "New filename:"
        else:
            default_pattern = "image_{n}"
            label_text = "Name pattern:"

        self.pattern_edit = QLineEdit(default_pattern)
        self.pattern_edit.setMinimumHeight(30)
        self.pattern_edit.selectAll()
        self.pattern_edit.textChanged.connect(self._refresh_preview)
        form.addRow(label_text, self.pattern_edit)

        options_row = QHBoxLayout()
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(1)
        self.start_spin.valueChanged.connect(self._refresh_preview)

        self.pad_spin = QSpinBox()
        self.pad_spin.setRange(0, 8)
        self.pad_spin.setSpecialValueText("Auto")
        self.pad_spin.setValue(0)
        self.pad_spin.valueChanged.connect(self._refresh_preview)

        self.lower_ext_chk = QCheckBox("Lowercase extension")
        self.lower_ext_chk.stateChanged.connect(self._refresh_preview)

        if self.is_batch:
            options_row.addWidget(QLabel("Start #:"))
            options_row.addWidget(self.start_spin)
            options_row.addSpacing(12)
            options_row.addWidget(QLabel("Zero-padding:"))
            options_row.addWidget(self.pad_spin)
            options_row.addSpacing(12)
        else:
            self.start_spin.hide()
            self.pad_spin.hide()

        options_row.addWidget(self.lower_ext_chk)
        options_row.addStretch()
        form.addRow("Options:", options_row)

        container_layout.addLayout(form)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Current Name", "Preview New Name", "Status"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setMinimumHeight(88 if not self.is_batch else 180)
        self.table.setColumnWidth(0, 230)
        container_layout.addWidget(self.table, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #cbd5e1;")
        container_layout.addWidget(self.summary_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.clicked.connect(self.reject)

        self.apply_btn = QPushButton("Apply Rename")
        self.apply_btn.setObjectName("primaryBtn")
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.apply_btn)
        container_layout.addLayout(btn_row)

        main_layout.addWidget(self.container)

    def mousePressEvent(self, event):
        self.handle_mouse_press(self, event)

    def mouseMoveEvent(self, event):
        self.handle_mouse_move(self, event)

    def _refresh_preview(self):
        plan = build_rename_plan(
            self.paths,
            pattern=self.pattern_edit.text(),
            start_index=self.start_spin.value(),
            pad_width=self.pad_spin.value(),
            lowercase_ext=self.lower_ext_chk.isChecked(),
        )
        self._result_plan = plan
        self.table.setRowCount(len(plan))
        changed_count = 0

        for row_idx, entry in enumerate(plan):
            old_item = QTableWidgetItem(entry["old_name"])
            new_item = QTableWidgetItem(entry["new_name"])
            if entry["changed"]:
                changed_count += 1
                status_text = "Rename"
                new_item.setForeground(Qt.GlobalColor.cyan)
            else:
                status_text = "Unchanged"
                new_item.setForeground(Qt.GlobalColor.gray)

            status_item = QTableWidgetItem(status_text)
            self.table.setItem(row_idx, 0, old_item)
            self.table.setItem(row_idx, 1, new_item)
            self.table.setItem(row_idx, 2, status_item)

        self.summary_label.setText(f"Ready to rename {changed_count} of {len(plan)} file(s).")
        self.apply_btn.setEnabled(changed_count > 0)

    def get_rename_plan(self):
        return [entry for entry in self._result_plan if entry.get("changed")]


class FileOperationsController:
    """Handles clipboard copy/cut/paste, drag-and-drop move/copy, staged deletes, renames, and undo/redo."""

    def __init__(self, win, settings_file="settings.json"):
        self.win = win
        self.settings_file = settings_file
        self._undo_stack = []
        self._redo_stack = []
        self._cut_source_paths = []

    def record_undo_action(self, action):
        self._undo_stack.append(action)
        for discarded in self._redo_stack:
            if discarded.get("type") == "delete":
                for _, staged_path in discarded.get("pairs", []):
                    if staged_path and os.path.exists(staged_path):
                        ok, err = self._send_file_to_recycle_bin(staged_path)
                        if not ok:
                            try:
                                os.remove(staged_path)
                            except OSError:
                                pass
        self._redo_stack.clear()

    def refresh_after_history_op(self, affected_paths):
        affected_dirs = {os.path.dirname(p) for p in affected_paths if p}
        for d in affected_dirs:
            self.win.folder_scan_cache.pop(d, None)
        if self.win.current_watched_dir:
            self.win.folder_scan_cache.pop(os.path.abspath(self.win.current_watched_dir), None)
        self.win.folder_sync.perform_smooth_folder_sync()

    def ensure_folder_in_watch_and_cache(self, folder_path):
        if not folder_path or not os.path.isdir(folder_path):
            return
        abs_folder = os.path.abspath(folder_path)
        self.win.folder_scan_cache.pop(abs_folder, None)
        self.win.folder_scan_cache.pop(folder_path, None)
        if abs_folder not in self.win.file_watcher.directories():
            self.win.file_watcher.addPath(abs_folder)

    def refresh_target_folder_if_active(self, target_folder):
        if not self.win.current_watched_dir or not target_folder:
            return
        if os.path.abspath(self.win.current_watched_dir) == os.path.abspath(target_folder):
            self.win.folder_sync.perform_smooth_folder_sync()

    def undo(self):
        if not self._undo_stack:
            self.win.set_status("Nothing to undo.")
            return

        action = self._undo_stack.pop()
        op_type = action["type"]
        affected = []

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        if op_type == "move":
            for orig_src, current_dst in reversed(action["pairs"]):
                if os.path.exists(current_dst):
                    os.makedirs(os.path.dirname(orig_src), exist_ok=True)
                    shutil.move(current_dst, orig_src)
                    affected.extend([orig_src, current_dst])
            self._redo_stack.append(action)
            self.win.set_status(f"Undid move of {len(action['pairs'])} file(s).")

        elif op_type == "copy":
            for orig_src, copied_dst in reversed(action["pairs"]):
                if os.path.exists(copied_dst):
                    os.remove(copied_dst)
                    affected.append(copied_dst)
            self._redo_stack.append(action)
            self.win.set_status(f"Undid copy of {len(action['pairs'])} file(s).")

        elif op_type == "delete":
            for orig_path, staged_path in reversed(action["pairs"]):
                if os.path.exists(staged_path):
                    os.makedirs(os.path.dirname(orig_path), exist_ok=True)
                    shutil.move(staged_path, orig_path)
                    affected.append(orig_path)
            self._redo_stack.append(action)
            self.win.set_status(f"Restored {len(action['pairs'])} deleted file(s).")

        elif op_type == "rename":
            staged_map = []
            for orig_src, current_dst in reversed(action["pairs"]):
                if os.path.exists(current_dst):
                    os.makedirs(os.path.dirname(orig_src), exist_ok=True)
                    tmp_path = os.path.join(
                        os.path.dirname(current_dst),
                        f".tmp_undo_{uuid.uuid4().hex}_{os.path.basename(current_dst)}"
                    )
                    os.replace(current_dst, tmp_path)
                    staged_map.append((tmp_path, orig_src))
            for tmp_path, orig_src in staged_map:
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, orig_src)
                    affected.extend([tmp_path, orig_src])
            self._redo_stack.append(action)
            self.win.set_status(f"Undid rename of {len(action['pairs'])} file(s).")

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)
        self.refresh_after_history_op(affected)

    def redo(self):
        if not self._redo_stack:
            self.win.set_status("Nothing to redo.")
            return

        action = self._redo_stack.pop()
        op_type = action["type"]
        affected = []

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        if op_type == "move":
            for orig_src, target_dst in action["pairs"]:
                if os.path.exists(orig_src):
                    os.makedirs(os.path.dirname(target_dst), exist_ok=True)
                    shutil.move(orig_src, target_dst)
                    affected.extend([orig_src, target_dst])
            self._undo_stack.append(action)
            self.win.set_status(f"Redid move of {len(action['pairs'])} file(s).")

        elif op_type == "copy":
            for orig_src, target_dst in action["pairs"]:
                if os.path.exists(orig_src):
                    os.makedirs(os.path.dirname(target_dst), exist_ok=True)
                    shutil.copy2(orig_src, target_dst)
                    affected.append(target_dst)
            self._undo_stack.append(action)
            self.win.set_status(f"Redid copy of {len(action['pairs'])} file(s).")

        elif op_type == "delete":
            for orig_path, staged_path in action["pairs"]:
                if os.path.exists(orig_path):
                    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                    shutil.move(orig_path, staged_path)
                    affected.append(orig_path)
            self._undo_stack.append(action)
            self.win.set_status(f"Redid delete of {len(action['pairs'])} file(s).")

        elif op_type == "rename":
            staged_map = []
            for orig_src, target_dst in action["pairs"]:
                if os.path.exists(orig_src):
                    os.makedirs(os.path.dirname(target_dst), exist_ok=True)
                    tmp_path = os.path.join(
                        os.path.dirname(orig_src),
                        f".tmp_redo_{uuid.uuid4().hex}_{os.path.basename(orig_src)}"
                    )
                    os.replace(orig_src, tmp_path)
                    staged_map.append((tmp_path, target_dst))
            for tmp_path, target_dst in staged_map:
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, target_dst)
                    affected.extend([tmp_path, target_dst])
            self._undo_stack.append(action)
            self.win.set_status(f"Redid rename of {len(action['pairs'])} file(s).")

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)
        self.refresh_after_history_op(affected)

    def rename_selected_files(self, paths):
        unique_paths = []
        seen = set()
        for p in (paths or []):
            if not p:
                continue
            abs_p = os.path.abspath(p)
            if abs_p not in seen and os.path.isfile(abs_p):
                seen.add(abs_p)
                unique_paths.append(abs_p)

        if not unique_paths:
            self.win.set_status("Rename failed: No image(s) selected.")
            return

        dlg = RenamePreviewDialog(unique_paths, parent=self.win)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        planned_entries = dlg.get_rename_plan()
        planned_ops = [(entry["src"], entry["dst"]) for entry in planned_entries]
        if not planned_ops:
            self.win.set_status("No files required renaming.")
            return

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        renamed_records = []
        failed_errors = []

        temp_stages = []
        for src_abs, final_dst_abs in planned_ops:
            if not os.path.exists(src_abs):
                failed_errors.append(f"{os.path.basename(src_abs)}: file missing")
                continue
            tmp_path = os.path.join(
                os.path.dirname(src_abs),
                f".tmp_ren_{uuid.uuid4().hex}_{os.path.basename(src_abs)}"
            )
            try:
                os.replace(src_abs, tmp_path)
                temp_stages.append((src_abs, tmp_path, final_dst_abs))
            except Exception as e:
                failed_errors.append(f"{os.path.basename(src_abs)}: {e}")

        for src_abs, tmp_path, final_dst_abs in temp_stages:
            try:
                os.replace(tmp_path, final_dst_abs)
                renamed_records.append((src_abs, final_dst_abs))
            except Exception as e:
                failed_errors.append(f"{os.path.basename(src_abs)} -> {os.path.basename(final_dst_abs)}: {e}")
                if os.path.exists(tmp_path):
                    try:
                        os.replace(tmp_path, src_abs)
                    except OSError:
                        pass

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)

        if renamed_records:
            self.record_undo_action({"type": "rename", "pairs": renamed_records})
            old_to_new = {os.path.normcase(src): dst for src, dst in renamed_records}

            for item in self.win.loaded_items:
                norm_fp = os.path.normcase(os.path.abspath(item.get("file_path", "")))
                if norm_fp in old_to_new:
                    new_fp = old_to_new[norm_fp]
                    new_name = os.path.basename(new_fp)
                    item["file_path"] = new_fp
                    item["filename"] = new_name.lower()
                    item["display_name"] = (new_name[:16] + "..") if len(new_name) > 18 else new_name
                    try:
                        st = os.stat(new_fp)
                        item["size"] = st.st_size
                        item["mtime"] = st.st_mtime
                    except OSError:
                        pass
                    self.win.start_thumbnail_task(new_fp, self.win.scan_id)

            if hasattr(self.win.workspace_grid, "selected_file_paths"):
                self.win.workspace_grid.selected_file_paths = {
                    old_to_new.get(os.path.normcase(os.path.abspath(p)), p)
                    for p in self.win.workspace_grid.selected_file_paths
                }

            self.win.rebuild_path_index()
            if self.win.current_watched_dir in self.win.folder_scan_cache:
                self.win.folder_scan_cache[self.win.current_watched_dir] = list(self.win.loaded_items)
            self.win.apply_sorting()

            msg = f"Renamed {len(renamed_records)} file(s) (Ctrl+Z to undo)."
            if failed_errors:
                msg += f" ({len(failed_errors)} failed)"
            self.win.set_status(msg)
        elif failed_errors:
            self.win.set_status(f"Rename failed: {failed_errors[0]}")

    def move_files_to_folder(self, src_paths, target_folder):
        if not src_paths or not target_folder or not os.path.isdir(target_folder):
            return

        is_copy = bool(QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        target_abs = os.path.abspath(target_folder)
        processed_records = []

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        for src in src_paths:
            src_abs = os.path.abspath(src)
            if not os.path.exists(src_abs):
                continue
            if not is_copy and os.path.dirname(src_abs) == target_abs:
                continue

            raw_name = os.path.basename(src_abs)
            base, ext = os.path.splitext(raw_name)
            safe_base = sanitize_filename_component(base, fallback="image")
            safe_filename = f"{safe_base}{ext}"
            dest_abs = os.path.abspath(os.path.join(target_abs, safe_filename))
            if os.path.commonpath([target_abs, dest_abs]) != target_abs:
                continue

            suffix = "_"
            counter = 1
            while os.path.exists(dest_abs):
                dest_abs = os.path.join(target_abs, f"{safe_base}{suffix}{counter:04d}{ext}")
                counter += 1

            try:
                if is_copy:
                    shutil.copy2(src_abs, dest_abs)
                else:
                    shutil.move(src_abs, dest_abs)
                processed_records.append((src_abs, dest_abs))
            except Exception as e:
                action_verb = "copy" if is_copy else "move"
                print(f"Failed to {action_verb} {src_abs} -> {dest_abs}: {e}")

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)
        self.ensure_folder_in_watch_and_cache(target_abs)

        if processed_records:
            action_type = "copy" if is_copy else "move"
            action_past = "Copied" if is_copy else "Moved"
            self.record_undo_action({"type": action_type, "pairs": processed_records})
            self.win.folder_scan_cache.pop(target_abs, None)
            if self.win.current_watched_dir:
                self.win.folder_scan_cache.pop(os.path.abspath(self.win.current_watched_dir), None)
            self.win.folder_sync.perform_smooth_folder_sync()
            self.refresh_target_folder_if_active(target_abs)
            self.win.set_status(
                f"{action_past} {len(processed_records)} file(s) to {os.path.basename(target_abs)} (Ctrl+Z to undo)"
            )

    def show_grid_context_menu(self, pos):
        selected_paths = self.win.get_target_file_paths()

        menu = QMenu(self.win)
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
        rename_action = menu.addAction("Batch Rename..." if len(selected_paths) > 1 else "Rename...")
        menu.addSeparator()
        delete_action = menu.addAction("Delete")

        has_selection = hasattr(self.win.workspace_grid, 'selected_file_paths') and len(
            self.win.workspace_grid.selected_file_paths) > 0

        copy_action.setEnabled(has_selection)
        cut_action.setEnabled(has_selection)
        rename_action.setEnabled(has_selection)
        delete_action.setEnabled(has_selection)

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()
        paste_action.setEnabled(mime_data.hasUrls())

        action = menu.exec(self.win.workspace_grid.mapToGlobal(pos))

        if action == copy_action:
            self.copy_selected_files(selected_paths, cut=False)
        elif action == cut_action:
            self.copy_selected_files(selected_paths, cut=True)
        elif action == paste_action:
            self.paste_files_from_clipboard()
        elif action == rename_action:
            self.rename_selected_files(selected_paths)
        elif action == delete_action:
            self.delete_selected_files(selected_paths)

    def paste_files_from_clipboard(self):
        if not hasattr(self.win, 'current_watched_dir') or not self.win.current_watched_dir:
            self.win.set_status("No active folder destination for pasting.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = clipboard.mimeData()

        if not mime_data.hasUrls():
            self.win.set_status("No files found in clipboard to paste.")
            return

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        valid_exts = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
        newly_added_items = []
        copied_records = []
        moved_records = []
        success_count = 0
        cut_paths = self._cut_source_paths
        dest_dir_abs = os.path.abspath(self.win.current_watched_dir)

        for url in mime_data.urls():
            src_path = url.toLocalFile()
            if src_path and os.path.exists(src_path):
                ext = os.path.splitext(src_path)[1].lower()
                if ext not in valid_exts:
                    continue
                try:
                    raw_name = os.path.basename(src_path)
                    base, file_ext = os.path.splitext(raw_name)
                    safe_base = sanitize_filename_component(base, fallback="image")
                    safe_filename = f"{safe_base}{file_ext}"
                    dest_path = os.path.abspath(os.path.join(dest_dir_abs, safe_filename))
                    if os.path.commonpath([dest_dir_abs, dest_path]) != dest_dir_abs:
                        continue
                    suffix = "_"
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(dest_dir_abs, f"{safe_base}{suffix}{counter:04d}{file_ext}")
                        counter += 1

                    if cut_paths and os.path.abspath(src_path) in [os.path.abspath(p) for p in cut_paths]:
                        shutil.move(src_path, dest_path)
                        moved_records.append((os.path.abspath(src_path), os.path.abspath(dest_path)))
                    else:
                        shutil.copy2(src_path, dest_path)
                        copied_records.append((os.path.abspath(src_path), os.path.abspath(dest_path)))

                    file_stats = os.stat(dest_path)
                    file_name = os.path.basename(dest_path)

                    has_comfy = False
                    if dest_path.lower().endswith(".png"):
                        try:
                            with Image.open(dest_path) as img:
                                if hasattr(img, "text") and img.text and (
                                        "workflow" in img.text or "prompt" in img.text):
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
                    self.win.start_thumbnail_task(dest_path, self.win.scan_id)
                    success_count += 1
                except Exception as e:
                    print(f"Failed to paste/move {src_path}: {e}")

        self._cut_source_paths = []
        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)

        if success_count > 0:
            if copied_records:
                self.record_undo_action({"type": "copy", "pairs": copied_records})
            if moved_records:
                self.record_undo_action({"type": "move", "pairs": moved_records})

            self.win.loaded_items.extend(newly_added_items)
            self.win.rebuild_path_index()
            if self.win.current_watched_dir in self.win.folder_scan_cache:
                self.win.folder_scan_cache[self.win.current_watched_dir] = list(self.win.loaded_items)
            self.win.apply_sorting()
            self.win.set_status(f"Successfully processed {success_count} file(s).")
        else:
            self.win.set_status("No valid image files pasted.")

    def copy_selected_files(self, paths, cut=False):
        raw_paths = list(paths) if paths else [
            item["file_path"] for item in self.win.loaded_items if item.get("file_path")
        ]

        unique_paths = []
        seen = set()
        for p in raw_paths:
            if not p:
                continue
            abs_p = os.path.abspath(p)
            if abs_p not in seen and os.path.exists(abs_p):
                seen.add(abs_p)
                unique_paths.append(abs_p)

        if not unique_paths:
            self.win.set_status("No images available to copy.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = QMimeData()
        urls = [QUrl.fromLocalFile(p) for p in unique_paths]
        mime_data.setUrls(urls)
        clipboard.setMimeData(mime_data)

        self._cut_source_paths = unique_paths if cut else []

        action_name = "Cut" if cut else "Copied"
        has_explicit_selection = bool(getattr(self.win.workspace_grid, "selected_file_paths", None))
        target_desc = "selected file(s)" if has_explicit_selection else "entire gallery"
        self.win.set_status(f"{action_name} {len(unique_paths)} file(s) ({target_desc}) to clipboard.")

    def _send_file_to_recycle_bin(self, target_path):
        """Sends a single file to the OS Recycle Bin via send2trash or Win32 SHFileOperationW."""
        abs_target = os.path.abspath(target_path)
        if not os.path.exists(abs_target):
            return True, None

        last_err = None
        try:
            from send2trash import send2trash  # type: ignore

            send2trash(abs_target)
            if not os.path.exists(abs_target):
                return True, None
            last_err = "send2trash completed, but file still exists."
        except ImportError:
            pass
        except Exception as exc:
            last_err = f"send2trash failed: {exc}"

        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                FO_DELETE = 3
                FOF_SILENT = 0x0004
                FOF_NOCONFIRMATION = 0x0010
                FOF_ALLOWUNDO = 0x0040
                FOF_NOERRORUI = 0x0400

                class _SHFILEOPSTRUCTW(ctypes.Structure):
                    _fields_ = [
                        ("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", wintypes.WORD),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", wintypes.LPVOID),
                        ("lpszProgressTitle", wintypes.LPCWSTR),
                    ]

                p_from = abs_target + "\x00\x00"
                flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
                op = _SHFILEOPSTRUCTW(
                    hwnd=None,
                    wFunc=FO_DELETE,
                    pFrom=p_from,
                    pTo=None,
                    fFlags=flags,
                    fAnyOperationsAborted=False,
                    hNameMappings=None,
                    lpszProgressTitle=None,
                )
                shell32 = ctypes.windll.shell32
                shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
                shell32.SHFileOperationW.restype = ctypes.c_int
                rc = shell32.SHFileOperationW(ctypes.byref(op))
                if rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(abs_target):
                    return True, None
                return False, f"SHFileOperationW rc={rc}, aborted={bool(op.fAnyOperationsAborted)}"
            except Exception as exc:
                return False, f"Win32 recycle bin error: {exc}"

        return False, last_err or "No Recycle Bin backend available."

    def delete_selected_files(self, paths):
        if not paths:
            self.win.status_label.setText("Status: Deletion failed - No valid images selected.")
            return

        settings = load_settings(self.settings_file)
        confirm_enabled = bool(settings.get("confirm_on_delete", True))
        use_undo_staging = not (bool(settings.get("use_recycle_bin", True)))

        if confirm_enabled:
            reply = QMessageBox.question(
                self.win, "Confirm Delete",
                f"Are you sure you want to move {len(paths)} selected file(s) to the Recycle Bin?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        if use_undo_staging:
            os.makedirs(self.win.undo_trash_dir, exist_ok=True)

        if self.win.current_watched_dir:
            self.win.file_watcher.removePaths(self.win.file_watcher.directories())

        deleted_paths = set()
        deleted_records = []
        failed_errors = []

        for path in paths:
            norm_path = os.path.abspath(path)
            if os.path.exists(norm_path):
                if use_undo_staging:
                    try:
                        raw_base = sanitize_filename_component(os.path.basename(norm_path), fallback="file")
                        staged_name = f"{uuid.uuid4().hex}_{raw_base}"
                        staged_path = os.path.join(self.win.undo_trash_dir, staged_name)
                        shutil.move(norm_path, staged_path)
                        deleted_paths.add(norm_path)
                        deleted_records.append((norm_path, staged_path))
                    except Exception as e:
                        failed_errors.append(f"{os.path.basename(norm_path)}: {e}")
                        print(f"DEBUG Error staging delete for {path}: {e}")
                else:
                    ok, err = self._send_file_to_recycle_bin(norm_path)
                    if ok:
                        deleted_paths.add(norm_path)
                    else:
                        failed_errors.append(f"{os.path.basename(norm_path)}: {err}")
                        print(f"DEBUG Error moving {path} to Recycle Bin: {err}")

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)

        if deleted_paths:
            if use_undo_staging and deleted_records:
                self.record_undo_action({"type": "delete", "pairs": deleted_records})

            self.win.loaded_items = [
                item for item in self.win.loaded_items
                if os.path.abspath(item["file_path"]) not in deleted_paths
            ]
            self.win.rebuild_path_index()
            if self.win.current_watched_dir in self.win.folder_scan_cache:
                self.win.folder_scan_cache[self.win.current_watched_dir] = list(self.win.loaded_items)

            self.win.workspace_grid.set_cards(self.win.loaded_items)

            deleted_count = len(deleted_paths)
            total_remaining = len(self.win.loaded_items)
            if use_undo_staging:
                status_msg = f"Deleted {deleted_count} file(s) (Ctrl+Z to undo). Total remaining: {total_remaining}"
            else:
                status_msg = f"Moved {deleted_count} file(s) to Recycle Bin. Total remaining: {total_remaining}"
            if failed_errors:
                status_msg += f" ({len(failed_errors)} failed)"
            self.win.set_status(status_msg)

            self.win.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Folder Selected\n• Total Images: {total_remaining}\n• Selected: None"
            )
        else:
            detail = f" ({failed_errors[0]})" if failed_errors else ""
            self.win.set_status(f"Deletion failed: No files were removed.{detail}")

    def flush_staged_trash_to_recycle_bin(self):
        trash_dir = getattr(self.win, "undo_trash_dir", None)
        if trash_dir and os.path.exists(trash_dir):
            try:
                for fname in os.listdir(trash_dir):
                    fpath = os.path.join(trash_dir, fname)
                    if os.path.exists(fpath):
                        ok, err = self._send_file_to_recycle_bin(fpath)
                        if not ok:
                            print(f"Error flushing {fpath} to Recycle Bin: {err}")
                if os.path.exists(trash_dir) and not os.listdir(trash_dir):
                    shutil.rmtree(trash_dir, ignore_errors=True)
            except Exception as e:
                print(f"Error flushing staged deletes to Recycle Bin: {e}")
