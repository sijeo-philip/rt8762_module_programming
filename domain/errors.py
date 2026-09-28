""" Domain-specific errors.

These errors describe violations of manufacturing rules. They are not transport, 
database, or GUI errors
"""

from __future__ import annotations

class DomainError(Exception):
    """Base class for expected manufacturing rule violations."""
    
    
class InvalidMacAddress(DomainError):
    """Raised when a MAC address cannot be parsed or is not usable."""
    
class InvalidAllocation(DomainError):
    """A Server-issued allocation document violates domain rules."""
    
class AllocationExpired(InvalidAllocation):
    """An allocation is past its permitted use time."""
    
class AllocationOwnershipMismatch(InvalidAllocation):
    """An allocation belongs to another station or jig"""
    
class AllocationPurposeMismatch(InvalidAllocation):
    """An allocation is being used for the wrong manufacturing purpose."""
    
class AllocationExhausted(DomainError):
    """Not enough authourised MACs remain for the requested operation."""
    
    
class DuplicateModuleQr(DomainError):
    """Raised when one module QR is assigned more than once """
    
class DuplicateMacAddress(DomainError):
    """Raised when one MAC is assigned more than once within the batch"""
    
class SlotAlreadyOccupied(DomainError):
    """Raised when different module is bound to an occupied jig slot."""
    
class SlotNotBound(DomainError):
    """Raised when an operation requires a module-bound slot."""
    
class InvalidBatchTransition(DomainError):
    """Raised when the requested batch action is not valid in its state."""
    
class VerificationMismatch(DomainError):
    """Raised when actual device identity differs from issued identity."""
    
class BatchOnHold(DomainError):
    """Raised when a production action is attempted while the batch is held."""
    
    
class MacAlreadyAssigned(DomainError):
    """A slot already has a MAC for the requested purpose."""
    
class InvalidMacTransition(DomainError):
    """A MAC lifecycle transition is not permitted."""
    
class InvalidDeviceTransition(DomainError):
    """A device lifecycle transition is not permitted."""
    
