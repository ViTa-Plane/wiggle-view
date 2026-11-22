import io
import os
import struct
import tempfile
from PIL import Image, PngImagePlugin
from PIL.ExifTags import TAGS, GPSTAGS

from PySide6.QtCore import QRunnable, Signal, QObject
from PySide6.QtGui import QImage
from .helpers import extract_image_metadata, load_settings
from .sqlite_thumb_cache import ThumbnailSQLiteCache


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_COMFY_TEXT_KEYS = (b"workflow", b"prompt")


def _png_has_comfy_metadata(file_path):
    """Fast PNG chunk scan for ComfyUI 'workflow'/'prompt' text chunks; stops at IDAT."""
    try:
        with open(file_path, "rb") as f:
            if f.read(8) != _PNG_SIGNATURE:
                return False
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                chunk_len, chunk_type = struct.unpack(">I4s", header)
                if chunk_type == b"IDAT" or chunk_type == b"IEND":
                    return False
                if chunk_type in (b"tEXt", b"iTXt", b"zTXt"):
                    if chunk_len <= 16 * 1024 * 1024:
                        data = f.read(chunk_len)
                        if len(data) < chunk_len:
                            return False
                        nul_idx = data.find(b"\x00")
                        key = data[:nul_idx] if nul_idx != -1 else data
                        if key in _COMFY_TEXT_KEYS:
                            return True
                    else:
                        f.seek(chunk_len + 4, os.SEEK_CUR)
                        continue
                else:
                    f.seek(chunk_len, os.SEEK_CUR)
                # Skip 4-byte CRC
                if len(f.read(4)) < 4:
                    return False
    except OSError:
        return False


class FolderScannerSignals(QObject):
    batch_scanned = Signal(int, list)
    scan_complete = Signal(int, int)


class FolderScannerTask(QRunnable):
    def __init__(self, folder_path, scan_id, signals, active_scan_getter=None, settings_file=None):
        super().__init__()
        self.folder_path = folder_path
        self.scan_id = scan_id
        self.signals = signals
        self.active_scan_getter = active_scan_getter
        self.settings_file = settings_file

    def _is_cancelled(self):
        return self.active_scan_getter is not None and self.active_scan_getter() != self.scan_id

    def _build_item_entry(self, file_path, filename, stat_result):
        display_name = (filename[:16] + '..') if len(filename) > 18 else filename

        if stat_result is not None:
            file_size = stat_result.st_size
            file_mtime = stat_result.st_mtime
        else:
            file_size = 0
            file_mtime = 0

        return {
            "file_path": file_path,
            "display_name": display_name,
            "thumb_path": None,
            "size": file_size,
            "mtime": file_mtime,
            "filename": filename.lower(),
            "dirname_lower": os.path.dirname(file_path).lower(),
            "has_comfy": False
        }

    def _flush_batch(self, batch, total_count):
        flush_threshold = 20 if total_count <= 20 else 100
        if len(batch) >= flush_threshold and not self._is_cancelled():
            self.signals.batch_scanned.emit(self.scan_id, batch)
            return [], total_count
        return batch, total_count

    def _scan_single_folder(self, valid_extensions):
        batch = []
        total_count = 0

        with os.scandir(self.folder_path) as entries:
            for entry in entries:
                if self._is_cancelled():
                    return batch, total_count
                if not entry.is_file():
                    continue

                filename = entry.name
                if not filename.lower().endswith(valid_extensions):
                    continue

                try:
                    stat_result = entry.stat()
                except Exception:
                    stat_result = None

                batch.append(self._build_item_entry(entry.path, filename, stat_result))
                total_count += 1
                batch, total_count = self._flush_batch(batch, total_count)

        return batch, total_count

    def _scan_with_subfolders(self, valid_extensions, max_depth=None):
        batch = []
        total_count = 0
        base_depth = len(os.path.normpath(self.folder_path).split(os.sep))

        for root, dirs, files in os.walk(self.folder_path, followlinks=False, onerror=lambda e: None):
            if self._is_cancelled():
                return batch, total_count

            current_depth = len(os.path.normpath(root).split(os.sep)) - base_depth
            if max_depth is not None and current_depth >= max_depth:
                dirs[:] = []

            for filename in files:
                if self._is_cancelled():
                    return batch, total_count
                if not filename.lower().endswith(valid_extensions):
                    continue

                full_path = os.path.join(root, filename)
                try:
                    stat_result = os.stat(full_path)
                except Exception:
                    stat_result = None

                batch.append(self._build_item_entry(full_path, filename, stat_result))
                total_count += 1
                batch, total_count = self._flush_batch(batch, total_count)

        return batch, total_count

    def run(self):
        valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
        settings = load_settings(self.settings_file) if self.settings_file else load_settings()
        include_subfolders = settings.get("include_subfolders", False)
        folder_depth = max(1, int(settings.get("subfolder_depth", 1)))

        try:
            if not os.path.isdir(self.folder_path) or self._is_cancelled():
                return

            if include_subfolders:
                batch, total_count = self._scan_with_subfolders(
                    valid_extensions, max_depth=folder_depth
                )
            else:
                batch, total_count = self._scan_single_folder(valid_extensions)

            if self._is_cancelled():
                return

            if batch:
                self.signals.batch_scanned.emit(self.scan_id, batch)

            self.signals.scan_complete.emit(self.scan_id, total_count)
        except Exception:
            pass


