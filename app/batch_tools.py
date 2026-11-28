import gc
import io
import json
import os
import shutil
import tempfile
import time

from PIL import Image, PngImagePlugin
from PIL.ExifTags import TAGS, GPSTAGS, IFD
from PySide6.QtCore import QObject, QRunnable, Signal, QTimer
from PySide6.QtWidgets import QDialog, QFileDialog

from .dialogs import BatchCompleteDialog, BatchConvertDialog, NoImageSelectedDialog, ConfirmDestructiveDialog
from .helpers import extract_image_metadata
from .workers import BatchConvertWorker, BatchConvertWorkerSignals, ComfyMetadataScannerTask


_CLEAR_DIALOG_SCROLLBAR_QSS = """
QScrollBar,
QScrollBar:vertical,
QScrollBar:horizontal,
QScrollBar::handle:vertical,
QScrollBar::handle:horizontal,
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: palette(base);
    color: palette(text);
    border: none;
    margin: 0px;
    padding: 0px;
    min-width: 0px;
    min-height: 0px;
}
"""

_INVALID_FILENAME_CHARS = '<>:"/\\|?*'


def sanitize_filename_component(name: str, fallback: str = "untitled") -> str:
    """Sanitizes a filename stem to prevent path traversal and invalid Windows/POSIX characters."""
    if not isinstance(name, str) or not name:
        return fallback
    safe = os.path.basename(name)
    safe = safe.replace("..", "_")
    safe = "".join("_" if (ch in _INVALID_FILENAME_CHARS or ord(ch) < 32) else ch for ch in safe)
    safe = safe.strip(" .")
    return safe if safe else fallback


def _configure_non_native_file_dialog(
    parent,
    title,
    start_dir="",
    name_filter="",
    directories_only=False,
):
    """Creates a non-native QFileDialog with scrollbar stylesheet overrides cleared."""
    dialog = QFileDialog(parent, title, start_dir, name_filter)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    if directories_only:
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    else:
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)

    dialog.setStyleSheet(_CLEAR_DIALOG_SCROLLBAR_QSS)
    for child in dialog.findChildren(QObject):
        if hasattr(child, "setStyleSheet"):
            try:
                child.setStyleSheet(_CLEAR_DIALOG_SCROLLBAR_QSS)
            except Exception:
                pass
    return dialog


def _replace_with_retry(src_path, dst_path, is_closing=None, max_attempts=10, base_delay=0.05):
    """Replaces dst_path with src_path, retrying briefly on transient Windows WinError 32 locks."""
    last_err = None
    for attempt in range(max_attempts):
        if callable(is_closing) and is_closing():
            raise RuntimeError("Operation cancelled because application is closing.")
        try:
            os.replace(src_path, dst_path)
            return
        except PermissionError as e:
            last_err = e
            winerror = getattr(e, "winerror", None)
            if winerror not in (5, 32, None) and attempt == max_attempts - 1:
                raise
            time.sleep(min(0.5, base_delay * (1.5 ** attempt)))
    if last_err is not None:
        raise last_err


def write_bytes_atomic(target_path, data_bytes, is_closing=None):
    """Writes bytes to a temp file in the same directory and atomically replaces target_path."""
    target_abs = os.path.abspath(target_path)
    parent_dir = os.path.dirname(target_abs)
    os.makedirs(parent_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".part", dir=parent_dir)
    try:
        try:
            tmp_file = os.fdopen(fd, "wb")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        with tmp_file:
            tmp_file.write(data_bytes)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        try:
            shutil.copystat(target_abs, tmp_path)
        except OSError:
            pass

        _replace_with_retry(tmp_path, target_abs, is_closing=is_closing)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def write_text_atomic(target_path, text_content, encoding="utf-8", newline="", is_closing=None):
    """Writes text atomically via a same-directory temp file and os.replace."""
    normalized_text = text_content
    if newline is not None:
        normalized_text = text_content.replace("\r\n", "\n").replace("\n", newline)
    data_bytes = normalized_text.encode(encoding)
    write_bytes_atomic(target_path, data_bytes, is_closing=is_closing)


