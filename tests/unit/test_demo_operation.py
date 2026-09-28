from __future__ import annotations

import pytest

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.concurrency.errors import OperationTimeout
from stationapp.services.demo_operation import make_demo_rf_test

@pytest.mark.unit
def test_demo_operation_reports_each_device() -> None:
    token = CancellationToken()
    progress = []
    
    operation = make_demo_rf_test(["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"], seconds_per_device=0.001, timeout_seconds=1.0)
    results = operation("test-op", token, progress.append)
    
    assert len(results) == 2
    assert all(result.connected for result in results)
    assert all(result.disconnected for result in results)
    assert progress[-1].completed == 2
    assert progress[-1].total == 2
    
@pytest.mark.unit
def test_demo_operation_honours_cancellation() -> None:
    token = CancellationToken()
    token.cancel()
    
    operation =  make_demo_rf_test(["AA:BB:CC:DD:EE:01"], seconds_per_device=0.001)
    
    with pytest.raises(Exception, match="Cancelled"):
        operation("test-op", token, lambda event: None)
        
        

@pytest.mark.unit
def test_demo_operation_times_out() -> None:
    token = CancellationToken()
    
    operation = make_demo_rf_test(["AA:BB:CC:DD:EE:01"], seconds_per_device=0.1, timeout_seconds=0.01)
    
    with pytest.raises(OperationTimeout):
        operation("test-op", token, lambda event: None)
        
        
    