"""Expected application-operation errors."""

from __future__ import annotations

class OperationError(Exception):
    """Base class for expected operation failure."""
    
class OperationTimeout(OperationError):
    """Raised when an operation exceeds its deadlinel."""
    
class OperationConflict(OperationError):
    """Raised when another incompatible operation is already running."""
    
    
