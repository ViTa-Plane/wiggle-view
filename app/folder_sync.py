import os
import re
import time
from PIL import Image
from PySide6.QtCore import QObject, QRunnable, QTimer, Signal

from .helpers import load_settings
from .workers import ComfyMetadataScannerTask


def _path_key(path):
    """Normalizes a filesystem path into a case-insensitive lookup key."""
    return os.path.normcase(os.path.abspath(path)).lower()


class ChildFolderScanSignals(QObject):
    scan_ready = Signal(int, str, list, set)


class DiskCacheSizeSignals(QObject):
    done = Signal(int)


class _DiskCacheSizeTask(QRunnable):
    """Calculates SQLite thumbnail cache size off the GUI thread with shutdown guards."""

    def __init__(self, db_cache, sig, is_closing_getter):
        super().__init__()
        self.setAutoDelete(True)
        self.db_cache = db_cache
        self.sig = sig
        self.is_closing_getter = is_closing_getter

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def _emit_done(self, total):
        if self._is_closing():
            return
        try:
            self.sig.done.emit(total)
        except RuntimeError:
            pass

    def run(self):
        if self._is_closing():
            return
        total = 0
        try:
            if self.db_cache is not None:
                total = self.db_cache.total_size_bytes()
        except Exception:
            total = 0

        self._emit_done(total)


class ChildFolderScanTask(QRunnable):
    """Scans child/root gallery folders and builds item metadata off the main GUI thread."""

    _VALID_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}

    def __init__(self, root_folder, scan_id, existing_paths_set, include_subfolders, max_depth, signals, active_scan_getter, is_closing_getter=None):
        super().__init__()
        self.root_folder = root_folder
        self.scan_id = scan_id
        self.existing_paths_set = existing_paths_set
        self.include_subfolders = include_subfolders
        self.max_depth = max_depth
        self.signals = signals
        self.active_scan_getter = active_scan_getter
        self.is_closing_getter = is_closing_getter

    def _is_stale(self):
        if callable(self.is_closing_getter) and self.is_closing_getter():
            return True
        return callable(self.active_scan_getter) and self.scan_id != self.active_scan_getter()

    def run(self):
        if self._is_stale() or not self.root_folder or not os.path.isdir(self.root_folder):
            return

        disk_files = set()
        if not self.include_subfolders:
            try:
                with os.scandir(self.root_folder) as entries:
                    for entry in entries:
                        if self._is_stale():
                            return
                        try:
                            if entry.is_file(follow_symlinks=False):
                                if os.path.splitext(entry.name)[1].lower() in self._VALID_EXTS:
                                    disk_files.add(os.path.abspath(entry.path))
                        except OSError:
                            continue
            except OSError:
                return
        else:
            base_depth = len(os.path.normpath(self.root_folder).split(os.sep))
            for root, dirs, files in os.walk(self.root_folder, followlinks=False, onerror=lambda e: None):
                if self._is_stale():
                    return
                current_depth = len(os.path.normpath(root).split(os.sep)) - base_depth
                if current_depth >= self.max_depth:
                    dirs[:] = []
                for fname in files:
                    if os.path.splitext(fname)[1].lower() in self._VALID_EXTS:
                        disk_files.add(os.path.abspath(os.path.join(root, fname)))

        if self._is_stale():
            return

        existing_keys = {_path_key(p) for p in self.existing_paths_set}
        added_paths = sorted(p for p in disk_files if _path_key(p) not in existing_keys)
        new_items = []
        for file_path in added_paths:
            if self._is_stale():
                return
            file_name = os.path.basename(file_path)
            try:
                stats = os.stat(file_path)
                size = stats.st_size
                mtime = stats.st_mtime
            except OSError:
                size = 0
                mtime = 0

            new_items.append({
                "file_path": file_path,
                "filename": file_name.lower(),
                "display_name": (file_name[:16] + '..') if len(file_name) > 18 else file_name,
                "size": size,
                "mtime": mtime,
                "thumb_path": None,
                "has_comfy": False,
            })

        if not self._is_stale():
            try:
                self.signals.scan_ready.emit(self.scan_id, os.path.abspath(self.root_folder), new_items, disk_files)
            except RuntimeError:
                pass


