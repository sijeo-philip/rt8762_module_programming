""" Slow demonstration operation used to excercise worker infrastructure.

This module is temporary training code. It is delibrately shaped like the future
golden rig operation: process a list of addresses one at a time report each result,
honour cancellation, and enforce a deadline.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep

from stationapp.concurrency.cancellation import CancellationToken
from stationapp.concurrency.errors import OperationTimeout
from stationapp.concurrency.events import ProgressEvent
from stationapp.concurrency.worker import ProgressCallback

@dataclass(frozen=True, slots=True)
class DemoDeviceResult:
    mac: str
    connected: bool
    disconnected: bool
    
    
def make_demo_rf_test(mac_addresses: list[str], *, seconds_per_device: float = 0.75, timeout_seconds: float = 30.0):
    """ Return an operation callable suitable for OperationManager.start()."""
    
    addresses = tuple(mac_addresses)
    
    def operation(operation_id: str,  token: CancellationToken, report_progress: ProgressCallback) -> list[DemoDeviceResult]:
        started = monotonic()
        results: list[DemoDeviceResult] = []
        
        for index, mac in enumerate(addresses, start=1):
            token.raise_if_cancelled()
            
            if monotonic() - started >= timeout_seconds:
                raise OperationTimeout(f"RF test exceeded {timeout_seconde:.1f} seconds")
                
            report_progress(ProgressEvent(
                                operation_id=operation_id,
                                stage="rf_connect",
                                message=f"Connecting to {mac}",
                                completed = index - 1,
                                total = len(addresses),
                                slot_number=index )
                            )
                            
                            
            #Sleep in small pieces so cancellation is observed quickly
            remaining = seconds_per_device
            while remaining > 0:
                token.raise_if_cancelled()
                
                if monotonic() - started >= timeout_seconds:
                    raise OperationTimeout(f"RF test exceeded {timeout_seconds:.1f} seconds")
                    
                interval = min(0.05, remaining)
                sleep(interval)
                remaining -= interval
                
            result = DemoDeviceResult(mac=mac, connected=True, disconnected=True)
            results.append(result)
            
            report_progress(ProgressEvent(
                            operation_id=operation_id,
                            stage="rf_complete",
                            message=f"{mac} connected and disconnected",
                            completed=index,
                            total=len(addresses),
                            slot_number=index )
                        )
                        
        return results
    return operation
                