def _save_pil_atomic(img, target_path, is_closing=None, **save_kwargs):
    """Saves a PIL Image to an in-memory buffer and atomically replaces target_path."""
    ext = os.path.splitext(target_path)[1].lower()
    fmt = save_kwargs.pop("format", None) or getattr(img, "format", None)
    if not fmt:
        fmt_map = {
            ".png": "PNG",
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".webp": "WEBP",
            ".bmp": "BMP",
            ".gif": "GIF",
        }
        fmt = fmt_map.get(ext)

    work_img = img
    if fmt == "JPEG" and img.mode in ("RGBA", "LA", "PA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        if "A" in rgba.getbands():
            alpha = rgba.getchannel("A")
            bg.paste(rgba.convert("RGB"), mask=alpha)
        else:
            bg.paste(rgba.convert("RGB"))
        work_img = bg

    buf = io.BytesIO()
    if fmt:
        work_img.save(buf, format=fmt, **save_kwargs)
    else:
        work_img.save(buf, **save_kwargs)
    write_bytes_atomic(target_path, buf.getvalue(), is_closing=is_closing)


def _looks_like_comfy_prompt_graph(obj):
    """Returns True if a parsed JSON dict resembles a ComfyUI API prompt graph ({node_id: {inputs, class_type}})."""
    if not isinstance(obj, dict) or not obj:
        return False
    node_like_count = 0
    for v in obj.values():
        if isinstance(v, dict) and ("class_type" in v or "inputs" in v):
            node_like_count += 1
    return node_like_count >= 1


def _looks_like_comfy_workflow_graph(obj):
    """Returns True if a parsed JSON dict resembles a ComfyUI UI workflow graph ({nodes, links})."""
    if not isinstance(obj, dict):
        return False
    has_nodes = isinstance(obj.get("nodes"), list)
    has_links = isinstance(obj.get("links"), list)
    return has_nodes and has_links


def _extract_comfy_chunks_from_json_payload(content):
    """
    Normalizes an imported JSON payload into PNG text chunk entries for ComfyUI metadata.
    Supports:
      - Direct ComfyUI workflow JSON ({nodes, links, ...}) -> {"workflow": "<json>"}
      - Direct ComfyUI prompt JSON ({"1": {"inputs": ..., "class_type": ...}, ...}) -> {"prompt": "<json>"}
      - Exported wrapper dicts containing "workflow" and/or "prompt" keys/strings
    Returns a dict mapping PNG text chunk key ("workflow" / "prompt") -> JSON string.
    """
    chunks = {}
    if not isinstance(content, dict):
        return chunks

    # Case 1: Wrapper dict explicitly carrying "workflow" and/or "prompt"
    for key in ("workflow", "prompt"):
        if key in content and content[key] is not None:
            val = content[key]
            if isinstance(val, str):
                stripped = val.strip()
                if stripped:
                    # Avoid passing huge strings to json.loads on worker threads if they
                    # can trigger C-level stack/buffer pressure alongside GUI painting.
                    if len(stripped) <= 2_000_000:
                        try:
                            parsed = json.loads(stripped)
                            chunks[key] = json.dumps(parsed, separators=(",", ":"))
                        except Exception:
                            chunks[key] = stripped
                    else:
                        chunks[key] = stripped
            elif isinstance(val, (dict, list)):
                chunks[key] = json.dumps(val)

    if chunks:
        return chunks

    # Case 2: Raw ComfyUI workflow JSON file (e.g., *_workflow.json)
    if _looks_like_comfy_workflow_graph(content):
        chunks["workflow"] = json.dumps(content)
        return chunks

    # Case 3: Raw ComfyUI prompt graph JSON file
    if _looks_like_comfy_prompt_graph(content):
        chunks["prompt"] = json.dumps(content)
        return chunks

    return chunks


class _BatchTaskSignals(QObject):
    progress = Signal(str)
    finished = Signal(str, str, list)

    def __init__(self, parent=None):
        super().__init__(parent)


class _BatchFunctionWorker(QRunnable):
    """Executes batch EXIF / ComfyUI file operations off the GUI thread with shutdown guards."""

    def __init__(self, fn, signals, is_closing_getter=None, on_complete=None):
        super().__init__()
        self.setAutoDelete(True)
        self.fn = fn
        self.signals = signals
        self.is_closing_getter = is_closing_getter
        self.on_complete = on_complete

    def _is_closing(self):
        return callable(self.is_closing_getter) and self.is_closing_getter()

    def _emit_progress(self, message):
        if self._is_closing() or self.signals is None:
            return
        try:
            self.signals.progress.emit(message)
        except RuntimeError:
            pass

    def _emit_finished(self, title, msg, modified_paths=None):
        safe_paths = list(modified_paths or [])
        if callable(self.on_complete):
            try:
                self.on_complete(self.signals)
            except Exception:
                pass
        if self._is_closing() or self.signals is None:
            return
        try:
            self.signals.finished.emit(title, msg, safe_paths)
        except RuntimeError:
            pass

    def run(self):
        if self._is_closing():
            if callable(self.on_complete):
                try:
                    self.on_complete(self.signals)
                except Exception:
                    pass
            return
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        gc_was_enabled = gc.isenabled()
        if gc_was_enabled:
            gc.disable()
        try:
            result = self.fn(self._emit_progress, self._is_closing)
            if isinstance(result, tuple) and len(result) == 3:
                title, msg, modified_paths = result
            else:
                title, msg = result
                modified_paths = []
        except Exception as e:
            title, msg, modified_paths = "Batch Operation Failed", f"Batch operation failed: {e}", []
        finally:
            if gc_was_enabled and not gc.isenabled():
                try:
                    gc.enable()
                except Exception:
                    pass
        self._emit_finished(title, msg, modified_paths)


class BatchToolsController:
    """Handles batch image conversion and EXIF / ComfyUI workflow import/export/purge."""

    def __init__(self, win):
        self.win = win
        self._active_dialog = None
        self._picker_busy = False
        self.convert_signals = None
        self._active_task_signals = set()

    def _is_closing(self):
        return bool(getattr(self.win, "_is_closing", False))

    def _get_selected_existing_paths(self, action_label="batch operation"):
        """Returns selected existing file paths, or prompts the user to select an image first."""
        grid = getattr(self.win, "workspace_grid", None)
        selected_raw = list(getattr(grid, "selected_file_paths", None) or []) if grid is not None else []
        paths = [os.path.abspath(p) for p in selected_raw if p and os.path.exists(p)]
        if paths:
            return paths

        msg = f"Please select at least one image before running {action_label}."
        self.win.set_status(msg)
        if not self._is_closing():
            parent = self.win if self.win is not None and self.win.isVisible() else None
            dialog = NoImageSelectedDialog(action_label, parent=parent)
            dialog.exec()
        return []

    def _confirm_destructive_operation(self, title, headline, detail_text, cancel_status):
        """Shows a warning dialog for potentially destructive operations and returns True if confirmed."""
        if self._is_closing():
            return False
        parent = self.win if self.win is not None and self.win.isVisible() else None
        dialog = ConfirmDestructiveDialog(title, headline, detail_text, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.win.set_status(cancel_status)
            return False
        return True

    def _select_open_file(self, title, start_dir="", name_filter=""):
        if self._is_closing() or self._picker_busy:
            return ""
        self._picker_busy = True
        try:
            parent = self.win if self.win is not None and self.win.isVisible() else None
            dialog = _configure_non_native_file_dialog(
                parent,
                title,
                start_dir=start_dir,
                name_filter=name_filter,
                directories_only=False,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return ""
            selected = dialog.selectedFiles()
            return selected[0] if selected else ""
        finally:
            self._picker_busy = False

    def _select_existing_directory(self, title, start_dir=""):
        if self._is_closing() or self._picker_busy:
            return ""
        self._picker_busy = True
        try:
            parent = self.win if self.win is not None and self.win.isVisible() else None
            dialog = _configure_non_native_file_dialog(
                parent,
                title,
                start_dir=start_dir,
                directories_only=True,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return ""
            selected = dialog.selectedFiles()
            return selected[0] if selected else ""
        finally:
            self._picker_busy = False

    def _release_task_signals(self, sig_obj):
        if sig_obj is not None:
            self._active_task_signals.discard(sig_obj)

    def _run_background_batch(self, worker_fn):
        if self._is_closing():
            return
        from PySide6.QtCore import Qt
        task_signals = _BatchTaskSignals(self.win)
        self._active_task_signals.add(task_signals)
        task_signals.progress.connect(self.win.set_status, Qt.ConnectionType.QueuedConnection)
        task_signals.finished.connect(self._on_background_batch_finished, Qt.ConnectionType.QueuedConnection)
        worker = _BatchFunctionWorker(
            worker_fn,
            task_signals,
            is_closing_getter=self._is_closing,
            on_complete=self._release_task_signals,
        )
        self.win.thread_pool.start(worker)

    def _refresh_modified_files_metadata(self, modified_paths):
        """Refreshes ComfyUI badges, grid cards, and inspector metadata for modified files."""
        if not modified_paths or self._is_closing():
            return

        abs_paths = []
        norm_set = set()
        for p in modified_paths:
            if not p:
                continue
            ap = os.path.abspath(str(p))
            abs_paths.append(ap)
            norm_set.add(os.path.normcase(ap))

        if not abs_paths:
            return

        items_by_path = getattr(self.win, "items_by_path", None)
        loaded_items = getattr(self.win, "loaded_items", None)
        modified_items = []

        if isinstance(loaded_items, list):
            for item in loaded_items:
                if not isinstance(item, dict):
                    continue
                fp = item.get("file_path")
                if fp and os.path.normcase(os.path.abspath(fp)) in norm_set:
                    item["has_comfy"] = False
                    modified_items.append(item)

        if isinstance(items_by_path, dict):
            for fp, item in items_by_path.items():
                if isinstance(item, dict) and fp and os.path.normcase(os.path.abspath(fp)) in norm_set:
                    item["has_comfy"] = False
                    if item not in modified_items:
                        modified_items.append(item)

        folder_scan_cache = getattr(self.win, "folder_scan_cache", None)
        if isinstance(folder_scan_cache, dict):
            for ap in abs_paths:
                parent_dir = os.path.dirname(ap)
                if parent_dir:
                    folder_scan_cache.pop(parent_dir, None)
            watched = getattr(self.win, "current_watched_dir", None)
            if watched:
                folder_scan_cache.pop(os.path.abspath(watched), None)

        # Directly inspect modified files on the spot for ComfyUI metadata so the badge
        # and ComfyUI tab update immediately without waiting on background scanner queue/timing.
        any_comfy_found = False
        for item in modified_items:
            fp = item.get("file_path")
            has_c = False
            if fp and os.path.isfile(fp) and fp.lower().endswith(".png"):
                try:
                    with Image.open(fp) as img:
                        txt = getattr(img, "text", None) or {}
                        has_c = bool(txt.get("workflow") or txt.get("prompt"))
                except Exception:
                    has_c = False
            item["has_comfy"] = has_c
            if has_c:
                any_comfy_found = True

        # Keep folder_scan_cache in sync with updated item dicts so cached folder reloads
        # also reflect the newly injected ComfyUI metadata.
        watched = getattr(self.win, "current_watched_dir", None)
        if isinstance(folder_scan_cache, dict) and watched and isinstance(loaded_items, list):
            folder_scan_cache[os.path.abspath(watched)] = list(loaded_items)

        workspace_grid = getattr(self.win, "workspace_grid", None)
        if workspace_grid is not None:
            if hasattr(self.win, "loaded_items") and hasattr(workspace_grid, "set_cards"):
                workspace_grid.set_cards(self.win.loaded_items)
            elif hasattr(workspace_grid, "update"):
                workspace_grid.update()
        schedule_repaint = getattr(self.win, "_schedule_grid_repaint", None)
        if callable(schedule_repaint):
            schedule_repaint(relayout=False)

        workspace_grid = getattr(self.win, "workspace_grid", None)
        selected_paths = getattr(workspace_grid, "selected_file_paths", None) if workspace_grid is not None else None
        if selected_paths:
            selected_abs = {os.path.normcase(os.path.abspath(p)) for p in selected_paths if p}
            matched_selected = [ap for ap in abs_paths if os.path.normcase(ap) in selected_abs]
            if matched_selected:
                first_matched = matched_selected[0]
                sidebar = getattr(self.win, "right_sidebar", None)
                comfy_view = getattr(sidebar, "comfy_view", None) if sidebar is not None else None
                if comfy_view is not None and first_matched.lower().endswith(".png"):
                    try:
                        with Image.open(first_matched) as img:
                            txt = getattr(img, "text", None) or {}
                            raw_wf = txt.get("workflow") or txt.get("prompt") or ""
                            if raw_wf:
                                try:
                                    comfy_view.setPlainText(json.dumps(json.loads(raw_wf), indent=4))
                                except Exception:
                                    comfy_view.setPlainText(raw_wf)
                    except Exception:
                        pass
                if hasattr(self.win, "on_selection_changed"):
                    self.win.on_selection_changed(list(selected_paths))

    def _on_background_batch_finished(self, title, msg, modified_paths=None):
        """Completes batch UI refresh and shows completion dialog on the GUI thread."""
        if self._is_closing():
            return

        pending_paths = list(modified_paths or [])
        status_text = msg
        dialog_title = title
        dialog_msg = msg

        if pending_paths:
            try:
                self._refresh_modified_files_metadata(pending_paths)
            except Exception as e:
                print(f"Error refreshing modified files metadata: {e}")

        def _finalize_after_dialog():
            if self._is_closing():
                return
            try:
                if pending_paths:
                    self._refresh_modified_files_metadata(pending_paths)
            except Exception as e:
                print(f"Error refreshing modified files metadata: {e}")
            try:
                self.win.set_status(status_text)
            except Exception:
                pass

        try:
            parent = self.win if self.win is not None and self.win.isVisible() else None
            self._active_dialog = BatchCompleteDialog(parent, dialog_title, dialog_msg)
            self._active_dialog.exec()
        finally:
            self._active_dialog = None
            QTimer.singleShot(50, _finalize_after_dialog)

    def batch_convert_images(self):
        paths = self._get_selected_existing_paths("batch conversion")
        if not paths:
            return

        self._active_dialog = BatchConvertDialog(self.win)
        if self._active_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        target_format = self._active_dialog.format_combo.currentText()
        quality = self._active_dialog.quality_slider.value()
        transfer_exif = self._active_dialog.transfer_exif_cb.isChecked()
        remove_original = self._active_dialog.remove_original_cb.isChecked()

        self.win.set_status("Starting background batch image conversion...")
        self.convert_signals = BatchConvertWorkerSignals()
        self.convert_signals.progress_update.connect(self.win.set_status)
        self.convert_signals.finished.connect(lambda s, f, msg: self._on_batch_convert_finished(s, f, msg, paths))

        worker = BatchConvertWorker(paths, target_format, quality, transfer_exif, remove_original, self.convert_signals)
        self.win.thread_pool.start(worker)

    def _on_batch_convert_finished(self, success_count, failed_count, msg, original_paths):
        if self._is_closing():
            return

        self.win.set_status(msg)
        scan_cache = getattr(self.win, "folder_scan_cache", None)
        if success_count > 0 and original_paths:
            affected_dirs = {
                os.path.abspath(os.path.dirname(p))
                for p in original_paths
                if p and os.path.dirname(p)
            }
            if isinstance(scan_cache, dict):
                for folder in affected_dirs:
                    scan_cache.pop(folder, None)

            watched = getattr(self.win, "current_watched_dir", None)
            if watched and os.path.isdir(watched):
                abs_watched = os.path.abspath(watched)
                if isinstance(scan_cache, dict):
                    scan_cache.pop(abs_watched, None)
                if hasattr(self.win, "folder_sync"):
                    self.win.folder_sync.perform_smooth_folder_sync()
                else:
                    self.win.load_images_from_folder(abs_watched)

        BatchCompleteDialog(
            self.win,
            "Batch Conversion Complete" if success_count > 0 else "Batch Conversion Failed",
            msg
        ).exec()

    def batch_purge_exif(self):
        paths = self._get_selected_existing_paths("EXIF purge")
        if not paths:
            return

        if not self._confirm_destructive_operation(
            "Warning: Destructive Operation",
            f"Purge EXIF metadata from {len(paths)} selected image(s)?",
            (
                "This operation is potentially destructive and modifies your image files in place. "
                "Existing EXIF/metadata tags will be permanently removed and lossy formats may be re-encoded."
            ),
            "EXIF purge cancelled.",
        ):
            return

        self.win.set_status("Purging EXIF metadata in background...")

        def _work(emit_progress, is_closing):
            success_count = 0
            failed_count = 0
            modified_paths = []
            for idx, path in enumerate(paths, 1):
                if is_closing():
                    break
                try:
                    emit_progress(f"Purging EXIF ({idx}/{len(paths)}): {os.path.basename(path)}")
                    ext = os.path.splitext(path)[1].lower()
                    with Image.open(path) as img:
                        img.load()
                        icc_profile = img.info.get("icc_profile")
                        clean_img = Image.new(img.mode, img.size)
                        if img.mode == "P":
                            palette = img.getpalette()
                            if palette:
                                clean_img.putpalette(palette)
                        clean_img.paste(img)

                    save_kwargs = {}
                    if icc_profile:
                        save_kwargs["icc_profile"] = icc_profile
                    if ext in (".jpg", ".jpeg"):
                        save_kwargs["quality"] = 95
                        save_kwargs["subsampling"] = 0
                        save_kwargs["exif"] = b""
                    elif ext == ".webp":
                        save_kwargs["quality"] = 95
                        save_kwargs["exif"] = b""

                    _save_pil_atomic(clean_img, path, is_closing=is_closing, **save_kwargs)
                    success_count += 1
                    modified_paths.append(path)
                except Exception as e:
                    failed_count += 1
                    print(f"Failed to purge EXIF for {path}: {e}")

            msg = f"Successfully purged EXIF data from {success_count} images."
            if failed_count > 0:
                msg += f" ({failed_count} failed)."
            return (
                "EXIF Purge Complete" if success_count > 0 or failed_count == 0 else "EXIF Purge Failed",
                msg,
                modified_paths,
            )

        self._run_background_batch(_work)

    def batch_export_exif(self):
        paths = self._get_selected_existing_paths("EXIF export")
        if not paths:
            return

        export_dir = self._select_existing_directory("Select Export Directory for EXIF JSON")
        if not export_dir:
            return

        self.win.set_status("Exporting EXIF metadata in background...")

        def _work(emit_progress, is_closing):
            success_count = 0
            failed_count = 0
            last_json_filename = ""
            export_abs = os.path.abspath(export_dir)

            for idx, path in enumerate(paths, 1):
                if is_closing():
                    break
                try:
                    emit_progress(f"Exporting EXIF ({idx}/{len(paths)}): {os.path.basename(path)}")
                    raw_stem = os.path.splitext(os.path.basename(path))[0]
                    safe_stem = sanitize_filename_component(raw_stem, fallback=f"image_{idx}")
                    json_filename = f"{safe_stem}_EXIF.json"
                    out_json = os.path.abspath(os.path.join(export_abs, json_filename))
                    if os.path.commonpath([export_abs, out_json]) != export_abs:
                        failed_count += 1
                        continue
                    last_json_filename = json_filename

                    _, metadata_dict = extract_image_metadata(path)
                    payload = json.dumps(metadata_dict, indent=4, default=str)
                    write_text_atomic(out_json, payload, encoding="utf-8", newline="\n", is_closing=is_closing)
                    success_count += 1
                except Exception as e:
                    failed_count += 1
                    print(f"Failed to export EXIF for {path}: {e}")

            if success_count == 1:
                msg = f"Exported EXIF report as {last_json_filename}"
            elif success_count > 1:
                msg = f"Exported {success_count} EXIF reports (e.g., {last_json_filename})"
            else:
                msg = "Failed to export EXIF reports."

            if failed_count > 0 and success_count > 0:
                msg += f" ({failed_count} failed)."

            return "EXIF Export Complete", msg, []

        self._run_background_batch(_work)

    def batch_inject_exif(self):
        paths = self._get_selected_existing_paths("EXIF injection")
        if not paths:
            return

        source_filter = "Source Files (*.jpg *.jpeg *.png *.webp *.json)"
        source_file = self._select_open_file(
            "Select EXIF Source (Image or JSON Report)", "", source_filter
        )
        if not source_file:
            return

        if not self._confirm_destructive_operation(
            "Warning: Destructive Operation",
            f"Inject EXIF metadata from '{os.path.basename(source_file)}' into {len(paths)} selected image(s)?",
            (
                "This operation is potentially destructive and overwrites your target image files in place. "
                "Existing metadata may be replaced or altered and lossy files may be re-encoded."
            ),
            "EXIF injection cancelled.",
        ):
            return

        self.win.set_status("Injecting EXIF metadata in background...")

        def _work(emit_progress, is_closing):
            exif_data_bytes = None
            exif_dict = {}
            comfy_chunks = {}

            if source_file.lower().endswith(".json"):
                with open(source_file, "r", encoding="utf-8") as f:
                    content = json.load(f)
                if isinstance(content, dict):
                    exif_dict = dict(content)
                    comfy_chunks = _extract_comfy_chunks_from_json_payload(content)
            else:
                exif_data_bytes, exif_dict = extract_image_metadata(source_file)
                try:
                    with Image.open(source_file) as src_img:
                        if not exif_data_bytes and hasattr(src_img, "getexif"):
                            raw_exif = src_img.getexif()
                            if raw_exif:
                                try:
                                    exif_data_bytes = raw_exif.tobytes()
                                except Exception:
                                    pass
                        if getattr(src_img, "text", None):
                            for k in ("workflow", "prompt"):
                                if k in src_img.text and src_img.text[k]:
                                    comfy_chunks[k] = src_img.text[k]
                except Exception:
                    pass

            # Also pick up workflow/prompt entries if present inside a full EXIF JSON dump
            if not comfy_chunks and isinstance(exif_dict, dict):
                comfy_chunks = _extract_comfy_chunks_from_json_payload(exif_dict)

            ifd0_rev = {str(v): k for k, v in TAGS.items()}
            exif_ifd_rev = {str(v): k for k, v in TAGS.items()}
            gps_ifd_rev = {str(v): k for k, v in GPSTAGS.items()}

            def _resolve_tag_id(key, rev_map):
                if isinstance(key, int):
                    return key
                k_str = str(key).strip()
                if k_str.isdigit():
                    return int(k_str)
                if k_str.startswith("0x") or k_str.startswith("0X"):
                    try:
                        return int(k_str, 16)
                    except ValueError:
                        pass
                return rev_map.get(k_str)

            def _populate_ifd(target_ifd, source_mapping, rev_map):
                if not isinstance(source_mapping, dict):
                    return
                for raw_k, raw_v in source_mapping.items():
                    if raw_v is None:
                        continue
                    tag_id = _resolve_tag_id(raw_k, rev_map)
                    if tag_id is None:
                        continue
                    val = json.dumps(raw_v) if isinstance(raw_v, (dict, list)) else raw_v
                    try:
                        target_ifd[tag_id] = val
                    except Exception:
                        try:
                            target_ifd[tag_id] = str(val)
                        except Exception:
                            pass

            def _build_exif_bytes_from_dict(base_target, mapping):
                exif_obj = Image.Exif()
                try:
                    existing = base_target.getexif()
                    if existing:
                        for tid, tval in existing.items():
                            exif_obj[tid] = tval
                except Exception:
                    pass

                # Support nested sub-IFD dicts ("Exif", "GPS") if present in JSON/metadata
                nested_exif = mapping.get("Exif") if isinstance(mapping.get("Exif"), dict) else None
                nested_gps = mapping.get("GPS") if isinstance(mapping.get("GPS"), dict) else None

                top_level = {
                    k: v for k, v in mapping.items()
                    if k not in ("Exif", "GPS", "workflow", "prompt")
                }
                _populate_ifd(exif_obj, top_level, ifd0_rev)

                if nested_exif:
                    try:
                        sub_exif = exif_obj.get_ifd(IFD.Exif)
                        _populate_ifd(sub_exif, nested_exif, exif_ifd_rev)
                    except Exception:
                        _populate_ifd(exif_obj, nested_exif, exif_ifd_rev)

                if nested_gps:
                    try:
                        sub_gps = exif_obj.get_ifd(IFD.GPSInfo)
                        _populate_ifd(sub_gps, nested_gps, gps_ifd_rev)
                    except Exception:
                        _populate_ifd(exif_obj, nested_gps, gps_ifd_rev)

                return exif_obj.tobytes()

            success_count = 0
            failed_count = 0
            modified_paths = []

            for idx, path in enumerate(paths, 1):
                if is_closing():
                    break
                try:
                    emit_progress(f"Injecting EXIF ({idx}/{len(paths)}): {os.path.basename(path)}")
                    file_ext = os.path.splitext(path)[1].lower()
                    save_kwargs = {}
                    with Image.open(path) as target:
                        target.load()
                        if file_ext == ".png":
                            meta = PngImagePlugin.PngInfo()
                            # Preserve existing PNG text chunks first (so existing workflow/prompt stay unless overridden)
                            existing_text = {}
                            if hasattr(target, "text") and target.text:
                                for k, v in target.text.items():
                                    if not str(k).lower().startswith("jfif"):
                                        existing_text[str(k)] = str(v)

                            # Apply imported ComfyUI chunks under canonical 'workflow' / 'prompt' keys
                            for k, v in comfy_chunks.items():
                                existing_text[str(k)] = str(v)

                            # Apply remaining metadata entries while avoiding overwriting canonical ComfyUI keys
                            # with non-JSON or stringified wrapper values unless no canonical chunk was extracted
                            for k, v in exif_dict.items():
                                k_str = str(k)
                                if k_str.lower().startswith("jfif"):
                                    continue
                                if k_str in ("workflow", "prompt") and k_str in comfy_chunks:
                                    continue
                                if k_str not in existing_text:
                                    existing_text[k_str] = (
                                        json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                                    )

                            for k, v in existing_text.items():
                                meta.add_text(k, v)

                            save_kwargs["pnginfo"] = meta
                        else:
                            if file_ext in (".jpg", ".jpeg", ".webp"):
                                save_kwargs["quality"] = 95
                            if exif_data_bytes:
                                save_kwargs["exif"] = exif_data_bytes
                            elif exif_dict:
                                try:
                                    built_bytes = _build_exif_bytes_from_dict(target, exif_dict)
                                    if built_bytes:
                                        save_kwargs["exif"] = built_bytes
                                except Exception:
                                    pass

                        _save_pil_atomic(target, path, is_closing=is_closing, **save_kwargs)
                    success_count += 1
                    modified_paths.append(path)
                except Exception as e:
                    failed_count += 1
                    print(f"Failed to inject EXIF for {path}: {e}")

            msg = f"Successfully injected metadata into {success_count} images."
            if failed_count > 0:
                msg += f" ({failed_count} failed)."

            return (
                "EXIF Injection Complete" if success_count > 0 or failed_count == 0 else "EXIF Injection Failed",
                msg,
                modified_paths,
            )

        self._run_background_batch(_work)

    def batch_export_comfy_workflow(self):
        paths = self._get_selected_existing_paths("ComfyUI workflow export")
        if not paths:
            return

        export_dir = self._select_existing_directory("Select Export Directory for ComfyUI Workflows")
        if not export_dir:
            return

        self.win.set_status("Exporting ComfyUI workflows in background...")

        def _work(emit_progress, is_closing):
            success_count = 0
            failed_count = 0
            skipped_non_png = 0
            export_abs = os.path.abspath(export_dir)

            for idx, path in enumerate(paths, 1):
                if is_closing():
                    break
                if not path.lower().endswith(".png"):
                    skipped_non_png += 1
                    continue

                emit_progress(f"Scanning workflow ({idx}/{len(paths)}): {os.path.basename(path)}")
                try:
                    workflow_content = None
                    with Image.open(path) as img:
                        if hasattr(img, "text") and img.text:
                            for key in ["workflow", "prompt"]:
                                if key in img.text:
                                    workflow_content = img.text[key]
                                    break

                    if workflow_content:
                        raw_stem = os.path.splitext(os.path.basename(path))[0]
                        safe_stem = sanitize_filename_component(raw_stem, fallback=f"workflow_{idx}")
                        out_json = os.path.abspath(os.path.join(export_abs, f"{safe_stem}_workflow.json"))
                        if os.path.commonpath([export_abs, out_json]) != export_abs:
                            failed_count += 1
                            continue
                        try:
                            parsed_json = json.loads(workflow_content)
                            out_text = json.dumps(parsed_json, indent=4)
                        except Exception:
                            out_text = workflow_content
                        write_text_atomic(out_json, out_text, encoding="utf-8", newline="\n", is_closing=is_closing)
                        success_count += 1
                    else:
                        failed_count += 1
                except Exception as e:
                    failed_count += 1
                    print(f"Failed to export workflow for {path}: {e}")

            if success_count > 0:
                msg = f"Successfully exported {success_count} ComfyUI workflow(s)."
                details = []
                if failed_count > 0:
                    details.append(f"{failed_count} without workflow/failed")
                if skipped_non_png > 0:
                    details.append(f"{skipped_non_png} non-PNG skipped")
                if details:
                    msg += f" ({', '.join(details)})."
                return "ComfyUI Workflow Export Complete", msg, []

            if skipped_non_png == len(paths):
                return (
                    "Export Failed",
                    "No PNG images were selected (ComfyUI workflows are stored in PNG metadata).",
                    [],
                )

            return (
                "Export Failed",
                "No valid ComfyUI workflows found in the selected images.",
                [],
            )

        self._run_background_batch(_work)
