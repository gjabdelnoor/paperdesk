"""One background thread that runs tablet operations strictly one at a time.

The backend API is synchronous, so every call is wrapped in a Job and executed
on a single worker thread.  Jobs are serialized, a job whose key is already
running or waiting is refused, and a failure cancels whatever was waiting
behind it: nothing is ever retried.
"""
from __future__ import annotations

import traceback
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

_UNCERTAIN_NAME_HINTS = ("unknownmutation", "uncertain", "unknownoutcome", "outcomeunknown",
                         "indeterminate", "unknownresult", "unknownstate")


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
    before: Callable[[], Any] | None = None
    cleanup: bool = False  # must survive a preceding cancelled/failed mutation
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)


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
    stopped = Signal()

    _dispatch = Signal(int, object)
    _stop = Signal()

    def __init__(self, parent: QObject | None = None, max_waiting: int = 12):
        super().__init__(parent)
        self.max_waiting = max_waiting
        self._queue: deque[Job] = deque()
        self._current: Job | None = None
        self._closed = False
        self._next_id = 1
        self.shutdown_errors: list[BaseException] = []
        self._thread = QThread()
        self._thread.setObjectName("paperdesk-operations")
        self._worker = _Worker()
        self._worker.moveToThread(self._thread)
        self._dispatch.connect(self._worker.execute)
        self._stop.connect(self._worker.stop)
        self._worker.finished.connect(self._on_finished)
        self._thread.finished.connect(self._on_stopped)
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
        if not job.cleanup and len(self._queue) >= self.max_waiting:
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
            job = self._current
            def execute():
                if job.cancelled.is_set():
                    raise RuntimeError("Operation cancelled before execution.")
                if job.before is not None:
                    job.before()
                # A stop can arrive between dispatch and begin_job, whose
                # reset must never erase a stop for this already queued job.
                if job.cancelled.is_set():
                    raise RuntimeError("Operation cancelled before execution.")
                return job.fn()
            self._dispatch.emit(job.id, execute)

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
            dropped = [waiting for waiting in self._queue if not waiting.cleanup]
            self._queue = deque(waiting for waiting in self._queue if waiting.cleanup)
            self.failed.emit(job, error)
            self._call(job.on_error, error)
            for waiting in dropped:
                self.rejected.emit(waiting, "cancelled")
                self._call(waiting.on_error, RuntimeError("Queued operation cancelled before execution."))
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
    def cancel_current(self) -> None:
        if self._current is not None and not self._current.cleanup:
            self._current.cancelled.set()

    def cancel_waiting(self, predicate: Callable[[Job], bool] | None = None) -> None:
        """Discard only the selected waiting jobs; never interrupt their worker."""
        dropped = [job for job in self._queue if not job.cleanup and (predicate is None or predicate(job))]
        self._queue = deque(job for job in self._queue if job not in dropped)
        for job in dropped:
            self.rejected.emit(job, "cancelled")
            self._call(job.on_error, RuntimeError("Queued operation cancelled before execution."))
        self.changed.emit()

    @Slot()
    def _on_stopped(self) -> None:
        self._current = None
        self.changed.emit()
        self.stopped.emit()

    def shutdown(self, final: list[Callable[[], Any]] | None = None, timeout_ms: int = 0) -> bool:
        """Drop waiting jobs, run the given closing calls, stop the thread.

        Returns True only after the thread has finished. The owner keeps the
        window and its files alive until ``stopped``; no running thread is
        abandoned, and cleanup never clears a pending cancellation signal.
        """
        if not self._thread.isRunning():
            self._closed = True
            self._current = None
            return True
        if not self._closed:
            self._closed = True
            self.cancel_waiting()
            self._queue.clear()  # final cleanup supersedes any queued cleanup
            def cleanup():
                for fn in final or []:
                    try:
                        fn()
                    except BaseException as exc:
                        self.shutdown_errors.append(exc)
            self._dispatch.emit(-1, cleanup)
            self._stop.emit()
        done = self._thread.wait(timeout_ms)
        if done:
            self._current = None
        return done
