from __future__ import annotations

class PersistenceError(Exception):
    """ Base class for station persistence failures. """

class AllocationImportConflict(PersistenceError):
        """An allocation conflicts with data already stored locally """

class DuplicateMacInDatabase(PersistenceError):
      """A MAC already exists in the station database"""

class BatchAlreadyExists(PersistenceError):
      """A batch identifier already exists."""

class BatchNotFound(PersistenceError):
      """The requested persisted batch does not exist"""

class InsufficientMacAvailability(PersistenceError):
      """ A Complete Jig load cannot be reserved atomically """

class InvalidPersistedBatch(PersistenceError):
      """ Persisted Batch STructure is not valid for production """

class DuplicateModuleQrInDatabase(PersistenceError):
      """A module QR is already bound to another persisted slot."""

class NoQrSlotAvailable(PersistenceError):
      """All module QRs have already been bound for the batch """

class IncompleteSlotIdentity(PersistenceError):
      """A slot does not yet have both required MAC identities. """