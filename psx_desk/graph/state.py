"""LangGraph state for the PSX desk.

The graph carries typed objects, not prose: the packet, the measured snapshot,
the two analyst reports, the accumulating debate turns and finally the CRO's
allocation statement.  Each node writes exactly one key (or appends to one list),
so a node can be re-run or faked in a test without disturbing the rest.
"""

from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, TypedDict

from psx_desk.dataflows.timeseries import PriceHistory
from psx_desk.indicators import TechnicalSnapshot
from psx_desk.schemas import (
    AllocationStatement,
    DebateArgument,
    DebateTranscript,
    FundamentalReport,
    PSXMarketPacket,
    TechnicalReport,
)


class DeskState(TypedDict, total=False):
    """Everything one run knows, keyed by the node that produced it."""

    # inputs
    symbol: str
    trade_date: date

    # node 1 — ingestion
    packet: PSXMarketPacket
    history: PriceHistory
    snapshot: TechnicalSnapshot

    # node 2 / node 3
    technical: TechnicalReport
    fundamental: FundamentalReport

    # node 4 — debate (appended, one turn at a time)
    bull_arguments: Annotated[list[DebateArgument], operator.add]
    bear_arguments: Annotated[list[DebateArgument], operator.add]
    debate: DebateTranscript

    # node 5 — CRO
    allocation: AllocationStatement

    # operational log, appended by any node
    notes: Annotated[list[str], operator.add]


NODE_INGEST = "PSX Data Ingestion"
NODE_TECHNICAL = "PSX Technical Analyst"
NODE_FUNDAMENTAL = "PSX Fundamental Analyst"
NODE_BULL = "Bull Researcher"
NODE_BEAR = "Bear Researcher"
NODE_CRO = "CRO Gatekeeper"

NODE_ORDER = (
    NODE_INGEST,
    NODE_TECHNICAL,
    NODE_FUNDAMENTAL,
    NODE_BULL,
    NODE_BEAR,
    NODE_CRO,
)
