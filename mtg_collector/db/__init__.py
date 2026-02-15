"""Database layer for MTG Collector."""

from mtg_collector.db.connection import close_connection, get_connection, get_db_path
from mtg_collector.db.models import (
    CardRepository,
    CollectionRepository,
    OrderRepository,
    PrintingRepository,
    SetRepository,
    WishlistRepository,
)
from mtg_collector.db.schema import SCHEMA_VERSION, init_db

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
    "OrderRepository",
    "WishlistRepository",
]
