"""PSX data access: HTTP, archives, portal dumps, timeseries, disclosures, macro.

Imports are lazy on purpose.  ``psx_desk.indicators`` depends on
``psx_desk.dataflows.timeseries``, and ``ingest`` depends on ``indicators``, so
eagerly importing every submodule here would create a cycle the first time an
agent module is imported.  Import the submodule you need, or use the names below.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DeskData",
    "IngestionError",
    "MarketDump",
    "MarketDumpUnavailable",
    "MarketRow",
    "build_desk_data",
    "build_market_packet",
    "ingest",
    "scrape_market_dump",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial indirection
    if name in {"DeskData", "IngestionError", "build_desk_data", "build_market_packet", "ingest"}:
        from psx_desk.dataflows import ingest as _ingest

        return getattr(_ingest, name)
    if name in {"MarketDump", "MarketDumpUnavailable", "MarketRow", "scrape_market_dump"}:
        from psx_desk.dataflows import portal as _portal

        return getattr(_portal, name)
    raise AttributeError(f"module 'psx_desk.dataflows' has no attribute {name!r}")
