"""PSX Trading Desk — an autonomous, PSX-only multi-agent research desk.

Five nodes, one exchange, one currency, one rulebook:

1. :mod:`psx_desk.dataflows.ingest`   — PSX Data Ingestion
2. :mod:`psx_desk.agents.technical`   — PSX Technical Analyst (circuit-aware)
3. :mod:`psx_desk.agents.fundamental` — PSX Fundamental Analyst (SBP-aware)
4. :mod:`psx_desk.agents.research`    — Bull vs Bear Researchers
5. :mod:`psx_desk.agents.risk`        — CRO Gatekeeper (strict allocation JSON)

Every fact comes from psx.com.pk / dps.psx.com.pk or sbp.org.pk.  There is no
US vendor, no Yahoo, no SEC EDGAR and no Finnhub anywhere in this package — and
:func:`psx_desk.sources.assert_source_allowed` enforces that at runtime.
"""

from __future__ import annotations

__version__ = "1.0.0"

from psx_desk.config import DeskConfig, RiskLimits, load_config
from psx_desk.graph.desk import PSXTradingDesk
from psx_desk.schemas import (
    AllocationStatement,
    CircuitState,
    DebateArgument,
    DebateTranscript,
    DefaulterSegment,
    DeskRun,
    FundamentalReport,
    MacroBackdrop,
    PSXMarketPacket,
    TechnicalReport,
)
from psx_desk.sources import normalize_symbol

__all__ = [
    "AllocationStatement",
    "CircuitState",
    "DebateArgument",
    "DebateTranscript",
    "DefaulterSegment",
    "DeskConfig",
    "DeskRun",
    "FundamentalReport",
    "MacroBackdrop",
    "PSXMarketPacket",
    "PSXTradingDesk",
    "RiskLimits",
    "TechnicalReport",
    "__version__",
    "load_config",
    "normalize_symbol",
]
