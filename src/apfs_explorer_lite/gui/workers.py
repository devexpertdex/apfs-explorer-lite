"""Run backend calls off the GUI thread and deliver results back on it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..errors import ApfsExplorerError

logger = logging.getLogger(__name__)

SuccessCallback = Callable[[Any], None]
ErrorCallback = Callable[[ApfsExplorerError], None]


class _Task(QRunnable):
    def __init__(self, runner: TaskRunner, task_id: int, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._runner = runner
        self._task_id = task_id
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except ApfsExplorerError as exc:
            self._runner._done.emit(self._task_id, False, exc)
        except Exception as exc:  # noqa: BLE001 - every failure must reach the UI
            logger.exception("Unexpected error in background task")
            self._runner._done.emit(
                self._task_id,
                False,
                ApfsExplorerError("Something unexpected went wrong.", detail=repr(exc)),
            )
        else:
            self._runner._done.emit(self._task_id, True, result)


class TaskRunner(QObject):
    """One background thread, so calls into a backend never overlap.

    Callbacks run on the GUI thread, in submission order.
    """

    busyChanged = Signal(bool)
    _done = Signal(int, bool, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pending: dict[int, tuple[SuccessCallback, ErrorCallback]] = {}
        self._next_id = 0
        self._done.connect(self._dispatch)

    @property
    def is_busy(self) -> bool:
        return bool(self._pending)

    def run(self, fn: Callable[[], Any], on_success: SuccessCallback, on_error: ErrorCallback) -> None:
        self._next_id += 1
        self._pending[self._next_id] = (on_success, on_error)
        if len(self._pending) == 1:
            self.busyChanged.emit(True)
        self._pool.start(_Task(self, self._next_id, fn))

    def wait(self, msecs: int = -1) -> bool:
        return self._pool.waitForDone(msecs)

    @Slot(int, bool, object)
    def _dispatch(self, task_id: int, ok: bool, payload: object) -> None:
        callbacks = self._pending.pop(task_id, None)
        if not self._pending:
            self.busyChanged.emit(False)
        if callbacks is None:
            return
        on_success, on_error = callbacks
        if ok:
            on_success(payload)
        else:
            on_error(payload)  # type: ignore[arg-type]