class ThumbnailSignalEmitter(QObject):
    thumbnail_ready = Signal(int, str, str, QImage)  # scan_id, file_path, thumb_path, decoded_qimage


class ThumbnailLoaderTask(QRunnable):
    """Generates or loads cached thumbnails off the GUI thread using thread-safe QImage."""

    def __init__(
        self,
        file_path,
        cache_dir,
        target_size,
        signals,
        scan_id=0,
        active_scan_getter=None,
        existing_thumb_path=None,
        jpeg_quality=None,
        persist_to_db=True,
        encrypt_thumbnail_cache=None,
    ):
        super().__init__()
        self.file_path = file_path
        self.cache_dir = cache_dir
        self.target_size = target_size
        self.signals = signals
        self.scan_id = scan_id
        self.active_scan_getter = active_scan_getter
        self.existing_thumb_path = existing_thumb_path
        self.jpeg_quality = jpeg_quality
        self.persist_to_db = bool(persist_to_db)
        self.encrypt_thumbnail_cache = (
            None if encrypt_thumbnail_cache is None else bool(encrypt_thumbnail_cache)
        )

    def _is_cancelled(self):
        return self.active_scan_getter is not None and self.active_scan_getter() != self.scan_id

    def run(self):
        if self._is_cancelled():
            return

        try:
            settings = None
            if self.jpeg_quality is None:
                settings = load_settings()
                raw_q = settings.get("thumbnail_jpeg_quality", 88)
                try:
                    jpeg_quality = max(1, min(100, int(raw_q)))
                except (TypeError, ValueError):
                    jpeg_quality = 88
            else:
                jpeg_quality = max(1, min(100, int(self.jpeg_quality)))

            if self.encrypt_thumbnail_cache is None:
                if settings is None:
                    settings = load_settings()
                encrypt_enabled = bool(settings.get("encrypt_thumbnail_cache", True))
            else:
                encrypt_enabled = self.encrypt_thumbnail_cache

            cache_dir = self.cache_dir or os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")
            db_cache = ThumbnailSQLiteCache.get_shared(
                cache_dir,
                encrypt_enabled=encrypt_enabled,
            )
            qimg, virtual_path = db_cache.generate_and_cache_qimage(
                self.file_path,
                target_size=self.target_size,
                jpeg_quality=jpeg_quality,
                persist_to_db=self.persist_to_db,
            )
            if not self._is_cancelled() and qimg is not None and not qimg.isNull():
                self.signals.thumbnail_ready.emit(self.scan_id, self.file_path, virtual_path or "", qimg)
        except Exception:
            pass


