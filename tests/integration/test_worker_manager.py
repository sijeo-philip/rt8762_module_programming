
from __future__ import annotations

import threading
from typing import Any

import pytest
from PyQt6.QtCore import QThread

from stationapp.concurrency.events import OperationFailure, OperationResult
from stationapp.concurrency.manager import OperationManager

@pytest.mark.integration
def test_worker_runs_outside_gui_thread(qtbot) -> None:
    manager = OperationManager()
    received: list[OperationResult] = []
    worker_thread_ids: list[int] = []
    gui_thread = QThread.currentThread()
    
    def operation(operation_id, token, report_progress) -> dict[str, Any]:
        worker_thread_ids.append(threading.get_ident())
        return {"current_qthread": QThread.currentThread()}
        
    def configure(worker) -> None:
        worker.succeeded.connect(received.append)
        
    manager.start(operation, operation_id="thread-test", configure=configure)
    
    qtbot.waitUntil(lambda: len(received) == 1, timeout=2_000)
    qtbot.waitUntil(lambda: manager.active_count == 0, timeout=2_000)
    
    assert received[0].value["current_qthread"] is not gui_thread
    assert len(worker_thread_ids) == 1

@pytest.mark.integration
def test_success_is_delivered_and_thread_is_cleaned_up(qtbot) -> None:
    manager = OperationManager()
    successes: list[OperationResult] = []
    
    def operation(operation_id, token, report_progress):
        return {"status": "PASS"}
        
    def configure(worker) -> None:
        worker.succeeded.connect(successes.append)
        
    manager.start(operation, operation_id="success-test", configure=configure)
    
    qtbot.waitUntil(lambda: len(successes) == 1, timeout=2_000)
    qtbot.waitUntil(lambda: manager.active_count == 0, timeout=2_000)
    
    assert successes[0].operation_id == "success-test"
    assert successes[0].value == {"status": "PASS"}


@pytest.mark.integration
def test_unexpected_exception_becomes_failure(qtbot) -> None:
    manager = OperationManager()
    failures: list[OperationFailure] = []
    
    def operation(operation_id, token, report_progress):
        raise RuntimeError("simulated defect")
        
    def configure(worker) -> None:
        worker.failed.connect(failures.append)
        
    manager.start(operation, operation_id="failure-test", configure=configure)
    
    qtbot.waitUntil(lambda: len(failures) == 1, timeout=2_000)
    qtbot.waitUntil(lambda: manager.active_count == 0, timeout=2_000)
    
    failure = failures[0]
    assert failure.error_type == "RuntimeError"
    assert "unexpected error" in failure.user_message.lower()
    assert "simulated defect" in failure.technical_message
    
@pytest.mark.integration
def test_cancellation_stops_worker(qtbot)->None:
    manager = OperationManager()
    cancelled: list[str] = []
    
    def operation(operation_id, token, report_progress):
        token.cancel()
        token.raise_if_cancelled()
        
    def configure(worker) -> None:
        worker.cancelled.connect(cancelled.append)
        
    manager.start(operation, operation_id = "cancel-test", configure=configure)
    
    qtbot.waitUntil(lambda: cancelled == ["cancel-test"], timeout=2_000)
    qtbot.waitUntil(lambda: manager.active_count == 0, timeout=2_000)
    
    