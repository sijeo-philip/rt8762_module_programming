""" Lifecycle management for application worker threads."""

from __future__ import annotations
import logging

from collections.abc import Callable
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from stationapp.concurrency.errors import OperationConflict
from stationapp.concurrency.worker import OperationCallable, OperationWorker

logger = logging.getLogger(__name__)

class OperationHandle(QObject):
    """ Owned handle for one running worker and its thread."""
    
    cleaned_up = pyqtSignal(str)
    
    def __init__(self, thread: QThread, worker: OperationWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.thread = thread
        self.worker = worker
        self.operation_id = worker.operation_id
        self._cleanup_complete = False
        
    @property
    def is_running(self) -> bool:
        return self.thread.isRunning()
        
        
    def cancel(self)-> None:
        # CancellationToken is thread safe. Calling this directly sets on the 
        #token; it does not invoke device or GUI methods cross-thread.
        self.worker.cancellation_token.cancel()
        
    def wait(self, timeout_ms: int) -> bool:
        return self.thread.wait(timeout_ms)
        
    def mark_cleaned_up(self) -> None:
        if self._cleanup_complete:
            return 
        self._cleanup_complete=True
        self.cleaned_up.emit(self.operation_id)
        
        
class OperationManager(QObject):
    """Starts, tracks, cancels, and cleans up worker threads."""
    
    operation_started = pyqtSignal(object)
    operation_removed = pyqtSignal(str)
    
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handles: dict[str, OperationHandle] = {}
        
        
    @property
    def active_count(self) -> int:
        return len(self._handles)
        
    @property
    def active_operation_ids(self) -> tuple[str, ...]:
        return tuple(self._handles)
        
    def start(self, operation: OperationCallable, *, operation_id: str | None = None, configure: Callable[[OperationWorker], None] | None = None) -> OperationHandle:
        if operation_id is not None and operation_id in self._handles:
            raise OperationConflict(f"Operation {operation_id} is already running.")
            
        thread = QThread(self)
        worker = OperationWorker(operation, operation_id)
        worker.moveToThread(thread)
        
        handle = OperationHandle(thread, worker, self)
        self._handles[handle.operation_id] = handle
        
        if configure is not None:
            configure(worker)
            
        thread.started.connect(worker.run)
        
        #'finished' is emitted for success, failure, timeout, or cancellation
        worker.finished.connect(thread.quit)
        
        # Delete the worker after its thread event loop processes the request.
        worker.finished.connect(worker.deleteLater)
        
        # The QThread object belongs to the GUI thread. Mark cleanup only after 
        # QT reports the worker thread has stopped.
        
        thread.finished.connect(lambda operation_id=handle.operation_id: self._remove(operation_id))
        thread.finished.connect(thread.deleteLater)
        
        thread.start()
        self.operation_started.emit(handle)
        return handle
        
        
    def get(self, operation_id:str) -> OperationHandle | None:
        return self._handles.get(operation_id)
        
    def cancel(self, operation_id: str) -> bool:
        handle = self._handles.get(operation_id)
        if handle is None:
            return False
            
        handle.cancel()
        return True
        
    def cancel_all(self) -> None:
        for handle in tuple(self._handles.values()):
            handle.cancel()
            
            
    def wait_for_all(self, timeout_ms: int) -> bool:
        """ Wait up to one total deadline for all active threads"""
        from time import monotonic
        deadline = monotonic() + timeout_ms / 1000.0
        
        for handle in tuple(self._handles.values()):
            remaining_ms = max(0, int((deadline - monotonic())*1000))
            if not handle.wait(remaining_ms):
                return False
        return True
        
        
    def _remove(self, operation_id: str)->None:
        handle = self._handles.pop(operation_id, None)
        if handle is None:
            return
            
        handle.mark_cleaned_up()
        self.operation_removed.emit(operation_id)
        
        logger.debug("Operation resources removed | operation_id=%s",operation_id)
        
        
        
    
    