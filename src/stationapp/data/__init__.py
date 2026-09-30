"""Persistence layer for the programming station."""

from stationapp.data.base import Base

# Import models so SQLAlchemy registers every table with Base.metadata.
from stationapp.data import models as models

__all__ = [
    "Base",
    "models",
]