class BatchConvertWorkerSignals(QObject):
    progress_update = Signal(str)
    finished = Signal(int, int, str)


def _prepare_image_for_target(img, target_ext):
    """Materializes a standalone PIL Image with a mode supported by the target encoder."""
    img.load()
    has_palette_transparency = (
        img.mode == "P" and "transparency" in getattr(img, "info", {})
    )

    if target_ext in ("jpg", "jpeg"):
        if has_palette_transparency or img.mode in ("RGBA", "LA", "PA"):
            rgba = img.convert("RGBA")
            try:
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[-1])
                return background
            finally:
                rgba.close()
        if img.mode != "RGB":
            return img.convert("RGB")
        return img.copy()

    if target_ext == "webp":
        # WebP encoder (libwebp) expects RGB or RGBA; materialize cleanly without fp ties
        if has_palette_transparency or img.mode in ("RGBA", "LA", "PA"):
            converted = img.convert("RGBA")
        elif img.mode in ("RGB", "RGBA"):
            converted = img.copy()
        else:
            converted = img.convert("RGB")
        # Ensure contiguous pixel buffer in RGB/RGBA
        if converted.mode not in ("RGB", "RGBA"):
            fixed = converted.convert("RGBA")
            converted.close()
            converted = fixed
        return converted

    if target_ext == "png":
        if has_palette_transparency or img.mode in ("LA", "PA"):
            return img.convert("RGBA")
        return img.copy()

    return img.copy()


def _encode_image_bytes(img, save_format, save_kwargs):
    """Encodes a PIL Image into memory via BytesIO to avoid direct path-open issues on worker threads."""
    buf = io.BytesIO()
    img.save(buf, format=save_format, **save_kwargs)
    return buf.getvalue()


class BatchConvertWorker(QRunnable):
    def __init__(self, paths, target_format, quality, transfer_exif, remove_original, signals):
        super().__init__()
        self.paths = paths
        self.target_format = target_format
        self.quality = quality
        self.transfer_exif = transfer_exif
        self.remove_original = remove_original
        self.signals = signals

    def run(self):
        success_count = 0
        failed_count = 0
        target_ext = self.target_format.lower()
        if target_ext == "jpg":
            target_ext = "jpeg"
        save_format = "JPEG" if target_ext == "jpeg" else self.target_format.upper()
        clamped_quality = max(1, min(100, int(self.quality)))

        for path in self.paths:
            img_to_save = None
            try:
                base, _ = os.path.splitext(path)
                new_path = f"{base}.{target_ext}"
                self.signals.progress_update.emit(f"Converting: {os.path.basename(path)}")

                exif_dict = {}
                if self.transfer_exif:
                    _, exif_dict = extract_image_metadata(path)

                with Image.open(path) as img:
                    img_to_save = _prepare_image_for_target(img, target_ext)

                save_kwargs = {}
                if target_ext in ("jpeg", "jpg", "webp"):
                    save_kwargs["quality"] = clamped_quality
                    if target_ext == "webp":
                        save_kwargs["method"] = 4

                if self.transfer_exif and exif_dict and target_ext != "webp":
                    if target_ext == "png":
                        meta = PngImagePlugin.PngInfo()
                        for k, v in exif_dict.items():
                            meta.add_text(str(k), str(v))
                        save_kwargs["pnginfo"] = meta
                    else:
                        try:
                            exif_obj = img_to_save.getexif()
                            if exif_obj is None:
                                exif_obj = Image.Exif()
                            for k, v in exif_dict.items():
                                tag_id = TAGS.get(k) or GPSTAGS.get(k)
                                if tag_id is not None:
                                    try:
                                        exif_obj[tag_id] = v
                                    except Exception:
                                        pass
                            exif_bytes = exif_obj.tobytes()
                            if exif_bytes:
                                save_kwargs["exif"] = exif_bytes
                        except Exception:
                            pass

                try:
                    encoded_bytes = _encode_image_bytes(img_to_save, save_format, save_kwargs)
                except Exception:
                    fallback_kwargs = {"quality": clamped_quality} if target_ext in ("jpeg", "jpg", "webp") else {}
                    encoded_bytes = _encode_image_bytes(img_to_save, save_format, fallback_kwargs)

                out_dir = os.path.dirname(os.path.abspath(new_path))
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                with open(new_path, "wb") as out_f:
                    out_f.write(encoded_bytes)

                if self.remove_original and os.path.exists(path) and os.path.abspath(path) != os.path.abspath(new_path):
                    os.remove(path)

                success_count += 1
            except Exception as e:
                failed_count += 1
                print(f"Failed to convert {path}: {e}")
            finally:
                if img_to_save is not None:
                    try:
                        img_to_save.close()
                    except Exception:
                        pass

        qual_str = f" (Quality: {self.quality})" if self.target_format != "PNG" else ""
        msg = f"Successfully converted {success_count} images to {self.target_format}{qual_str}."
        if failed_count > 0:
            msg += f" ({failed_count} failed)."
        self.signals.finished.emit(success_count, failed_count, msg)


