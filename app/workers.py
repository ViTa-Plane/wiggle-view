# version 0.4.3

import os
import hashlib
import tempfile
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

from PySide6.QtCore import Qt, QSize, QRunnable, Signal, QObject, QBuffer, QIODevice
from PySide6.QtGui import QColor, QPixmap, QPainter, QImageReader
from .encryption import encrypt_data
from .helpers import extract_image_metadata

class FolderScannerSignals(QObject):
    batch_scanned = Signal(int, list)
    scan_complete = Signal(int, int)


class FolderScannerTask(QRunnable):
    def __init__(self, folder_path, scan_id, signals):
        super().__init__()
        self.folder_path = folder_path
        self.scan_id = scan_id
        self.signals = signals

    def run(self):
        valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
        thumb_cache_dir = os.path.join(tempfile.gettempdir(), "pyqt_slate_thumbnail_cache")

        try:
            if not os.path.isdir(self.folder_path):
                return

            batch = []
            total_count = 0

            for f in os.listdir(self.folder_path):
                if f.lower().endswith(valid_extensions):
                    file_path = os.path.join(self.folder_path, f)
                    display_name = (f[:16] + '..') if len(f) > 18 else f

                    try:
                        file_size = os.path.getsize(file_path)
                        file_mtime = os.path.getmtime(file_path)
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

                    # ComfyUI check skipped here for maximum initial scan speed!
                    batch.append({
                        "file_path": file_path,
                        "display_name": display_name,
                        "thumb_path": precomputed_thumb_path,
                        "size": file_size,
                        "mtime": file_mtime,
                        "filename": f.lower(),
                        "has_comfy": False  # Evaluated lazily in background
                    })
                    total_count += 1

                    if len(batch) >= 50:
                        self.signals.batch_scanned.emit(self.scan_id, batch)
                        batch = []

            if batch:
                self.signals.batch_scanned.emit(self.scan_id, batch)

            self.signals.scan_complete.emit(self.scan_id, total_count)
        except Exception:
            pass


class ThumbnailSignalEmitter(QObject):
    """ Pass the file_path, thumb_path, and the freshly generated QPixmap object directly!"""
    thumbnail_ready = Signal(str, str, object)


class ThumbnailLoaderTask(QRunnable):
    def __init__(self, file_path, cache_dir, target_size, signals):
        super().__init__()
        self.file_path = file_path
        self.cache_dir = cache_dir
        self.target_size = target_size
        self.signals = signals

    def run(self):
        try:
            try:
                mtime = os.path.getmtime(self.file_path)
            except Exception:
                mtime = 0

            hash_input = f"{self.file_path}_{mtime}".encode('utf-8')
            path_hash = hashlib.md5(hash_input).hexdigest()

            thumb_filename = f"thumb_{path_hash}_{self.target_size}.jpg"
            thumb_path = os.path.join(self.cache_dir, thumb_filename)

            px = QPixmap()

            if os.path.exists(thumb_path):
                # If it already exists on disk, we don't need to write anything.
                # We can just let the paint event load it or pre-load it.
                pass
            else:
                reader = QImageReader(self.file_path)
                reader.setAutoTransform(True)

                orig_size = reader.size()
                if orig_size.isValid():
                    reader.setScaledSize(
                        orig_size.scaled(self.target_size, self.target_size, Qt.AspectRatioMode.KeepAspectRatio))

                image = reader.read()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image)

                    canvas = QPixmap(self.target_size, self.target_size)
                    canvas.fill(Qt.GlobalColor.transparent)

                    painter = QPainter(canvas)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

                    x = (self.target_size - pixmap.width()) // 2
                    y = (self.target_size - pixmap.height()) // 2
                    painter.drawPixmap(x, y, pixmap)
                    painter.end()

                    px = canvas  # Keep the QPixmap reference for RAM injection

                    # Save to disk cache once
                    buffer = QBuffer()
                    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                    canvas.save(buffer, "JPG", 88)
                    raw_bytes = buffer.data().data()

                    encrypted_bytes = encrypt_data(raw_bytes)
                    with open(thumb_path, "wb") as f_out:
                        f_out.write(encrypted_bytes)

            if os.path.exists(thumb_path):
                # Send thumb_path and the QPixmap object directly to the main thread
                self.signals.thumbnail_ready.emit(self.file_path, thumb_path, px)
        except Exception:
            pass

             
class BatchConvertWorkerSignals(QObject):
    progress_update = Signal(str)
    finished = Signal(int, int, str) # success_count, failed_count, message


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

                exif_data_bytes = None
                exif_dict = {}
                if self.transfer_exif:
                    exif_data_bytes, exif_dict = extract_image_metadata(path)

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
    comfy_status_ready = Signal(str, bool) # file_path, has_comfy

class ComfyMetadataScannerTask(QRunnable):
    def __init__(self, items, scan_id, signals):
        super().__init__()
        self.items = items
        self.scan_id = scan_id
        self.signals = signals

    def run(self):
        for item in self.items:
            file_path = item["file_path"]
            if file_path.lower().endswith(".png"):
                try:
                    with Image.open(file_path) as img:
                        if hasattr(img, "text") and img.text:
                            if any(k in img.text for k in ["workflow", "prompt"]):
                                self.signals.comfy_status_ready.emit(file_path, True)
                except Exception:
                    pass