class FolderSyncController:
    """Handles directory watching, child folder scans, smooth reconciliation, and disk cache metrics."""

    def __init__(self, win, settings_file="settings.json"):
        self.win = win
        self.settings_file = settings_file
        self._closing = False
        self._cached_disk_size_str = self.format_mb(0)
        self._disk_size_scan_running = False
        self._last_disk_size_refresh_ms = 0
        self._active_disk_size_emitter = None
        self._watcher_debounce_timer = None
        self._pending_changed_paths = set()
        self._branch_snapshots = {}  # abs_folder -> (exists, mtime_ns, size, entries_tuple)
        self._watched_tree_signature = None

        self.scan_signals = ChildFolderScanSignals()
        self.scan_signals.scan_ready.connect(self.on_subfolder_scan_ready)

        self.disk_size_signals = DiskCacheSizeSignals()
        self.disk_size_signals.done.connect(self._on_disk_cache_size_calculated)

    def prepare_for_close(self):
        self._closing = True
        self._disk_size_scan_running = False
        self._pending_changed_paths.clear()
        self._branch_snapshots.clear()
        self._watched_tree_signature = None
        if self._watcher_debounce_timer is not None:
            self._watcher_debounce_timer.stop()

    def _is_closing(self):
        return self._closing or bool(getattr(self.win, "_is_closing", False))

    def is_disk_cache_enabled(self):
        if not getattr(self.win, "thumb_cache_dir", None):
            return False
        settings = load_settings(self.settings_file)
        limit_val = settings.get("disk_cache_limit_mb", "1 GB")
        if isinstance(limit_val, str) and limit_val.strip().lower() == "disabled":
            return False
        return bool(limit_val)

    @staticmethod
    def format_mb(num_bytes):
        if num_bytes < 0:
            num_bytes = 0
        return f"{num_bytes / (1024 * 1024):.2f} MB"

    @staticmethod
    def get_disk_cache_limit_str(settings):
        limit_val = settings.get("disk_cache_limit_mb", "1 GB")
        if isinstance(limit_val, (int, float)):
            return f"{float(limit_val):.2f} MB"
        if isinstance(limit_val, str):
            text = limit_val.strip()
            if not text or text.lower() == "disabled":
                return None
            if text.lower() == "unlimited":
                return "Unlimited"
            try:
                num = float(text.lower().replace("mb", "").strip())
                return f"{num:.2f} MB"
            except ValueError:
                return text
        return None

    def _on_disk_cache_size_calculated(self, total_bytes):
        self._disk_size_scan_running = False
        if self._is_closing():
            return
        self._cached_disk_size_str = self.format_mb(total_bytes)
        if hasattr(self.win, "status_label") and self.win.status_label:
            if not getattr(self.win.workspace_grid, "selected_file_paths", None):
                try:
                    self.win.set_footer_status(self.win.build_full_gallery_status())
                except RuntimeError:
                    pass

    def get_disk_cache_size_str(self):
        if self._is_closing() or not self.is_disk_cache_enabled():
            self._cached_disk_size_str = self.format_mb(0)
            return self._cached_disk_size_str

        cache_dir = getattr(self.win, "thumb_cache_dir", None)
        if not cache_dir or not os.path.isdir(cache_dir):
            self._cached_disk_size_str = self.format_mb(0)
            return self._cached_disk_size_str

        now_ms = int(time.monotonic() * 1000)
        if not self._disk_size_scan_running and (now_ms - self._last_disk_size_refresh_ms >= 500):
            self._disk_size_scan_running = True
            self._last_disk_size_refresh_ms = now_ms
            task = _DiskCacheSizeTask(
                getattr(self.win, "thumb_db_cache", None),
                self.disk_size_signals,
                self._is_closing,
            )
            self.win.thread_pool.start(task)

        return self._cached_disk_size_str

    _VALID_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}

    @staticmethod
    def _depth_split(path_str):
        norm = os.path.normcase(os.path.abspath(path_str))
        drive, tail = os.path.splitdrive(norm)
        parts = [p for p in tail.split(os.sep) if p]
        if drive:
            parts.insert(0, drive)
        return parts

    def _capture_dir_snapshot(self, dir_path):
        abs_dir = os.path.abspath(dir_path)
        try:
            st = os.stat(abs_dir)
            dir_mtime_ns = st.st_mtime_ns
        except OSError:
            return (False, 0, ())

        entries = []
        try:
            with os.scandir(abs_dir) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_file = (not is_dir) and entry.is_file(follow_symlinks=False)
                        low_name = entry.name.lower()
                        if is_dir:
                            entries.append((low_name, "D", 0, 0))
                        elif is_file and os.path.splitext(low_name)[1] in self._VALID_IMAGE_EXTS:
                            est = entry.stat(follow_symlinks=False)
                            entries.append((low_name, "F", est.st_size, est.st_mtime_ns))
                    except OSError:
                        continue
        except OSError:
            return (False, 0, ())

        entries.sort()
        return (True, dir_mtime_ns, tuple(entries))

    def _is_ancestor_or_same(self, ancestor_path, candidate_path):
        anc = os.path.normcase(os.path.abspath(ancestor_path))
        cand = os.path.normcase(os.path.abspath(candidate_path))
        if anc == cand:
            return True
        if not anc.endswith(os.sep):
            anc = anc + os.sep
        return cand.startswith(anc)

    def _collect_watched_dirs(self, root_folder, include_subfolders, max_depth):
        abs_root = os.path.abspath(root_folder)
        if not os.path.isdir(abs_root):
            return []

        paths_to_watch = [abs_root]
        max_watch = getattr(self.win, "_MAX_WATCHED_DIRS", 256)
        if not include_subfolders:
            return paths_to_watch

        base_parts = self._depth_split(abs_root)
        base_len = len(base_parts)

        for dirpath, dirnames, _ in os.walk(abs_root, followlinks=False, onerror=lambda e: None):
            if len(paths_to_watch) >= max_watch:
                dirnames[:] = []
                break
            current_depth = len(self._depth_split(dirpath)) - base_len
            if current_depth >= max_depth:
                dirnames[:] = []
                continue

            kept_dirs = []
            for d in sorted(dirnames):
                if len(paths_to_watch) >= max_watch:
                    break
                full_dir = os.path.abspath(os.path.join(dirpath, d))
                if os.path.isdir(full_dir):
                    paths_to_watch.append(full_dir)
                    if current_depth + 1 < max_depth:
                        kept_dirs.append(d)
            dirnames[:] = kept_dirs

        return paths_to_watch

    def sync_file_watcher_paths(self, root_folder):
        if self._is_closing():
            return
        if not root_folder or not os.path.isdir(root_folder):
            current_paths = set(self.win.file_watcher.directories())
            if current_paths:
                self.win.file_watcher.removePaths(list(current_paths))
            self._watched_tree_signature = None
            return

        abs_root = os.path.abspath(root_folder)
        settings = load_settings(self.settings_file)
        include_subfolders = bool(settings.get("include_subfolders", False))
        max_depth = max(1, int(settings.get("subfolder_depth", 1)))

        paths_to_watch = self._collect_watched_dirs(abs_root, include_subfolders, max_depth)
        tree_sig = (
            os.path.normcase(abs_root),
            include_subfolders,
            max_depth,
            tuple(os.path.normcase(p) for p in paths_to_watch),
        )

        current_set = set(self.win.file_watcher.directories())
        desired_set = set(paths_to_watch)

        if self._watched_tree_signature == tree_sig and current_set == desired_set:
            return

        to_remove = [p for p in current_set if p not in desired_set]
        to_add = [p for p in paths_to_watch if p not in current_set]
        if to_remove:
            self.win.file_watcher.removePaths(to_remove)
        if to_add:
            self.win.file_watcher.addPaths(to_add)
        self._watched_tree_signature = tree_sig

    def on_directory_changed(self, path):
        if self._is_closing():
            return
        if not self.win.current_watched_dir or not os.path.exists(self.win.current_watched_dir):
            return

        abs_changed = os.path.abspath(path) if path else os.path.abspath(self.win.current_watched_dir)
        if path and os.path.isdir(abs_changed) and abs_changed not in self.win.file_watcher.directories():
            self.win.file_watcher.addPath(abs_changed)

        self._pending_changed_paths.add(abs_changed)

        if self._watcher_debounce_timer is None:
            self._watcher_debounce_timer = QTimer(self.win)
            self._watcher_debounce_timer.setSingleShot(True)
            self._watcher_debounce_timer.timeout.connect(self.perform_smooth_folder_sync)

        self._watcher_debounce_timer.start(300)

    def _collect_disk_image_paths(self, changed_paths=None):
        abs_root = os.path.abspath(self.win.current_watched_dir)
        settings = load_settings(self.settings_file)
        include_subfolders = bool(settings.get("include_subfolders", False))
        max_depth = max(1, int(settings.get("subfolder_depth", 1)))

        if not include_subfolders:
            snap = self._capture_dir_snapshot(abs_root)
            self._branch_snapshots = {abs_root: snap}
            self.win.folder_scan_cache.pop(abs_root, None)
            self.win.folder_scan_cache.pop(self.win.current_watched_dir, None)
            disk_files = set()
            if snap[0]:
                try:
                    with os.scandir(abs_root) as entries:
                        for entry in entries:
                            try:
                                if entry.is_file(follow_symlinks=False):
                                    if os.path.splitext(entry.name)[1].lower() in self._VALID_IMAGE_EXTS:
                                        disk_files.add(os.path.abspath(entry.path))
                            except OSError:
                                continue
                except OSError:
                    pass
            if not disk_files:
                for name, kind, _sz, _mt in snap[2]:
                    if kind == "F":
                        disk_files.add(os.path.join(abs_root, name))
            return disk_files

        watched_dirs = self._collect_watched_dirs(abs_root, include_subfolders, max_depth)
        watched_set = set(watched_dirs)

        # Prune snapshots for directories outside the active watched depth/root
        for old_dir in list(self._branch_snapshots.keys()):
            if old_dir not in watched_set:
                self._branch_snapshots.pop(old_dir, None)

        # If we have no prior snapshot or no specific changed paths were recorded, inspect all watched_dirs
        # using fast per-directory snapshots and only walk branches whose snapshot changed.
        targets_to_check = set()
        if not self._branch_snapshots or not changed_paths:
            targets_to_check.update(watched_dirs)
        else:
            for cp in changed_paths:
                abs_cp = os.path.abspath(cp)
                if abs_cp in watched_set:
                    targets_to_check.add(abs_cp)
                elif self._is_ancestor_or_same(abs_root, abs_cp):
                    for wd in watched_dirs:
                        if self._is_ancestor_or_same(abs_cp, wd) or self._is_ancestor_or_same(wd, abs_cp):
                            targets_to_check.add(wd)

        # Always check root itself so new/removed direct subfolders or root files are detected
        targets_to_check.add(abs_root)

        changed_branches = set()
        for d in sorted(targets_to_check):
            new_snap = self._capture_dir_snapshot(d)
            old_snap = self._branch_snapshots.get(d)
            if old_snap != new_snap:
                self._branch_snapshots[d] = new_snap
                changed_branches.add(d)
                self.win.folder_scan_cache.pop(d, None)

        if changed_branches:
            self.win.folder_scan_cache.pop(abs_root, None)
            self.win.folder_scan_cache.pop(self.win.current_watched_dir, None)

        # If a directory's snapshot did not change (e.g., an image was only added/modified in a deeper
        # leaf subfolder while its parent directory entries & mtime remained unchanged), keep the
        # already-indexed files from unchanged branches without re-walking them.
        if self._branch_snapshots and changed_paths and not changed_branches:
            return {os.path.abspath(item["file_path"]) for item in self.win.loaded_items}

        base_parts = self._depth_split(abs_root)
        base_len = len(base_parts)
        disk_files = set()

        # Walk only branches that changed (or all watched_dirs if rebuilding snapshot state)
        dirs_to_walk = watched_dirs if not self._branch_snapshots or not changed_paths else sorted(changed_branches)
        if not dirs_to_walk:
            return {os.path.abspath(item["file_path"]) for item in self.win.loaded_items}

        # Preserve existing items from unchanged directories when doing incremental branch updates
        if self._branch_snapshots and changed_paths:
            unchanged_dirs = {
                _path_key(d) for d in watched_dirs if d not in changed_branches
            }
            for item in self.win.loaded_items:
                item_abs = os.path.abspath(item["file_path"])
                item_parent = _path_key(os.path.dirname(item_abs))
                if item_parent in unchanged_dirs:
                    disk_files.add(item_abs)

        for branch_root in dirs_to_walk:
            if not os.path.isdir(branch_root):
                continue
            for root, dirs, files in os.walk(branch_root, followlinks=False, onerror=lambda e: None):
                current_depth = len(self._depth_split(root)) - base_len
                if current_depth >= max_depth:
                    dirs[:] = []
                for f in files:
                    if os.path.splitext(f)[1].lower() in self._VALID_IMAGE_EXTS:
                        disk_files.add(os.path.abspath(os.path.join(root, f)))

        return disk_files

    @staticmethod
    def _build_external_item(file_path):
        file_name = os.path.basename(file_path)
        try:
            stats = os.stat(file_path)
            size = stats.st_size
            mtime = stats.st_mtime
        except OSError:
            size = 0
            mtime = 0

        has_comfy = False
        if file_path.lower().endswith(".png"):
            try:
                with Image.open(file_path) as img:
                    if hasattr(img, "text") and img.text and ("workflow" in img.text or "prompt" in img.text):
                        has_comfy = True
            except Exception:
                pass

        return {
            "file_path": file_path,
            "filename": file_name.lower(),
            "display_name": (file_name[:16] + '..') if len(file_name) > 18 else file_name,
            "size": size,
            "mtime": mtime,
            "thumb_path": None,
            "has_comfy": has_comfy,
        }

    def perform_smooth_folder_sync(self):
        if self._is_closing():
            return
        if not self.win.current_watched_dir or not os.path.exists(self.win.current_watched_dir):
            return

        try:
            pending_changes = set(self._pending_changed_paths)
            self._pending_changed_paths.clear()

            self.sync_file_watcher_paths(self.win.current_watched_dir)

            current_disk_files = self._collect_disk_image_paths(changed_paths=pending_changes)
            current_disk_keys = {_path_key(p) for p in current_disk_files}
            disk_by_key = {_path_key(p): os.path.abspath(p) for p in current_disk_files}

            loaded_by_key = {_path_key(item["file_path"]): item for item in self.win.loaded_items}
            loaded_keys = set(loaded_by_key.keys())

            if current_disk_keys == loaded_keys:
                if getattr(self.win, "loaded_items", None):
                    prev_selected = set(getattr(self.win.workspace_grid, "selected_file_paths", set()))
                    self.win.sort_loaded_items_quietly()
                    self.win.workspace_grid.set_cards(self.win.loaded_items)
                    if hasattr(self.win.workspace_grid, "selected_file_paths"):
                        self.win.workspace_grid.selected_file_paths = {
                            p for p in prev_selected if _path_key(p) in current_disk_keys
                        }
                    self.win.workspace_grid.update()
                return

            removed_keys = loaded_keys - current_disk_keys
            added_keys = sorted(current_disk_keys - loaded_keys)

            v_bar = self.win.scroll_area.verticalScrollBar()
            prev_scroll = v_bar.value()
            prev_selected = set(getattr(self.win.workspace_grid, "selected_file_paths", set()))

            removed_paths = set()
            if removed_keys:
                removed_paths = {os.path.abspath(loaded_by_key[k]["file_path"]) for k in removed_keys}
                self.win.loaded_items = [
                    item for item in self.win.loaded_items
                    if _path_key(item["file_path"]) not in removed_keys
                ]
                for rp in removed_paths:
                    if hasattr(self.win.workspace_grid, "pixmap_cache"):
                        self.win.workspace_grid.pixmap_cache.pop(rp, None)

            new_items = []
            added_paths = []
            for ak in added_keys:
                added_path = disk_by_key[ak]
                added_paths.append(added_path)
                new_items.append(self._build_external_item(added_path))

            if new_items:
                self.win.loaded_items.extend(new_items)

            self.win.rebuild_path_index()
            if removed_paths or added_paths:
                self.win.sort_loaded_items_quietly()

            if self.win.current_watched_dir in self.win.folder_scan_cache:
                self.win.folder_scan_cache[self.win.current_watched_dir] = list(self.win.loaded_items)

            self.win.workspace_grid.set_cards(self.win.loaded_items)
            if hasattr(self.win.workspace_grid, "selected_file_paths"):
                self.win.workspace_grid.selected_file_paths = {
                    p for p in prev_selected if _path_key(p) in current_disk_keys
                }

            self.win.workspace_grid.update()
            QTimer.singleShot(0, lambda pos=prev_scroll: v_bar.setValue(pos))

            for item in new_items:
                if not self.win.workspace_grid.has_cached_pixmap(item["file_path"]):
                    self.win.start_thumbnail_task(item["file_path"], self.win.scan_id)
        except Exception as e:
            print(f"Error during smooth folder sync: {e}")

    def toggle_subfolder_scan(self):
        if self._is_closing():
            return
        settings = load_settings(self.settings_file)
        new_state = not bool(settings.get("include_subfolders", False))
        from .helpers import save_settings
        save_settings(self.settings_file, {"include_subfolders": new_state})

        self.win.folder_scan_cache.clear()
        state_label = "ON" if new_state else "OFF"
        self.win._persistent_child_scan_status = f"Subfolder scan: {state_label}"
        if hasattr(self.win, "right_sidebar") and hasattr(self.win.right_sidebar, "log_text"):
            log_widget = self.win.right_sidebar.log_text
            if hasattr(log_widget, "appendPlainText"):
                log_widget.appendPlainText(f"• Child folder scan: {state_label}")
            else:
                current_text = log_widget.toPlainText()
                prefix = f"{current_text}\n" if current_text else ""
                log_widget.setPlainText(f"{prefix}• Child folder scan: {state_label}")
            log_widget.verticalScrollBar().setValue(
                log_widget.verticalScrollBar().maximum()
            )
        self.win.set_footer_status(self.win.build_full_gallery_status())

        if self.win.current_watched_dir and os.path.isdir(self.win.current_watched_dir):
            self.win.scan_id += 1
            active_scan = self.win.scan_id
            max_depth = max(1, int(settings.get("subfolder_depth", 1)))
            QTimer.singleShot(25, lambda fp=self.win.current_watched_dir: self.sync_file_watcher_paths(fp))
            existing_paths = (
                {os.path.abspath(item["file_path"]) for item in self.win.loaded_items}
                if new_state else set()
            )
            task = ChildFolderScanTask(
                self.win.current_watched_dir,
                active_scan,
                existing_paths,
                new_state,
                max_depth,
                self.scan_signals,
                self.win.get_active_scan_id,
                is_closing_getter=self._is_closing,
            )
            self.win.thread_pool.start(task)
        elif not getattr(self.win.workspace_grid, "selected_file_paths", None):
            self.win.set_footer_status(self.win.build_full_gallery_status())

    def on_subfolder_scan_ready(self, scan_id, root_folder, new_items, disk_files):
        if self._is_closing():
            return
        if scan_id != self.win.scan_id:
            return
        if not self.win.current_watched_dir or os.path.abspath(self.win.current_watched_dir) != root_folder:
            return

        loaded_map = {_path_key(item["file_path"]): item for item in self.win.loaded_items}
        loaded_keys = set(loaded_map.keys())
        disk_keys = {_path_key(p) for p in disk_files}
        removed_keys = loaded_keys - disk_keys

        if not removed_keys and not new_items:
            self._force_grid_refresh_and_thumbnails(scan_id)
            return

        v_bar = self.win.scroll_area.verticalScrollBar()
        prev_scroll = v_bar.value()
        prev_selected = set(getattr(self.win.workspace_grid, "selected_file_paths", set()))

        if removed_keys:
            removed_paths = {os.path.abspath(loaded_map[k]["file_path"]) for k in removed_keys}
            self.win.loaded_items = [
                item for item in self.win.loaded_items
                if _path_key(item["file_path"]) not in removed_keys
            ]
            for rp in removed_paths:
                if hasattr(self.win.workspace_grid, "pixmap_cache"):
                    self.win.workspace_grid.pixmap_cache.pop(rp, None)

        if new_items:
            existing_keys = {_path_key(item["file_path"]) for item in self.win.loaded_items}
            for item in new_items:
                if _path_key(item["file_path"]) not in existing_keys:
                    self.win.loaded_items.append(item)

        self.win.rebuild_path_index()
        self.win.sort_loaded_items_quietly()

        if hasattr(self.win.workspace_grid, "clear_cache_if_needed"):
            max_px = getattr(self.win.workspace_grid, "_MAX_PIXMAP_CACHE_ITEMS", 2000)
            self.win.workspace_grid.clear_cache_if_needed(max_allowed=max_px)

        if self.win.current_watched_dir in self.win.folder_scan_cache:
            self.win.folder_scan_cache[self.win.current_watched_dir] = list(self.win.loaded_items)

        self._force_grid_refresh_and_thumbnails(
            scan_id,
            prev_scroll=prev_scroll,
            prev_selected=prev_selected,
            disk_files=disk_files,
        )

    def _force_grid_refresh_and_thumbnails(self, scan_id, prev_scroll=None, prev_selected=None, disk_files=None):
        if self._is_closing():
            return
        self.win.needs_layout_update = False
        self.win.grid_repaint_timer.stop()
        self.win.sort_loaded_items_quietly()
        if prev_selected is not None and disk_files is not None and hasattr(self.win.workspace_grid, "selected_file_paths"):
            disk_keys = {_path_key(p) for p in disk_files}
            self.win.workspace_grid.selected_file_paths = {
                p for p in prev_selected if _path_key(p) in disk_keys
            }
        self.win.workspace_grid.set_cards(self.win.loaded_items)
        self.win.workspace_grid.update()
        self.win.scroll_area.viewport().update()

        if prev_scroll is not None:
            v_bar = self.win.scroll_area.verticalScrollBar()
            QTimer.singleShot(0, lambda pos=prev_scroll: v_bar.setValue(pos))

        # Preserve already-cached items in the progress baseline so Processed / Left
        # reflects the full scanned gallery rather than only newly queued uncached tasks.
        total_items = len(self.win.loaded_items)
        queued = 0
        skipped_uncached = 0
        max_queued = getattr(self.win, "_MAX_CHILD_SCAN_THUMB_TASKS", 512)
        for item in self.win.loaded_items:
            if not self.win.workspace_grid.has_cached_pixmap(item["file_path"]):
                if queued < max_queued:
                    self.win.start_thumbnail_task(item["file_path"], scan_id)
                    queued += 1
                else:
                    skipped_uncached += 1

        # Track total scanned items as the target and count already-cached items as ready,
        # capped to total_items so Processed + Left stays consistent even when queued tasks are capped.
        already_cached = max(0, total_items - (queued + skipped_uncached))
        self.win.total_thumbs_to_generate = total_items
        self.win.thumbs_ready_count = min(total_items, already_cached)

        comfy_task = ComfyMetadataScannerTask(
            self.win.loaded_items,
            scan_id,
            self.win.comfy_signals,
            active_scan_getter=self.win.get_active_scan_id,
        )
        self.win.thread_pool.start(comfy_task)
        self.win.set_footer_status(self.win.build_full_gallery_status())

        cap_notes = []
        if skipped_uncached > 0:
            cap_notes.append(f"thumbnail queue cap ({max_queued}/{max_queued + skipped_uncached} uncached; {skipped_uncached} deferred)")
        if hasattr(self.win.workspace_grid, "pixmap_cache"):
            max_px = getattr(self.win.workspace_grid, "_MAX_PIXMAP_CACHE_ITEMS", 2000)
            if len(self.win.loaded_items) > max_px:
                cap_notes.append(f"RAM pixmap cache cap ({max_px} items)")
        if hasattr(self.win, "file_watcher") and len(self.win.file_watcher.directories()) >= getattr(self.win, "_MAX_WATCHED_DIRS", 256):
            cap_notes.append(f"folder watcher cap ({getattr(self.win, '_MAX_WATCHED_DIRS', 256)} dirs)")

        if cap_notes:
            self.win.set_status(
                f"Child scan updated {len(self.win.loaded_items)} items — Cap protection active: {'; '.join(cap_notes)}",
                log=True,
            )
            self.win.set_footer_status(self.win.build_full_gallery_status())
