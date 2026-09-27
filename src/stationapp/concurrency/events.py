"""Structured messages sent from workers to the GUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A Progress update emitted by a worker."""
    
    operation_id: str
    stage: str
    message: str
    completed: int | None = None
    total: int | None = None
    slot_number: int | None = None
    occured_at: datetime | None = None
    
    def __post_init__(self) -> None:
        if self.occured_at is None:
            object.__setattr__(self, "occured_at", datetime.now(timezone.utc))
            
        if self.completed is not None and self.completed < 0:
            raise ValueError("completed cannot be negative")
        
        if self.total is not None and self.total <= 0:
            raise ValueError("total must be positive")
            
            
        if( self.completed is not None
            and self.total is not None
            and self.completed > self.total):
            raise ValueError("completed cannot exceed total")
            
            
@dataclass(frozen=True, slots=True)
class OperationResult:
    """Successful result returned by a worker"""
    
    operation_id: str
    value: Any
    started_at: datetime
    completed_at: datetime
    
    @property
    def elapsed_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()
        

@dataclass(frozen=True, slots=True)
class OperationFailure:
    """ Safe failure information returned to the GUI"""
    
    operation_id: str
    error_type: str
    user_message: str
    technical_message: str
    