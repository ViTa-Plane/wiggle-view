"""Standalone crash reporter for WiggleView (PySide6 / Python).

Captures:
1. Fatal signals, segfaults / access violations, and native/C++ hard crashes via `faulthandler`
   (writing stack traces across all threads to a persistent log file).
2. Uncaught Python exceptions on the main thread via `sys.excepthook`.
3. Uncaught Python exceptions on worker threads via `threading.excepthook`.
4. Qt critical/fatal messages (and optional warnings) via `qInstallMessageHandler`.
"""

import atexit
import datetime
import faulthandler
import os
import platform
import sys
import tempfile
import threading
import traceback

try:
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
except ImportError:
    QtMsgType = None
    qInstallMessageHandler = None


class CrashReporter:
    """Manages crash log initialization and exception/message hooks."""

    def __init__(self, app_name="WiggleView", log_dir=None, log_filename="wiggleview_crash.log", capture_warnings=False):
        self.app_name = app_name
        self.log_dir = log_dir or os.path.join(tempfile.gettempdir(), "wiggleview_logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.abspath(os.path.join(self.log_dir, log_filename))
        self.capture_warnings = bool(capture_warnings)

        self._lock = threading.Lock()
        self._file = None
        self._installed = False
        self._prev_sys_excepthook = sys.excepthook
        self._prev_threading_excepthook = getattr(threading, "excepthook", None)
        self._prev_qt_handler = None

    def install(self):
        """Enable faulthandler and install Python + Qt crash/error hooks."""
        if self._installed:
            return self.log_path

        self._file = open(self.log_path, "a", encoding="utf-8", buffering=1)
        self._write_session_header()

        if not faulthandler.is_enabled():
            faulthandler.enable(file=self._file, all_threads=True)
        else:
            try:
                faulthandler.disable()
                faulthandler.enable(file=self._file, all_threads=True)
            except Exception:
                pass

        sys.excepthook = self._handle_sys_exception
        if hasattr(threading, "excepthook"):
            threading.excepthook = self._handle_threading_exception

        if qInstallMessageHandler is not None and QtMsgType is not None:
            self._prev_qt_handler = qInstallMessageHandler(self._handle_qt_message)

        atexit.register(self.close)
        self._installed = True
        return self.log_path

    def _write_session_header(self):
        now = datetime.datetime.now().isoformat(timespec="seconds")
        sep = "=" * 78
        header = (
            "\n" + sep + "\n"
            + f"[{now}] {self.app_name} Session Started (PID={os.getpid()})\n"
            + f"Python: {sys.version.replace(os.linesep, ' ')}\n"
            + f"Platform: {platform.platform()} ({platform.machine()})\n"
            + f"Crash Log: {self.log_path}\n"
            + sep + "\n"
        )
        with self._lock:
            if self._file and not self._file.closed:
                self._file.write(header)
                self._file.flush()

    def write_event(self, level, title, details=""):
        """Write a timestamped diagnostic or crash entry to the log file."""
        now = datetime.datetime.now().isoformat(timespec="seconds")
        block = f"\n[{now}] [{level}] {title}\n"
        if details:
            block += details.rstrip() + "\n"
        with self._lock:
            if self._file and not self._file.closed:
                try:
                    self._file.write(block)
                    self._file.flush()
                except OSError:
                    pass

    def _handle_sys_exception(self, exc_type, exc_value, exc_tb):
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self.write_event("UNCAUGHT_MAIN_EXCEPTION", f"{exc_type.__name__}: {exc_value}", tb_text)
        if callable(self._prev_sys_excepthook):
            self._prev_sys_excepthook(exc_type, exc_value, exc_tb)

    def _handle_threading_exception(self, args):
        exc_type = getattr(args, "exc_type", BaseException)
        exc_value = getattr(args, "exc_value", None)
        exc_tb = getattr(args, "exc_traceback", None)
        thread_obj = getattr(args, "thread", None)
        thread_name = getattr(thread_obj, "name", "UnknownThread")
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self.write_event(
            "UNCAUGHT_THREAD_EXCEPTION",
            f"Thread '{thread_name}' -> {exc_type.__name__}: {exc_value}",
            tb_text,
        )
        if callable(self._prev_threading_excepthook):
            self._prev_threading_excepthook(args)

    def _handle_qt_message(self, msg_type, context, message):
        should_log = False
        level = "QT_MSG"

        if QtMsgType is not None:
            if msg_type == QtMsgType.QtFatalMsg:
                should_log = True
                level = "QT_FATAL"
            elif msg_type == QtMsgType.QtCriticalMsg:
                should_log = True
                level = "QT_CRITICAL"
            elif msg_type == QtMsgType.QtWarningMsg and self.capture_warnings:
                should_log = True
                level = "QT_WARNING"

        if should_log:
            ctx_parts = []
            if context is not None:
                if getattr(context, "file", None):
                    ctx_parts.append(f"{context.file}:{getattr(context, 'line', 0)}")
                if getattr(context, "function", None):
                    ctx_parts.append(context.function)
            ctx_str = f" ({' | '.join(ctx_parts)})" if ctx_parts else ""
            self.write_event(level, f"{message}{ctx_str}")

        if callable(self._prev_qt_handler):
            try:
                self._prev_qt_handler(msg_type, context, message)
            except Exception:
                pass

    def close(self):
        """Flush and close the crash log file handle."""
        with self._lock:
            if self._file and not self._file.closed:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                self._file = None


_DEFAULT_REPORTER = None


def install_crash_reporter(app_name="WiggleView", log_dir=None, log_filename="wiggleview_crash.log", capture_warnings=False):
    """Install and return a singleton CrashReporter instance."""
    global _DEFAULT_REPORTER
    if _DEFAULT_REPORTER is None:
        _DEFAULT_REPORTER = CrashReporter(
            app_name=app_name,
            log_dir=log_dir,
            log_filename=log_filename,
            capture_warnings=capture_warnings,
        )
    _DEFAULT_REPORTER.install()
    return _DEFAULT_REPORTER


def get_crash_reporter():
    """Return the active CrashReporter instance if installed, else None."""
    return _DEFAULT_REPORTER
