"""Exporters for various collection platforms."""

from mtg_collector.exporters.base import BaseExporter
from mtg_collector.exporters.moxfield import MoxfieldExporter
from mtg_collector.exporters.archidekt import ArchidektExporter
from mtg_collector.exporters.deckbox import DeckboxExporter

EXPORTERS = {
    "moxfield": MoxfieldExporter,
    "archidekt": ArchidektExporter,
    "deckbox": DeckboxExporter,
}


def get_exporter(format_name: str) -> BaseExporter:
    """Get an exporter by format name."""
    exporter_class = EXPORTERS.get(format_name.lower())
    if not exporter_class:
        raise ValueError(f"Unknown export format: {format_name}. Available: {', '.join(EXPORTERS.keys())}")
    return exporter_class()


__all__ = ["BaseExporter", "MoxfieldExporter", "ArchidektExporter", "DeckboxExporter", "get_exporter", "EXPORTERS"]
