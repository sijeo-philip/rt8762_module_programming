""" Generic worker for blocking station operations."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from stationapp.concurrency.cancellation import (CancellationToken, OperationCancelled)
from stationapp.concurrency.errors import OperationError, OperationTimeout
from stationapp.concurrency.events import ( OperationFailure, OperationResult, ProgressEvent)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[ProgressEvent], None]
OperationCallable = Callable[[str, CancellationToken, ProgressCallback], Any]


class OperationWorker(QObject):
    """ Runs one blocking operation in a worker thread.
    The operation callable receives:
        operation_id
        cancellation token
        progress callback
    It returns any application result. The worker wraps that result in an 
    OperationResult and sends it to the GUI
    """
    
    started = pyqtSignal(str)
    progress = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)
    cancelled = pyqtSignal(str)
    finished = pyqtSignal(str)
    
    def __init__(self, operation: OperationCallable,
        *,
        operation_id: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.operation_id = operation_id or str(uuid4())
        self._operation = operation
        self._token = CancellationToken()
        self._has_started = False
        
    @property
    def cancellation_token(self) -> CancellationToken:
        return self._token
    
    @pyqtSlot()
    def run(self) -> None:
        if self._has_started:
            logger.error("Worker run called more than once | operation_id=%s",self.operation_id)
            return 
            
        self._has_started = True
        started_at = datetime.now(timezone.utc)
        self.started.emit(self.operation_id)
        
        logger.info("Operation started | operation_id=%s",self.operation_id)
        
        try:
            self._token.raise_if_cancelled()
            
            value = self.operation(self.operation_id, self._token, self.progress.emit)
            self._token.raise_if_cancelled()
            result = OperationResult(operation_id=self.operation_id, value=value, started_at = started_at, completed_at = datetime.now(timezone.utc))
            self.succeeded.emit(result)
            

            logger.info("Operation succeeded | operation_id=%s elapsed=%.3fs",self.operation_id, result.elapsed_seconds)
            
        except OperationCancelled:
            logger.info("Operation Cancelled | operation_id=%s, self.operation_id)
            self.cancelled.emit(self.operation_id)
            
        except OperationTimeout as exc:
            logger.warning("Operation timed out | operation_id=%s error=%s", self.operation_id, exc)
            self.failed.emit(OperationFailure(operation_id=self.operation_id, 
                                error_type=type(exc).__name__,
                                user_message="The operation timed out.",
                                technical_message=str(exc),
                                )
                            )
        except OperationError as exc:
            logger.warning("Operation Failed | operation_id=%s error=%s", self.operation_id, exc)
            self.failed.emit(OperationFailure(
                                operation_id=self.operation_id,
                                error_type=type(exc).__name__,
                                user_message=str(exc),
                                technical_message=str(exc)))
        except Exception as exc:
            technical = "".join(trackback.format_exception(type(exc), exc, exc.__traceback__))
            logger.exception("Unexcepted Operation Failure | operation_id=%s", self.operation_id)
            self.failed.emit(OperationFailure(
                                operation_id=self.operation_id,
                                error_type=type(exc).__name__,
                                user_message=("An unexpected error occured. The operation was stopped."),
                                technical_message=technical,
                            )
                        )
                        
        finally:
            self.finished.emit(self.operation_id)
            
    @pyqtSlot()
    def request_cancel(self) -> None:
        logger.info("Cancellation Requested | operation_id=%s",self.operation_id)
        self._token.cancel()
        