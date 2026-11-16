# version 0.0.17

import os
import hashlib

from PySide6.QtCore import Qt, QSize, QRunnable, Signal, QObject, QRect, QPoint
from PySide6.QtGui import QColor, QPixmap, QPainter, QFont, QPen, QMouseEvent,QImageReader


class FolderScannerSignals(QObject):
    batch_scanned = Signal(int, list)  # scan_id, batch
    scan_complete = Signal(int, int)   # scan_id, total_count


class FolderScannerTask(QRunnable):
    def __init__(self, folder_path, scan_id, signals):
        super().__init__()
        self.folder_path = folder_path
        self.scan_id = scan_id
        self.signals = signals

    def run(self):
        valid_extensions = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.webp')
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

                    batch.append({
                        "file_path": file_path,
                        "display_name": display_name,
                        "thumb_path": None,
                        "size": file_size,
                        "mtime": file_mtime,
                        "filename": f.lower()
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
    thumbnail_ready = Signal(str, str)


class ThumbnailLoaderTask(QRunnable):
    def __init__(self, file_path, cache_dir, target_size, signals):
        super().__init__()
        self.file_path = file_path
        self.cache_dir = cache_dir
        self.target_size = target_size
        self.signals = signals

    def run(self):
        try:
            path_hash = hashlib.md5(self.file_path.encode('utf-8')).hexdigest()
            thumb_filename = f"thumb_{path_hash}_{self.target_size}.jpg"
            thumb_path = os.path.join(self.cache_dir, thumb_filename)

            if not os.path.exists(thumb_path):
                # Use QImageReader to load huge images safely without memory spikes
                reader = QImageReader(self.file_path)
                reader.setAutoTransform(True)

                orig_size = reader.size()
                if orig_size.isValid():
                    # Scale down to fit the target thumbnail size while keeping aspect ratio
                    reader.setScaledSize(
                        orig_size.scaled(self.target_size, self.target_size, Qt.AspectRatioMode.KeepAspectRatio))

                image = reader.read()
                if not image.isNull():
                    pixmap = QPixmap.fromImage(image)

                    canvas = QPixmap(self.target_size, self.target_size)
                    canvas.fill(Qt.GlobalColor.transparent)

                    painter = QPainter(canvas)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

                    # Center the scaled image inside the square thumbnail card
                    x = (self.target_size - pixmap.width()) // 2
                    y = (self.target_size - pixmap.height()) // 2
                    painter.drawPixmap(x, y, pixmap)
                    painter.end()

                    canvas.save(thumb_path, "JPG", 88)

            if os.path.exists(thumb_path):
                self.signals.thumbnail_ready.emit(self.file_path, thumb_path)
        except Exception:
            pass