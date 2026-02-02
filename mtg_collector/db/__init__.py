"""Database layer for MTG Collector."""

from mtg_collector.db.connection import get_db_path, get_connection, close_connection
from mtg_collector.db.schema import init_db, SCHEMA_VERSION
from mtg_collector.db.models import (
    CardRepository,
    SetRepository,
    PrintingRepository,
    CollectionRepository,
)

__all__ = [
    "get_db_path",
    "get_connection",
    "close_connection",
    "init_db",
    "SCHEMA_VERSION",
    "CardRepository",
    "SetRepository",
    "PrintingRepository",
    "CollectionRepository",
]
