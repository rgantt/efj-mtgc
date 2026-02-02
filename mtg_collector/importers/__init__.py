"""Importers for various collection platforms."""

from mtg_collector.importers.base import BaseImporter
from mtg_collector.importers.moxfield import MoxfieldImporter
from mtg_collector.importers.archidekt import ArchidektImporter
from mtg_collector.importers.deckbox import DeckboxImporter

IMPORTERS = {
    "moxfield": MoxfieldImporter,
    "archidekt": ArchidektImporter,
    "deckbox": DeckboxImporter,
}


def get_importer(format_name: str) -> BaseImporter:
    """Get an importer by format name."""
    importer_class = IMPORTERS.get(format_name.lower())
    if not importer_class:
        raise ValueError(f"Unknown import format: {format_name}. Available: {', '.join(IMPORTERS.keys())}")
    return importer_class()


def detect_format(file_path: str) -> str:
    """
    Auto-detect the format of a CSV file.

    Returns format name or raises ValueError if unknown.
    """
    import csv

    with open(file_path, "r", encoding="utf-8") as f:
        # Try to detect delimiter
        sample = f.read(4096)
        f.seek(0)

        # Archidekt uses semicolon
        if ";" in sample and sample.count(";") > sample.count(","):
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        else:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")

        f.seek(0)
        reader = csv.reader(f, dialect)
        headers = next(reader, [])
        headers_lower = [h.lower().strip() for h in headers]

    # Check for Archidekt-specific columns
    if "scryfall_uuid" in headers_lower or "export_type" in headers_lower:
        return "archidekt"

    # Check for Deckbox-specific columns
    if "signed" in headers_lower or "artist proof" in headers_lower or "altered art" in headers_lower:
        return "deckbox"

    # Check for Moxfield columns
    if "edition" in headers_lower and "collector number" in headers_lower:
        return "moxfield"

    # Default guess based on common patterns
    if "count" in headers_lower and "name" in headers_lower:
        return "moxfield"

    raise ValueError("Could not auto-detect CSV format. Please specify --format explicitly.")


__all__ = [
    "BaseImporter",
    "MoxfieldImporter",
    "ArchidektImporter",
    "DeckboxImporter",
    "get_importer",
    "detect_format",
    "IMPORTERS",
]
