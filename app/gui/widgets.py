from __future__ import annotations

import sys
import traceback
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from PySide6.QtWidgets import QListWidget, QAbstractItemView


class DropListWidget(QListWidget):
    """Danh sách file nhận kéo-thả từ Finder."""

    filesDropped = Signal(list)

    def __init__(self, extensions: Optional[List[str]] = None, multi: bool = True, parent=None):
        super().__init__(parent)
        self.extensions = [e.lower() for e in extensions] if extensions else None
        self.setAcceptDrops(True)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.setAlternatingRowColors(True)
        self.setToolTip("Kéo-thả file vào đây")
        if not multi:
            self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._multi = multi

    def _matches(self, path: str) -> bool:
        if not self.extensions:
            return True
        lower = path.lower()
        return any(lower.endswith(ext) for ext in self.extensions)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return

        paths = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and self._matches(path):
                paths.append(path)

        if paths:
            if not self._multi:
                self.clear()
                paths = paths[:1]
            for path in paths:
                existing = [self.item(i).text() for i in range(self.count())]
                if path not in existing:
                    self.addItem(path)
            self.filesDropped.emit(paths)
        event.acceptProposedAction()

    def file_paths(self) -> List[str]:
        return [self.item(i).text() for i in range(self.count())]

    def first_path(self) -> Optional[str]:
        return self.item(0).text() if self.count() > 0 else None


class _StreamToSignal:
    """File-like object chuyển write() thành tín hiệu Qt, dùng để redirect stdout/stderr."""

    def __init__(self, emit_func: Callable[[str], None]):
        self._emit = emit_func
        self._buffer = ""

    def write(self, data: str) -> int:
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""


class WorkerSignals(QObject):
    log = Signal(str)
    finished = Signal(bool, str)


class Worker(QRunnable):
    """Chạy 1 hàm nền, forward print()/progress_cb vào log, không chặn UI."""

    def __init__(self, fn: Callable[..., str], *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def _emit_log(self, line: str) -> None:
        self.signals.log.emit(line)

    @Slot()
    def run(self) -> None:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        redirect = _StreamToSignal(self._emit_log)
        sys.stdout = redirect  # type: ignore[assignment]
        sys.stderr = redirect  # type: ignore[assignment]
        try:
            if "progress_cb" in self.kwargs or _accepts_progress_cb(self.fn):
                self.kwargs.setdefault("progress_cb", self._emit_log)
            result = self.fn(*self.args, **self.kwargs)
            redirect.flush()
            self.signals.finished.emit(True, str(result) if result else "Hoàn tất.")
        except Exception as exc:  # noqa: BLE001
            redirect.flush()
            self._emit_log(traceback.format_exc())
            self.signals.finished.emit(False, str(exc))
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr


def _accepts_progress_cb(fn: Callable) -> bool:
    try:
        import inspect

        return "progress_cb" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
