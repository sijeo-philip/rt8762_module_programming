"""Cooperative cancellation primitives.

Python cannot safely terminate a thread. A station operation must stop at well-defined
checkpoints, close its resources, and then  report cancellation.
"""

from __future__ import annotations
from threading import Event

class OperationCancelled(Exception):
    """Raised when a running operation observes a cancellation request. """
    
    
class CancellationToken:
    """Thread safe, cooperative cancellation token."""
    
    def __init__(self) -> None:
        self._event = Event()
        
    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()
        
    def cancel(self) -> None:
        self._event.set()
        
    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise OperationCancelled("Operation Cancelled")
            
            