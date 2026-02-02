"""Database connection management."""

import os
import sqlite3
from pathlib import Path
from typing import Optional

# Global connection cache
_connection: Optional[sqlite3.Connection] = None
_db_path: Optional[str] = None


def get_db_path(override: Optional[str] = None) -> str:
    """
    Get the database path.

    Priority:
    1. Explicit override parameter
    2. MTGC_DB environment variable
    3. Default: $HOME/.mtgc/collection.sqlite
    """
    if override:
        return override

    env_path = os.environ.get("MTGC_DB")
    if env_path:
        return env_path

    home = Path.home()
    default_dir = home / ".mtgc"
    return str(default_dir / "collection.sqlite")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Get or create a database connection.

    Uses a cached connection for the same path.
    """
    global _connection, _db_path

    path = get_db_path(db_path)

    # Return cached connection if path matches
    if _connection is not None and _db_path == path:
        return _connection

    # Close existing connection if path changed
    if _connection is not None:
        _connection.close()

    # Ensure directory exists
    db_dir = Path(path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    # Create new connection
    _connection = sqlite3.connect(path)
    _connection.row_factory = sqlite3.Row
    _connection.execute("PRAGMA foreign_keys = ON")
    _db_path = path

    return _connection


def close_connection():
    """Close the cached connection if one exists."""
    global _connection, _db_path

    if _connection is not None:
        _connection.close()
        _connection = None
        _db_path = None
