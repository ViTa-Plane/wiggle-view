import os
import shutil
import uuid
from PIL import Image
from PySide6.QtCore import Qt, QMimeData, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QMessageBox

from .helpers import load_settings


class FileOperationsController:
    """Handles clipboard copy/cut/paste, drag-and-drop move/copy, staged deletes, and undo/redo."""

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

        if self.win.current_watched_dir:
            self.win.folder_sync.sync_file_watcher_paths(self.win.current_watched_dir)
        self.refresh_after_history_op(affected)

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

            filename = os.path.basename(src_abs)
            dest_abs = os.path.join(target_abs, filename)
            base, ext = os.path.splitext(filename)
            suffix = "_copy" if is_copy else "_moved"
            counter = 1
            while os.path.exists(dest_abs):
                dest_abs = os.path.join(target_abs, f"{base}{suffix}{counter}{ext}")
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
        menu.addSeparator()
        delete_action = menu.addAction("Delete")

        has_selection = hasattr(self.win.workspace_grid, 'selected_file_paths') and len(
            self.win.workspace_grid.selected_file_paths) > 0

        copy_action.setEnabled(has_selection)
        cut_action.setEnabled(has_selection)
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

        for url in mime_data.urls():
            src_path = url.toLocalFile()
            if src_path and os.path.exists(src_path):
                ext = os.path.splitext(src_path)[1].lower()
                if ext not in valid_exts:
                    continue
                try:
                    filename = os.path.basename(src_path)
                    dest_path = os.path.join(self.win.current_watched_dir, filename)

                    base, file_ext = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(dest_path):
                        dest_path = os.path.join(self.win.current_watched_dir, f"{base}_copy{counter}{file_ext}")
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
        if not paths:
            paths = [item["file_path"] for item in self.win.loaded_items if os.path.exists(item["file_path"])]

        if not paths:
            self.win.set_status("No images available to copy.")
            return

        clipboard = QGuiApplication.clipboard()
        mime_data = QMimeData()
        urls = [QUrl.fromLocalFile(p) for p in paths if os.path.exists(p)]
        mime_data.setUrls(urls)
        clipboard.setMimeData(mime_data)

        self._cut_source_paths = paths if cut else []

        action_name = "Cut" if cut else "Copied"
        target_desc = "selected file(s)" if self.win.workspace_grid.selected_file_paths else "entire gallery"
        self.win.set_status(f"{action_name} {len(paths)} file(s) ({target_desc}) to clipboard.")

    def delete_selected_files(self, paths):
        if not paths:
            self.win.status_label.setText("Status: Deletion failed - No valid images selected.")
            return

        settings = load_settings(self.settings_file)
        confirm_enabled = settings.get("confirm_on_delete", True)

        if confirm_enabled:
            reply = QMessageBox.question(
                self.win, "Confirm Delete",
                f"Are you sure you want to move {len(paths)} selected file(s) to the Recycle Bin?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        deleted_paths = set()
        deleted_records = []

        for path in paths:
            norm_path = os.path.abspath(path)
            if os.path.exists(norm_path):
                try:
                    staged_name = f"{uuid.uuid4().hex}_{os.path.basename(norm_path)}"
                    staged_path = os.path.join(self.win.undo_trash_dir, staged_name)
                    shutil.move(norm_path, staged_path)
                    deleted_paths.add(norm_path)
                    deleted_records.append((norm_path, staged_path))
                except Exception as e:
                    print(f"DEBUG Error staging delete for {path}: {e}")

        if deleted_paths:
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
            self.win.set_status(f"Moved {deleted_count} file(s) to Recycle Bin. Total remaining: {total_remaining}")

            self.win.right_sidebar.inspector_label.setText(
                f"Inspector Properties:\n\n• Folder Selected\n• Total Images: {total_remaining}\n• Selected: None"
            )
        else:
            self.win.set_status("Deletion failed: No files were removed.")

    def flush_staged_trash_to_recycle_bin(self):
        trash_dir = getattr(self.win, "undo_trash_dir", None)
        if trash_dir and os.path.exists(trash_dir):
            try:
                from send2trash import send2trash
                for fname in os.listdir(trash_dir):
                    fpath = os.path.join(trash_dir, fname)
                    if os.path.exists(fpath):
                        send2trash(fpath)
                shutil.rmtree(trash_dir, ignore_errors=True)
            except Exception as e:
                print(f"Error flushing staged deletes to Recycle Bin: {e}")