class ComfyMetadataScannerSignals(QObject):
    """Lightweight worker signals for scanning PNG files for embedded ComfyUI workflows."""
    comfy_batch_ready = Signal(int, list)  # scan_id, list of file_paths with ComfyUI metadata


class ComfyMetadataScannerTask(QRunnable):
    def __init__(self, items, scan_id, signals, active_scan_getter=None):
        super().__init__()
        self.items = list(items)
        self.scan_id = scan_id
        self.signals = signals
        self.active_scan_getter = active_scan_getter

    def _is_cancelled(self):
        return self.active_scan_getter is not None and self.active_scan_getter() != self.scan_id

    def run(self):
        batch = []
        for item in self.items:
            if self._is_cancelled():
                return

            file_path = item["file_path"]
            if file_path.lower().endswith(".png"):
                if _png_has_comfy_metadata(file_path):
                    batch.append(file_path)

            if len(batch) >= 50:
                self.signals.comfy_batch_ready.emit(self.scan_id, batch)
                batch = []

        if batch and not self._is_cancelled():
            self.signals.comfy_batch_ready.emit(self.scan_id, batch)


class CacheCleanupTask(QRunnable):
    """Background task to enforce SQLite thumbnail cache limits and purge legacy loose files."""

    def __init__(self, cache_dir):
        super().__init__()
        self.cache_dir = cache_dir

    def run(self):
        try:
            settings = load_settings()
            limit_str = settings.get("disk_cache_limit_mb", "1 GB")
            encrypt_enabled = bool(settings.get("encrypt_thumbnail_cache", True))

            if not os.path.exists(self.cache_dir):
                return

            db_cache = ThumbnailSQLiteCache.get_shared(
                self.cache_dir,
                encrypt_enabled=encrypt_enabled,
            )
            db_cache.purge_loose_jpeg_files()

            if limit_str == "Disabled":
                db_cache.clear_all()
                print("Disk cache disabled: Cleared SQLite thumbnail cache.")
                return

            if not limit_str or "Unlimited" in limit_str:
                db_cache.flush()
                return

            limit_bytes = 1024 * 1024 * 1024  # Default 1 GB
            parts = limit_str.strip().split()
            if len(parts) >= 2:
                val = float(parts[0])
                unit = parts[1].upper()
                if "GB" in unit:
                    limit_bytes = int(val * 1024 * 1024 * 1024)
                elif "MB" in unit:
                    limit_bytes = int(val * 1024 * 1024)

            db_cache.enforce_limit_bytes(limit_bytes)

            db_cache.flush()
            db_cache.enforce_limit_bytes(limit_bytes)
            total_size = db_cache.total_size_bytes()
            if total_size <= limit_bytes:
                print(
                    f"CacheCleanupTask (disk cache size):{total_size / (1024 * 1024):.1f} mb / {limit_bytes / (1024 * 1024):.1f} mb "
                )
                return

        except Exception as e:
            print(f"Cache cleanup error: {e}")
