"""One background thread that runs tablet operations strictly one at a time.

The backend API is synchronous, so every call is wrapped in a Job and executed
on a single worker thread.  Jobs are serialized, a job whose key is already
running or waiting is refused, and a failure cancels whatever was waiting
behind it: nothing is ever retried.
"""
from __future__ import annotations

import traceback
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

_UNCERTAIN_NAME_HINTS = ("unknownmutation", "uncertain", "unknownoutcome", "outcomeunknown",
                         "indeterminate", "unknownresult", "unknownstate")
_orphans: list[tuple[QThread, QObject]] = []


def is_uncertain(exc: BaseException) -> bool:
    """True when the backend says a mutation may or may not have been applied."""
    for attr in ("uncertain", "outcome_unknown", "unknown_outcome", "no_retry"):
        if getattr(exc, attr, False) is True:
            return True
    if getattr(exc, "retry_safe", None) is False:
        return True
    for cls in type(exc).__mro__:
        name = cls.__name__.lower().replace("_", "")
        if any(hint in name for hint in _UNCERTAIN_NAME_HINTS):
            return True
    return False


@dataclass(eq=False)
class Job:
    name: str
    label: str
    fn: Callable[[], Any]
    key: str | None = None
    mutation: bool = False
    quiet: bool = False
    partial: bool = False  # any failure may leave the change half-applied (streamed pen input)
    on_done: Callable[[Any], None] | None = None
    on_error: Callable[[BaseException], None] | None = None
    id: int = 0


class _Worker(QObject):
    finished = Signal(int, object, object)

    @Slot(int, object)
    def execute(self, job_id: int, fn: Callable[[], Any]) -> None:
        result, error = None, None
        try:
            result = fn()
        except BaseException as exc:  # reported to the GUI thread, never swallowed
            error = exc
        self.finished.emit(job_id, result, error)

    @Slot()
    def stop(self) -> None:
        QThread.currentThread().quit()


class OperationRunner(QObject):
    started = Signal(object)
    succeeded = Signal(object, object)
    failed = Signal(object, object)
    rejected = Signal(object, str)
    changed = Signal()

    _dispatch = Signal(int, object)
    _stop = Signal()

    def __init__(self, parent: QObject | None = None, max_waiting: int = 12):
        super().__init__(parent)
        self.max_waiting = max_waiting
        self._queue: deque[Job] = deque()
        self._current: Job | None = None
        self._closed = False
        self._next_id = 1
        self._thread = QThread()
        self._thread.setObjectName("paperdesk-operations")
        self._worker = _Worker()
        self._worker.moveToThread(self._thread)
        self._dispatch.connect(self._worker.execute)
        self._stop.connect(self._worker.stop)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    # -- state ---------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self._current is not None or bool(self._queue)

    @property
    def current(self) -> Job | None:
        return self._current

    @property
    def waiting(self) -> int:
        return len(self._queue)

    @property
    def running(self) -> bool:
        return self._thread.isRunning()

    def has_key(self, key: str) -> bool:
        if self._current is not None and self._current.key == key:
            return True
        return any(job.key == key for job in self._queue)

    # -- submission ----------------------------------------------------
    def submit(self, job: Job) -> bool:
        if self._closed:
            return False
        if job.key is not None and self.has_key(job.key):
            self.rejected.emit(job, "duplicate")
            return False
        if len(self._queue) >= self.max_waiting:
            self.rejected.emit(job, "full")
            return False
        job.id = self._next_id
        self._next_id += 1
        self._queue.append(job)
        self._pump()
        self.changed.emit()
        return True

    def _pump(self) -> None:
        if self._current is None and self._queue and not self._closed:
            self._current = self._queue.popleft()
            self.started.emit(self._current)
            self._dispatch.emit(self._current.id, self._current.fn)

    @Slot(int, object, object)
    def _on_finished(self, job_id: int, result: Any, error: BaseException | None) -> None:
        job = self._current
        if job is None or job.id != job_id or self._closed:
            return
        self._current = None
        if error is None:
            self.succeeded.emit(job, result)
            self._call(job.on_done, result)
        else:
            dropped = list(self._queue)
            self._queue.clear()
            self.failed.emit(job, error)
            self._call(job.on_error, error)
            for waiting in dropped:
                self.rejected.emit(waiting, "cancelled")
        self._pump()
        self.changed.emit()

    @staticmethod
    def _call(callback: Callable | None, value: Any) -> None:
        if callback is None:
            return
        try:
            callback(value)
        except Exception:  # a display bug must not stall the operation queue
            traceback.print_exc()

    # -- teardown ------------------------------------------------------
    def shutdown(self, final: list[Callable[[], Any]] | None = None, timeout_ms: int = 8000) -> bool:
        """Drop waiting jobs, run the given closing calls, stop the thread.

        Returns True when the thread has finished.  A backend call that never
        returns cannot be interrupted; in that case the thread is parked so Qt
        does not destroy it while running, and False is returned.
        """
        if not self._thread.isRunning():
            self._closed = True
            return True
        self._closed = True
        self._queue.clear()
        for fn in final or []:
            self._dispatch.emit(-1, _guarded(fn))
        self._stop.emit()
        done = self._thread.wait(timeout_ms)
        if not done:
            _orphans.append((self._thread, self._worker))
        self._current = None
        return done


def _guarded(fn: Callable[[], Any]) -> Callable[[], Any]:
    def call() -> None:
        try:
            fn()
        except Exception:
            pass
    return call
