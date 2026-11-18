import os
import hashlib
import tempfile
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

from PySide6.QtCore import Qt, QRunnable, Signal, QObject, QBuffer, QIODevice
from PySide6.QtGui import QImage, QPainter, QImageReader
from .encryption import encrypt_data, decrypt_data
from .helpers import extract_image_metadata


class FolderScannerSignals(QObject):
    batch_scanned = Signal(int, list)
    scan_complete = Signal(int, int)


class FolderScannerTask(QRunnable):
    def __init__(self, folder_path, scan_id, signals, active_scan_getter=None):
        super().__init__()
        self.folder_path = folder_path
        self.scan_id = scan_id
        self.signals = signals
        self.active_scan_getter = active_scan_getter

    def _is_cancelled(self):
        return self.active_scan_getter is not None and self.active_scan_getter() != self.scan_id

    def run(self):
        valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
        thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")

        try:
            if not os.path.isdir(self.folder_path) or self._is_cancelled():
                return

            batch = []
            total_count = 0

            with os.scandir(self.folder_path) as entries:
                for entry in entries:
                    if self._is_cancelled():
                        return
                    if not entry.is_file():
                        continue

                    f = entry.name
                    if not f.lower().endswith(valid_extensions):
                        continue

                    file_path = entry.path
                    display_name = (f[:16] + '..') if len(f) > 18 else f

                    try:
                        stat = entry.stat()
                        file_size = stat.st_size
                        file_mtime = stat.st_mtime
                    except Exception:
                        file_size = 0
                        file_mtime = 0

                    precomputed_thumb_path = None
                    try:
                        hash_input = f"{file_path}_{file_mtime}".encode('utf-8')
                        path_hash = hashlib.md5(hash_input).hexdigest()
                        candidate_thumb = os.path.join(thumb_cache_dir, f"thumb_{path_hash}_170.jpg")
                        if os.path.exists(candidate_thumb):
                            precomputed_thumb_path = candidate_thumb
                    except Exception:
                        pass

                    batch.append({
                        "file_path": file_path,
                        "display_name": display_name,
                        "thumb_path": precomputed_thumb_path,
                        "size": file_size,
                        "mtime": file_mtime,
                        "filename": f.lower(),
                        "has_comfy": False
                    })
                    total_count += 1

                    if len(batch) >= 100:
                        self.signals.batch_scanned.emit(self.scan_id, batch)
                        batch = []

            if batch and not self._is_cancelled():
                self.signals.batch_scanned.emit(self.scan_id, batch)

            if not self._is_cancelled():
                self.signals.scan_complete.emit(self.scan_id, total_count)
        except Exception:
            pass


class ThumbnailSignalEmitter(QObject):
    thumbnail_ready = Signal(int, str, str, QImage)  # scan_id, file_path, thumb_path, decoded_qimage


class ThumbnailLoaderTask(QRunnable):
    """Generates or decrypts cached thumbnails off the GUI thread using thread-safe QImage."""

    def __init__(self, file_path, cache_dir, target_size, signals, scan_id=0, active_scan_getter=None,
                 existing_thumb_path=None):
        super().__init__()
        self.file_path = file_path
        self.cache_dir = cache_dir
        self.target_size = target_size
        self.signals = signals
        self.scan_id = scan_id
        self.active_scan_getter = active_scan_getter
        self.existing_thumb_path = existing_thumb_path

    def _is_cancelled(self):
        return self.active_scan_getter is not None and self.active_scan_getter() != self.scan_id

    def run(self):
        if self._is_cancelled():
            return

        try:
            # Load dynamic settings for encryption and JPEG quality
            from .helpers import load_settings
            settings = load_settings()
            encrypt_enabled = settings.get("encrypt_thumbnail_cache", True)
            jpeg_quality = settings.get("thumbnail_jpeg_quality", 88)

            thumb_path = self.existing_thumb_path
            if not thumb_path:
                try:
                    mtime = os.path.getmtime(self.file_path)
                except Exception:
                    mtime = 0

                hash_input = f"{self.file_path}_{mtime}".encode('utf-8')
                path_hash = hashlib.md5(hash_input).hexdigest()
                thumb_filename = f"thumb_{path_hash}_{self.target_size}.jpg"
                thumb_path = os.path.join(self.cache_dir, thumb_filename)

            # Fast path: Decrypt and decode already-cached thumbnail off the GUI thread
            if os.path.exists(thumb_path):
                with open(thumb_path, "rb") as f_in:
                    file_bytes = f_in.read()

                data_bytes = decrypt_data(file_bytes) if encrypt_enabled else file_bytes
                img = QImage()
                if img.loadFromData(data_bytes, "JPG") and not self._is_cancelled():
                    self.signals.thumbnail_ready.emit(self.scan_id, self.file_path, thumb_path, img)
                    return

            if self._is_cancelled():
                return

            # Slow path: Read source image, scale via QImageReader, compose on QImage, encrypt & save
            reader = QImageReader(self.file_path)
            reader.setAutoTransform(True)

            orig_size = reader.size()
            if orig_size.isValid():
                reader.setScaledSize(
                    orig_size.scaled(self.target_size, self.target_size, Qt.AspectRatioMode.KeepAspectRatio)
                )

            image = reader.read()
            if not image.isNull() and not self._is_cancelled():
                canvas = QImage(self.target_size, self.target_size, QImage.Format.Format_ARGB32_Premultiplied)
                canvas.fill(Qt.GlobalColor.transparent)

                painter = QPainter(canvas)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                x = (self.target_size - image.width()) // 2
                y = (self.target_size - image.height()) // 2
                painter.drawImage(x, y, image)
                painter.end()

                buffer = QBuffer()
                buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                # Apply dynamic thumbnail JPEG quality from settings
                canvas.save(buffer, "JPG", jpeg_quality)
                raw_bytes = bytes(buffer.data())

                output_bytes = encrypt_data(raw_bytes) if encrypt_enabled else raw_bytes
                with open(thumb_path, "wb") as f_out:
                    f_out.write(output_bytes)

                if not self._is_cancelled():
                    self.signals.thumbnail_ready.emit(self.scan_id, self.file_path, thumb_path, canvas)
        except Exception:
            pass


