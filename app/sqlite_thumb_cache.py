import hashlib
import os
import sqlite3
import threading
import time
from io import BytesIO
from PIL import Image, ImageOps, ImageFile
from PySide6.QtGui import QImage


ImageFile.LOAD_TRUNCATED_IMAGES = True

class _ThreadLocalConnState:
    """Tracks per-thread SQLite connection and generation counter for safe connection lifecycle."""

    def __init__(self):
        self.conn = None
        self.generation = -1

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.generation = -1


class ThumbnailSQLiteCache:
    """SSD-friendly SQLite BLOB thumbnail cache minimizing disk writes via batching & deferred access updates.

    Uses per-thread SQLite connections (with WAL mode and busy timeout) rather than sharing a single
    sqlite3.Connection across multiple threads, avoiding concurrent cursor/transaction/close races.
    """

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get_shared(cls, cache_dir, db_filename="thumbnails_cache.db", flush_threshold=32, flush_interval_sec=3.0):
        norm_dir = os.path.abspath(cache_dir)
        key = (norm_dir, db_filename)
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(norm_dir, db_filename=db_filename, flush_threshold=flush_threshold, flush_interval_sec=flush_interval_sec)
                cls._instances[key] = inst
            return inst

    @classmethod
    def close_all_shared(cls):
        with cls._instances_lock:
            instances = list(cls._instances.values())
            cls._instances.clear()
        for inst in instances:
            try:
                inst.close()
            except Exception:
                pass

    def __init__(self, cache_dir, db_filename="thumbnails_cache.db", flush_threshold=32, flush_interval_sec=3.0):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.db_path = os.path.join(self.cache_dir, db_filename)
        self.flush_threshold = max(1, int(flush_threshold))
        self.flush_interval_sec = max(0.2, float(flush_interval_sec))

        self._lock = threading.RLock()
        self._conn_generation = 0
        self._closed = False
        self._tls = threading.local()
        self._all_states = []       # weak/tracked list of _ThreadLocalConnState for cleanup
        self._pending_puts = {}       # cache_key -> (source_path, size_bytes, now, blob)
        self._pending_access = {}     # cache_key -> last_accessed_float
        self._last_flush_time = time.monotonic()

        self._init_db()

    def _get_thread_state(self):
        state = getattr(self._tls, "state", None)
        if state is None:
            state = _ThreadLocalConnState()
            self._tls.state = state
            with self._lock:
                self._all_states.append(state)
        return state

    def _create_connection(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=True)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    def _get_conn(self):
        """Returns a SQLite connection owned exclusively by the calling thread."""
        with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError("ThumbnailSQLiteCache is closed.")
            current_gen = self._conn_generation

        state = self._get_thread_state()
        if state.conn is not None and state.generation != current_gen:
            state.close()

        if state.conn is None:
            state.conn = self._create_connection()
            state.generation = current_gen

        return state.conn

    def _close_all_thread_connections_locked(self):
        """Closes all tracked per-thread connections and bumps generation so other threads re-open cleanly."""
        self._conn_generation += 1
        for state in list(self._all_states):
            state.close()
        self._all_states.clear()

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS thumbnails (
                    cache_key TEXT PRIMARY KEY,
                    source_path TEXT,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    last_accessed REAL NOT NULL DEFAULT 0,
                    jpeg_blob BLOB NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_thumbnails_last_accessed
                ON thumbnails (last_accessed)
            """)
            conn.commit()

    @staticmethod
    def make_cache_key(source_path, target_size=170, jpeg_quality=90):
        abs_p = os.path.abspath(source_path)
        try:
            st = os.stat(abs_p)
            sig = f"{abs_p}|{st.st_size}|{st.st_mtime_ns}|{int(target_size)}|{int(jpeg_quality)}"
        except OSError:
            sig = f"{abs_p}|{int(target_size)}|{int(jpeg_quality)}"
        return hashlib.sha256(sig.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _decode_blob_to_qimage(blob):
        if not blob:
            return QImage()
        img = QImage()
        if img.loadFromData(blob, "JPG"):
            return img
        if img.loadFromData(blob):
            return img
        return QImage()

    def get_qimage(self, cache_key):
        """Read-only fast lookup: avoids per-read disk writes by recording access time in memory."""
        if not cache_key:
            return None
        blob = None
        now = time.time()
        with self._lock:
            if self._closed:
                return None
            pending = self._pending_puts.get(cache_key)
            if pending is not None:
                blob = pending[3]
                self._pending_access[cache_key] = now

        if blob is None:
            try:
                conn = self._get_conn()
                row = conn.execute(
                    "SELECT jpeg_blob FROM thumbnails WHERE cache_key = ?",
                    (cache_key,)
                ).fetchone()
                if row and row[0]:
                    blob = row[0]
                    with self._lock:
                        if not self._closed and cache_key not in self._pending_puts:
                            self._pending_access[cache_key] = now
            except Exception as e:
                print(f"SQLite cache read error: {e}")
                return None

        if not blob:
            return None
        img = self._decode_blob_to_qimage(blob)
        return img if not img.isNull() else None

    def put_pil_image(self, cache_key, source_path, pil_img, jpeg_quality=90):
        """Queues a compressed JPEG BLOB in memory and flushes in batches to reduce SSD writes."""
        if not cache_key or pil_img is None:
            return 0
        buf = BytesIO()
        rgb_img = pil_img.convert("RGB") if pil_img.mode not in ("RGB", "L") else pil_img
        rgb_img.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=True)
        blob = buf.getvalue()
        size_bytes = len(blob)
        now = time.time()

        with self._lock:
            if self._closed:
                return 0
            self._pending_puts[cache_key] = (source_path, size_bytes, now, blob)
            self._pending_access.pop(cache_key, None)
            if self._should_flush_locked():
                self._flush_locked()

        return size_bytes

    def _should_flush_locked(self):
        total_queued = len(self._pending_puts) + len(self._pending_access)
        if total_queued >= self.flush_threshold:
            return True
        if total_queued > 0 and (time.monotonic() - self._last_flush_time) >= self.flush_interval_sec:
            return True
        return False

    def flush(self):
        """Writes queued thumbnail inserts and deferred last_accessed updates in a single transaction."""
        with self._lock:
            if self._closed:
                return
            self._flush_locked()

    def _flush_locked(self):
        if not self._pending_puts and not self._pending_access:
            self._last_flush_time = time.monotonic()
            return

        puts = self._pending_puts
        access_updates = self._pending_access
        self._pending_puts = {}
        self._pending_access = {}
        self._last_flush_time = time.monotonic()

        conn = None
        try:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE;")
            if puts:
                rows = [
                    (key, src, sz, ts, sqlite3.Binary(blob))
                    for key, (src, sz, ts, blob) in puts.items()
                ]
                conn.executemany("""
                    INSERT INTO thumbnails (cache_key, source_path, size_bytes, last_accessed, jpeg_blob)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source_path = excluded.source_path,
                        size_bytes = excluded.size_bytes,
                        last_accessed = excluded.last_accessed,
                        jpeg_blob = excluded.jpeg_blob
                """, rows)

            if access_updates:
                acc_rows = [(ts, key) for key, ts in access_updates.items() if key not in puts]
                if acc_rows:
                    conn.executemany(
                        "UPDATE thumbnails SET last_accessed = ? WHERE cache_key = ?",
                        acc_rows
                    )
            conn.commit()
        except Exception as e:
            print(f"SQLite cache flush error: {e}")
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            for k, v in puts.items():
                self._pending_puts.setdefault(k, v)
            for k, v in access_updates.items():
                self._pending_access.setdefault(k, v)

    def generate_and_cache_qimage(self, source_path, target_size=170, jpeg_quality=90, persist_to_db=True):
        """Loads/caches thumbnail solely in SQLite BLOB without creating loose JPEG files on disk."""
        cache_key = self.make_cache_key(source_path, target_size=target_size, jpeg_quality=jpeg_quality)
        cached = self.get_qimage(cache_key)
        if cached is not None and not cached.isNull():
            return cached, f"sqlite://{cache_key}"
        try:
            with Image.open(source_path) as raw_img:
                raw_img.load()
                img = ImageOps.exif_transpose(raw_img)
                thumb = img.copy()
                thumb.thumbnail((int(target_size), int(target_size)), Image.Resampling.LANCZOS)
                buf = BytesIO()
                rgb_img = thumb.convert("RGB") if thumb.mode not in ("RGB", "L") else thumb
                rgb_img.save(buf, format="JPEG", quality=int(jpeg_quality), optimize=False)
                blob = buf.getvalue()
                size_bytes = len(blob)
                now = time.time()
                if persist_to_db:
                    with self._lock:
                        if not self._closed:
                            self._pending_puts[cache_key] = (source_path, size_bytes, now, blob)
                            self._pending_access.pop(cache_key, None)
                            if self._should_flush_locked():
                                self._flush_locked()
                qimg = self._decode_blob_to_qimage(blob)
                return (qimg if qimg is not None else QImage()), f"sqlite://{cache_key}"
        except Exception as e:
            print(f"Thumbnail generation failed for {source_path}: {e}")
            return QImage(), None

    def purge_loose_jpeg_files(self):
        """Removes legacy individual .jpg/.jpeg thumbnail files in cache_dir while keeping the DB."""
        removed = 0
        if not self.cache_dir or not os.path.isdir(self.cache_dir):
            return removed
        try:
            for root, _dirs, files in os.walk(self.cache_dir, followlinks=False, onerror=lambda e: None):
                for fname in files:
                    low = fname.lower()
                    if low.endswith((".jpg", ".jpeg")):
                        full_path = os.path.join(root, fname)
                        try:
                            os.remove(full_path)
                            removed += 1
                        except OSError:
                            pass
        except OSError:
            pass
        return removed

    def total_size_bytes(self):
        with self._lock:
            if self._closed:
                return 0
            pending_bytes = sum(item[1] for item in self._pending_puts.values())
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM thumbnails"
            ).fetchone()
            db_bytes = int(row[0] or 0)
        except Exception:
            db_bytes = 0
        return db_bytes + pending_bytes

    def enforce_limit_bytes(self, max_bytes):
        if max_bytes is None or max_bytes < 0:
            return
        with self._lock:
            if self._closed:
                return
            self._flush_locked()
        try:
            conn = self._get_conn()
            cur_total = conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM thumbnails"
            ).fetchone()[0] or 0
            if cur_total <= max_bytes:
                return
            rows = conn.execute(
                "SELECT cache_key, size_bytes FROM thumbnails ORDER BY last_accessed ASC"
            ).fetchall()
            deleted_any = False
            for key, sz in rows:
                if cur_total <= max_bytes:
                    break
                conn.execute("DELETE FROM thumbnails WHERE cache_key = ?", (key,))
                cur_total -= int(sz or 0)
                deleted_any = True
            if deleted_any:
                conn.commit()
        except Exception as e:
            print(f"SQLite cache eviction error: {e}")

    def clear_all(self):
        with self._lock:
            self._pending_puts.clear()
            self._pending_access.clear()
            if self._closed:
                return
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM thumbnails")
            conn.commit()
        except Exception as e:
            print(f"SQLite cache clear error: {e}")

    def reopen(self):
        """Re-opens the cache for continued use after a clear/close cycle if needed."""
        with self._lock:
            self._close_all_thread_connections_locked()
            self._closed = False
            self._init_db()

    def close(self):
        with self._lock:
            if not self._closed:
                self._flush_locked()
                self._closed = True
            self._close_all_thread_connections_locked()
