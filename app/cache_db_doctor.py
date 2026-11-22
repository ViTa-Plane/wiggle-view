#!/usr/bin/env python3
"""
Standalone CLI & reusable diagnostic/repair utility for thumbnails_cache.db.

Checks SQLite database integrity (PRAGMA integrity_check / quick_check),
schema health, and blob encoding/format (WVENC1 encrypted vs. JPEG vs. invalid/corrupt),
and supports safe backup + quarantine/repair of corrupt or mixed-state rows.

Designed so its core functions (`audit_database`, `repair_database`, `classify_blob`)
can be imported directly into a main application later.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


DEFAULT_MAGIC_PREFIX: bytes = b"WVENC1"
JPEG_SOI: bytes = b"\xff\xd8"
JPEG_EOI: bytes = b"\xff\xd9"

ProgressCallback = Optional[Callable[[int, int, str], None]]


@dataclass
class AuditReport:
    db_path: str
    exists: bool = False
    file_size_bytes: int = 0
    header_valid: bool = False
    sqlite_integrity_ok: bool = False
    sqlite_integrity_messages: List[str] = field(default_factory=list)
    table_exists: bool = False
    columns: List[str] = field(default_factory=list)
    total_rows: int = 0
    encrypted_count: int = 0
    unencrypted_jpeg_count: int = 0
    empty_blob_count: int = 0
    corrupt_or_unknown_count: int = 0
    truncated_jpeg_count: int = 0
    corrupt_keys: List[str] = field(default_factory=list)
    truncated_keys: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    @property
    def is_healthy(self) -> bool:
        return (
            self.exists
            and self.header_valid
            and self.sqlite_integrity_ok
            and self.table_exists
            and self.corrupt_or_unknown_count == 0
            and len(self.errors) == 0
        )

    @property
    def encoding_state(self) -> str:
        if not self.exists or not self.table_exists:
            return "MISSING_OR_NO_TABLE"
        active = self.encrypted_count + self.unencrypted_jpeg_count
        if active == 0 and self.total_rows == 0:
            return "EMPTY"
        if self.encrypted_count > 0 and self.unencrypted_jpeg_count == 0:
            return "ENCRYPTED"
        if self.unencrypted_jpeg_count > 0 and self.encrypted_count == 0:
            return "UNENCRYPTED_JPEG"
        if self.encrypted_count > 0 and self.unencrypted_jpeg_count > 0:
            return "MIXED"
        return "UNKNOWN"


@dataclass
class RepairReport:
    db_path: str
    backup_path: Optional[str] = None
    dry_run: bool = False
    deleted_corrupt_rows: int = 0
    deleted_empty_rows: int = 0
    deleted_truncated_rows: int = 0
    vacuumed: bool = False
    post_audit: Optional[AuditReport] = None
    errors: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def classify_blob(
    blob: Optional[bytes | bytearray | memoryview],
    magic_prefix: bytes = DEFAULT_MAGIC_PREFIX,
    check_jpeg_eoi: bool = True,
) -> str:
    """
    Classifies a raw blob from `jpeg_blob`.

    Returns one of:
      - "EMPTY": None or 0-length bytes
      - "ENCRYPTED": Starts with `magic_prefix` (e.g., b"WVENC1")
      - "JPEG": Starts with JPEG SOI (b"\xff\xd8") and (if checked) ends with EOI (b"\xff\xd9")
      - "JPEG_TRUNCATED": Starts with b"\xff\xd8" but does not end with b"\xff\xd9"
      - "CORRUPT_OR_UNKNOWN": Non-empty payload not matching expected header
    """
    if blob is None:
        return "EMPTY"

    if isinstance(blob, memoryview):
        raw = blob.tobytes()
    elif isinstance(blob, bytearray):
        raw = bytes(blob)
    elif isinstance(blob, bytes):
        raw = blob
    else:
        return "CORRUPT_OR_UNKNOWN"

    if len(raw) == 0:
        return "EMPTY"

    if magic_prefix and raw.startswith(magic_prefix):
        return "ENCRYPTED"

    if raw.startswith(JPEG_SOI):
        if check_jpeg_eoi:
            # Trim trailing null padding if present before checking EOI
            trimmed = raw.rstrip(b"\x00")
            if len(trimmed) >= 4 and trimmed.endswith(JPEG_EOI):
                return "JPEG"
            return "JPEG_TRUNCATED"
        return "JPEG"

    return "CORRUPT_OR_UNKNOWN"


def _has_valid_sqlite_header(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 100:
            return False
        with path.open("rb") as fh:
            header = fh.read(16)
        return header == b"SQLite format 3\x00"
    except OSError:
        return False


def get_default_cache_db_path(filename: str = "thumbnails_cache.db") -> Path:
    """
    Returns the default path in the system temporary folder:
      <TEMP>/pyqt_slate_thumbnail_cache/thumbnails_cache.db
    """
    temp_root = Path(tempfile.gettempdir())
    return temp_root / "pyqt_slate_thumbnail_cache" / filename


def resolve_db_path(db_path: Optional[str | os.PathLike] = None) -> Path:
    """Resolves an explicit path or falls back to the default temp cache DB path."""
    if db_path is None or str(db_path).strip() == "":
        return get_default_cache_db_path()
    return Path(db_path)


def _open_readonly_or_standard(db_path: Path) -> sqlite3.Connection:
    # URI mode=ro avoids creating -wal/-shm/journal files on read-only media when possible,
    # falling back to standard connect if needed.
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.execute("SELECT 1").fetchone()
        return conn
    except sqlite3.Error:
        return sqlite3.connect(str(db_path), timeout=30.0)


def audit_database(
    db_path: Optional[str | os.PathLike] = None,
    magic_prefix: bytes = DEFAULT_MAGIC_PREFIX,
    quick_integrity: bool = False,
    check_jpeg_eoi: bool = True,
    sample_key_limit: int = 50,
    progress_cb: ProgressCallback = None,
) -> AuditReport:
    """
    Audits a SQLite cache database for file/SQLite integrity and blob encoding state.
    Safe to call from CLI or from a main GUI/application thread.
    """
    start_time = time.perf_counter()
    path = resolve_db_path(db_path)
    report = AuditReport(db_path=str(path))

    if not path.exists() or not path.is_file():
        report.errors.append(f"Database file does not exist: {path}")
        report.elapsed_seconds = time.perf_counter() - start_time
        return report

    report.exists = True
    try:
        report.file_size_bytes = path.stat().st_size
    except OSError as exc:
        report.errors.append(f"Failed to stat file: {exc}")

    report.header_valid = _has_valid_sqlite_header(path)
    if not report.header_valid:
        report.errors.append("Invalid or truncated SQLite 3 file header.")
        report.elapsed_seconds = time.perf_counter() - start_time
        return report

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _open_readonly_or_standard(path)
        pragma_name = "PRAGMA quick_check" if quick_integrity else "PRAGMA integrity_check"
        pragma_rows = [r[0] for r in conn.execute(pragma_name).fetchall()]
        report.sqlite_integrity_messages = pragma_rows
        report.sqlite_integrity_ok = len(pragma_rows) == 1 and str(pragma_rows[0]).lower() == "ok"
        if not report.sqlite_integrity_ok:
            report.errors.append(f"{pragma_name} reported issues: {pragma_rows}")

        table_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thumbnails'"
        ).fetchone()
        if not table_row:
            report.table_exists = False
            report.errors.append("Table 'thumbnails' not found in database.")
            report.elapsed_seconds = time.perf_counter() - start_time
            return report

        report.table_exists = True
        col_info = conn.execute("PRAGMA table_info(thumbnails)").fetchall()
        report.columns = [c[1] for c in col_info]

        count_row = conn.execute("SELECT COUNT(*) FROM thumbnails").fetchone()
        report.total_rows = int(count_row[0]) if count_row else 0

        key_expr = "cache_key" if "cache_key" in report.columns else "CAST(rowid AS TEXT)"
        has_blob = "jpeg_blob" in report.columns
        if not has_blob:
            report.errors.append("Column 'jpeg_blob' missing from 'thumbnails' table.")
            report.elapsed_seconds = time.perf_counter() - start_time
            return report

        cursor = conn.execute(f"SELECT {key_expr}, jpeg_blob FROM thumbnails")
        processed = 0
        for key, blob in cursor:
            processed += 1
            kind = classify_blob(blob, magic_prefix=magic_prefix, check_jpeg_eoi=check_jpeg_eoi)
            key_str = str(key)

            if kind == "ENCRYPTED":
                report.encrypted_count += 1
            elif kind == "JPEG":
                report.unencrypted_jpeg_count += 1
            elif kind == "EMPTY":
                report.empty_blob_count += 1
            elif kind == "JPEG_TRUNCATED":
                report.truncated_jpeg_count += 1
                report.corrupt_or_unknown_count += 1
                if len(report.truncated_keys) < sample_key_limit:
                    report.truncated_keys.append(key_str)
                if len(report.corrupt_keys) < sample_key_limit:
                    report.corrupt_keys.append(key_str)
            else:
                report.corrupt_or_unknown_count += 1
                if len(report.corrupt_keys) < sample_key_limit:
                    report.corrupt_keys.append(key_str)

            if progress_cb and (processed % 250 == 0 or processed == report.total_rows):
                progress_cb(processed, report.total_rows, f"Auditing rows ({processed}/{report.total_rows})")

    except sqlite3.Error as exc:
        report.errors.append(f"SQLite error during audit: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    report.elapsed_seconds = time.perf_counter() - start_time
    return report


def create_backup(db_path: str | os.PathLike, backup_dir: Optional[str | os.PathLike] = None) -> Path:
    """Creates a timestamped backup copy of the database file before repair."""
    src = Path(db_path)
    target_dir = Path(backup_dir) if backup_dir else src.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = target_dir / f"{src.stem}.backup_{stamp}{src.suffix}"
    shutil.copy2(src, dst)
    return dst


def repair_database(
    db_path: Optional[str | os.PathLike] = None,
    magic_prefix: bytes = DEFAULT_MAGIC_PREFIX,
    remove_empty: bool = True,
    remove_truncated_jpeg: bool = True,
    remove_corrupt_blobs: bool = True,
    make_backup: bool = True,
    backup_dir: Optional[str | os.PathLike] = None,
    run_vacuum: bool = True,
    dry_run: bool = False,
    check_jpeg_eoi: bool = True,
    progress_cb: ProgressCallback = None,
) -> RepairReport:
    """
    Repairs a SQLite thumbnail cache by backing up first (optional), removing
    corrupt/empty/truncated rows according to flags, optionally VACUUMing,
    and returning a post-repair AuditReport.

    Note: If the SQLite file structure itself is severely corrupted at the B-tree/page
    level, row deletion alone may fail; in that case, recreate or restore from backup.
    """
    start_time = time.perf_counter()
    path = resolve_db_path(db_path)
    rep = RepairReport(db_path=str(path), dry_run=dry_run)

    if not path.exists() or not path.is_file():
        rep.errors.append(f"Database file does not exist: {path}")
        rep.elapsed_seconds = time.perf_counter() - start_time
        return rep

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(path), timeout=30.0)
        table_row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thumbnails'"
        ).fetchone()
        if not table_row:
            rep.errors.append("Table 'thumbnails' does not exist; nothing to repair.")
            rep.elapsed_seconds = time.perf_counter() - start_time
            return rep

        col_info = conn.execute("PRAGMA table_info(thumbnails)").fetchall()
        cols = [c[1] for c in col_info]
        if "jpeg_blob" not in cols:
            rep.errors.append("Column 'jpeg_blob' not found in 'thumbnails'.")
            rep.elapsed_seconds = time.perf_counter() - start_time
            return rep

        rows = conn.execute("SELECT rowid, jpeg_blob FROM thumbnails").fetchall()
        total = len(rows)
        rowids_to_delete: List[int] = []

        for idx, (rowid, blob) in enumerate(rows, start=1):
            kind = classify_blob(blob, magic_prefix=magic_prefix, check_jpeg_eoi=check_jpeg_eoi)
            should_delete = False

            if kind == "EMPTY" and remove_empty:
                rep.deleted_empty_rows += 1
                should_delete = True
            elif kind == "JPEG_TRUNCATED" and remove_truncated_jpeg:
                rep.deleted_truncated_rows += 1
                rep.deleted_corrupt_rows += 1
                should_delete = True
            elif kind == "CORRUPT_OR_UNKNOWN" and remove_corrupt_blobs:
                rep.deleted_corrupt_rows += 1
                should_delete = True

            if should_delete:
                rowids_to_delete.append(int(rowid))

            if progress_cb and (idx % 250 == 0 or idx == total):
                progress_cb(idx, total, f"Scanning rows for repair ({idx}/{total})")

        if dry_run:
            conn.close()
            conn = None
            rep.elapsed_seconds = time.perf_counter() - start_time
            return rep

        if rowids_to_delete and make_backup:
            conn.close()
            conn = None
            backup_file = create_backup(path, backup_dir=backup_dir)
            rep.backup_path = str(backup_file)
            conn = sqlite3.connect(str(path), timeout=30.0)

        if rowids_to_delete:
            with conn:
                chunk_size = 500
                for i in range(0, len(rowids_to_delete), chunk_size):
                    chunk = rowids_to_delete[i : i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM thumbnails WHERE rowid IN ({placeholders})", chunk)

        conn.close()
        conn = None

        if run_vacuum and (rowids_to_delete or make_backup):
            vconn = sqlite3.connect(str(path), timeout=60.0)
            try:
                vconn.execute("VACUUM")
                rep.vacuumed = True
            finally:
                vconn.close()

        rep.post_audit = audit_database(
            path,
            magic_prefix=magic_prefix,
            check_jpeg_eoi=check_jpeg_eoi,
        )

    except sqlite3.Error as exc:
        rep.errors.append(f"SQLite error during repair: {exc}")
    except OSError as exc:
        rep.errors.append(f"File system error during repair: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    rep.elapsed_seconds = time.perf_counter() - start_time
    return rep


def _format_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    val = float(num_bytes)
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.2f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{num_bytes} B"


def print_audit_report(report: AuditReport) -> None:
    print(f"Database        : {report.db_path}")
    print(f"Exists          : {report.exists}")
    if not report.exists:
        for err in report.errors:
            print(f"  [ERROR] {err}")
        return

    print(f"File Size       : {_format_bytes(report.file_size_bytes)}")
    print(f"SQLite Header   : {'OK' if report.header_valid else 'INVALID'}")
    print(
        f"SQLite Integrity: {'OK' if report.sqlite_integrity_ok else 'FAILED'} "
        f"({', '.join(report.sqlite_integrity_messages) if report.sqlite_integrity_messages else 'n/a'})"
    )
    print(f"Table 'thumbnails': {'Present' if report.table_exists else 'Missing'}")
    print(f"Encoding State  : {report.encoding_state}")
    print(f"Total Rows      : {report.total_rows}")
    print(f"  Encrypted     : {report.encrypted_count}")
    print(f"  Unenc. JPEG   : {report.unencrypted_jpeg_count}")
    print(f"  Empty Blobs   : {report.empty_blob_count}")
    print(f"  Truncated JPEG: {report.truncated_jpeg_count}")
    print(f"  Corrupt/Other : {report.corrupt_or_unknown_count}")
    print(f"Overall Healthy : {'YES' if report.is_healthy else 'NO'}")
    print(f"Elapsed         : {report.elapsed_seconds:.3f}s")

    if report.corrupt_keys:
        print(f"Sample Bad Keys ({len(report.corrupt_keys)} shown): {', '.join(report.corrupt_keys[:10])}")
    for err in report.errors:
        print(f"  [ERROR] {err}")


def print_repair_report(rep: RepairReport) -> None:
    mode_label = "DRY RUN" if rep.dry_run else "APPLIED"
    print(f"\n--- Repair Summary ({mode_label}) ---")
    print(f"Backup Created    : {rep.backup_path or 'None'}")
    print(f"Empty Rows Marked : {rep.deleted_empty_rows}")
    print(f"Truncated Marked  : {rep.deleted_truncated_rows}")
    print(f"Corrupt Marked    : {rep.deleted_corrupt_rows}")
    print(f"VACUUM Executed   : {rep.vacuumed}")
    print(f"Repair Elapsed    : {rep.elapsed_seconds:.3f}s")
    for err in rep.errors:
        print(f"  [ERROR] {err}")
    if rep.post_audit is not None:
        print("\n--- Post-Repair Audit ---")
        print_audit_report(rep.post_audit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and repair SQLite thumbnail cache encoding and integrity."
    )
    default_db = str(get_default_cache_db_path())
    parser.add_argument(
        "db_path",
        nargs="?",
        default=default_db,
        help=f"Path to SQLite cache database (default: {default_db})",
    )
    parser.add_argument(
        "--magic",
        default=DEFAULT_MAGIC_PREFIX.decode("latin1"),
        help="Encrypted blob magic prefix (default: WVENC1)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use PRAGMA quick_check instead of full PRAGMA integrity_check",
    )
    parser.add_argument(
        "--no-eoi-check",
        action="store_true",
        help="Do not require JPEG EOI (FF D9) marker for unencrypted JPEG blobs",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Remove corrupt/empty/truncated rows (creates a backup unless --no-backup)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --repair, report what would be removed without modifying the database",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a backup file before repairing",
    )
    parser.add_argument(
        "--backup-dir",
        default=None,
        help="Optional directory for backup files (defaults to database directory)",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM after deleting rows",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Do not delete rows with empty/null blobs during repair",
    )
    parser.add_argument(
        "--keep-truncated",
        action="store_true",
        help="Do not delete JPEG blobs missing FF D9 EOI marker during repair",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress updates during large scans",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    magic_bytes = args.magic.encode("latin1")
    check_eoi = not args.no_eoi_check

    def _on_progress(current: int, total: int, message: str) -> None:
        if not args.quiet:
            print(f"\r{message}...", end="", flush=True)
            if current >= total:
                print()

    audit = audit_database(
        args.db_path,
        magic_prefix=magic_bytes,
        quick_integrity=args.quick,
        check_jpeg_eoi=check_eoi,
        progress_cb=_on_progress if not args.quiet else None,
    )
    print_audit_report(audit)

    exit_code = 0 if audit.is_healthy else 1

    if args.repair:
        rep = repair_database(
            args.db_path,
            magic_prefix=magic_bytes,
            remove_empty=not args.keep_empty,
            remove_truncated_jpeg=not args.keep_truncated,
            remove_corrupt_blobs=True,
            make_backup=not args.no_backup,
            backup_dir=args.backup_dir,
            run_vacuum=not args.no_vacuum,
            dry_run=args.dry_run,
            check_jpeg_eoi=check_eoi,
            progress_cb=_on_progress if not args.quiet else None,
        )
        print_repair_report(rep)
        if rep.errors or (rep.post_audit is not None and not rep.post_audit.is_healthy):
            exit_code = 2
        else:
            exit_code = 0

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