class BatchConvertWorkerSignals(QObject):
    progress_update = Signal(str)
    finished = Signal(int, int, str)


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

        for path in self.paths:
            try:
                base, _ = os.path.splitext(path)
                new_path = f"{base}.{target_ext}"
                self.signals.progress_update.emit(f"Converting: {os.path.basename(path)}")

                exif_dict = {}
                if self.transfer_exif:
                    _, exif_dict = extract_image_metadata(path)

                with Image.open(path) as img:
                    if target_ext in ["jpg", "jpeg"] and (
                            img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                        img_to_save = background
                    else:
                        img_to_save = img.convert("RGB") if target_ext in ["jpeg", "jpg"] else img

                    save_format = "JPEG" if target_ext == "jpeg" else self.target_format.upper()

                    if target_ext in ["jpeg", "jpg", "webp"]:
                        img_to_save.save(new_path, save_format, quality=self.quality)
                    else:
                        img_to_save.save(new_path, save_format)

                if self.transfer_exif and exif_dict:
                    with Image.open(new_path) as target:
                        if target_ext in ["png"]:
                            from PIL import PngImagePlugin
                            meta = PngImagePlugin.PngInfo()
                            for k, v in exif_dict.items():
                                meta.add_text(str(k), str(v))
                            target.save(new_path, pnginfo=meta)
                        else:
                            try:
                                exif_obj = target.getexif()
                                if exif_obj is None:
                                    exif_obj = Image.Exif()
                                for k, v in exif_dict.items():
                                    tag_id = TAGS.get(k) or GPSTAGS.get(k)
                                    if tag_id is not None:
                                        try:
                                            exif_obj[tag_id] = v
                                        except Exception:
                                            pass
                                target.save(new_path, exif=exif_obj.tobytes(),
                                            quality=self.quality if target_ext in ["jpeg", "jpg", "webp"] else None)
                            except Exception:
                                target.save(new_path)

                if self.remove_original and os.path.exists(path) and os.path.abspath(path) != os.path.abspath(new_path):
                    os.remove(path)

                success_count += 1
            except Exception as e:
                failed_count += 1
                print(f"Failed to convert {path}: {e}")

        qual_str = f" (Quality: {self.quality})" if self.target_format != "PNG" else ""
        msg = f"Successfully converted {success_count} images to {self.target_format}{qual_str}."
        if failed_count > 0:
            msg += f" ({failed_count} failed)."
        self.signals.finished.emit(success_count, failed_count, msg)

'''Create a lightweight worker that checks PNG files for ComfyUI workflows 
in the background after the grid has already rendered:'''
class ComfyMetadataScannerSignals(QObject):
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
                try:
                    with Image.open(file_path) as img:
                        if hasattr(img, "text") and img.text:
                            if any(k in img.text for k in ["workflow", "prompt"]):
                                batch.append(file_path)
                except Exception:
                    pass

                # Batch updates in chunks of 50 to prevent signal storms on 3000+ image galleries
                if len(batch) >= 50:
                    self.signals.comfy_batch_ready.emit(self.scan_id, batch)
                    batch = []

        if batch and not self._is_cancelled():
            self.signals.comfy_batch_ready.emit(self.scan_id, batch)


class CacheCleanupTask(QRunnable):
    """Background task to prune thumbnail cache files when they exceed the disk limit."""

    def __init__(self, cache_dir):
        super().__init__()
        self.cache_dir = cache_dir

    def run(self):
        try:
            from .helpers import load_settings
            settings = load_settings()
            limit_str = settings.get("disk_cache_limit_mb", "1 GB")

            if not limit_str or "Unlimited" in limit_str:
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

            if not os.path.exists(self.cache_dir):
                return

            file_entries = []
            total_size = 0
            with os.scandir(self.cache_dir) as entries:
                for entry in entries:
                    if entry.is_file():
                        stat = entry.stat()
                        file_entries.append((entry.path, stat.st_size, stat.st_mtime))
                        total_size += stat.st_size

            if total_size <= limit_bytes:
                print(f"CacheCleanupTask (disk cache size):{limit_bytes/ (1024 * 1024):.2f} mb / {total_size / (1024 * 1024):.2f} mb")
                return

            # Sort by modification time ascending (oldest files first)
            file_entries.sort(key=lambda x: x[2])

            removed_count = 0
            for path, size, mtime in file_entries:
                if total_size <= limit_bytes:
                    break
                try:
                    os.remove(path)
                    total_size -= size
                    removed_count += 1
                except Exception:
                    pass

            # Log the cleanup results to the terminal console
            if removed_count > 0:
                print(f"Cache cleanup: Successfully removed {removed_count} old cached thumbnail file(s) to respect the disk limit.")
        except Exception as e:
            print(f"Cache cleanup error: {e}")