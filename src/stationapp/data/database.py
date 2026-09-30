""" Local SQLite database configuration.

The station database is delibrately configured for durability

Important production ruels:
* SQLite is local to one station PC
* Foreign-Key enforcement is enabled every connection .
* WAL is used for crash-safe concurrent reads.
* synchronous = FULL favours durability over raw write speed.
* busy_timeout prevents short write-contention periods from immediately
  falling with " database is locked"
  
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_BUSY_TIMEOUT_MS = 5000

def _ensure_sqlite_parent_directory(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database."""
    
    url = make_url(database_url)
    
    if url.get_backend_name() != "sqlite":
        return
        
    database = url.database
    
    if database is None:
        return
        
        
    if database == ":memory:":
        return 
        
    path = Path(database)
    
    if not path.is_absolute():
        path = Path.cwd() / path
        
    path.parent.mkdir(parents=True, exist_ok=True)
    
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Apply mandatory SQLite settings to every new DB-API connection. """
    
    cursor = dbapi_connection.cursor()
    
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA synchronous = FULL")
        cursor.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        
    finally:
        cursor.close()
        
def create_station_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create the database engine used by one programming station."""
    
    _ensure_sqlite_parent_directory(database_url)
    engine = create_engine(database_url, echo=echo, future=True)
    
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
        
    return engine
    

def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create the application's SQLAlchemy session factory."""
    
    return sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)
    
    
