"""Assemble the PSX desk as a LangGraph state machine.

    START
      |
      v
    PSX Data Ingestion ──> PSX Technical Analyst ──> PSX Fundamental Analyst
                                                            |
                                                            v
                                                     Bull Researcher
                                                            |
                                                            v
                                                     Bear Researcher
                                                            |
                                      (rounds left?) ───────┴──> Bull Researcher
                                                            |
                                                            v
                                                      CRO Gatekeeper
                                                            |
                                                           END

The path is strictly sequential — analysts file in order, the debate then runs
``max_debate_rounds`` times, and the CRO is terminal.  There are no tools, no
retrieval loops and no free-text hand-offs: every node receives typed state and
returns a validated Pydantic model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from psx_desk.agents.fundamental import make_fundamental_analyst
from psx_desk.agents.llm import StructuredInvoker
from psx_desk.agents.research import make_bear_researcher, make_bull_researcher
from psx_desk.agents.risk import make_cro
from psx_desk.agents.technical import make_technical_analyst
from psx_desk.config import DeskConfig
from psx_desk.dataflows.ingest import DeskData, build_market_packet
from psx_desk.dataflows.timeseries import Bar, PriceHistory
from psx_desk.graph.state import (
    NODE_BEAR,
    NODE_BULL,
    NODE_CRO,
    NODE_FUNDAMENTAL,
    NODE_INGEST,
    NODE_TECHNICAL,
    DeskState,
)
from psx_desk.indicators import build_snapshot
from psx_desk.sources import normalize_symbol

logger = logging.getLogger(__name__)


def make_ingestion_node(data: DeskData) -> Callable[[DeskState], dict]:
    """Node 1: pull the real PSX packet, the bars, and the measured snapshot."""

    def ingest_node(state: DeskState) -> dict:
        symbol = normalize_symbol(state["symbol"])
        trade_date: date = state["trade_date"]
        packet = build_market_packet(symbol, trade_date, data=data)

        bars = tuple(
            Bar(
                trade_date=row["date"] if isinstance(row["date"], date) else date.fromisoformat(str(row["date"])),
                close=float(row["close"]),
                open=row.get("open"),
                high=row.get("high"),
                low=row.get("low"),
                volume=row.get("volume"),
            )
            for row in packet.history
            if row.get("close")
        )
        history = PriceHistory(
            symbol=symbol,
            bars=bars,
            source=next((ref for ref in packet.sources if ref.source_kind == "timeseries"), None),
            has_high_low=all(bar.high is not None and bar.low is not None for bar in bars[-60:]),
            quality=packet.data_quality,
        )
        snapshot = build_snapshot(history) if bars else None

        notes = [
            f"Ingested {symbol} for {trade_date.isoformat()}: {len(bars)} bars, "
            f"quality={packet.data_quality.value}."
        ]
        return {"packet": packet, "history": history, "snapshot": snapshot, "notes": notes}

    return ingest_node


def build_desk_graph(
    *,
    invoker: StructuredInvoker,
    config: DeskConfig,
    data: DeskData,
):
    """Compile the five-node desk graph."""

    from langgraph.graph import END, START, StateGraph

    workflow = StateGraph(DeskState)

    workflow.add_node(NODE_INGEST, make_ingestion_node(data))
    workflow.add_node(NODE_TECHNICAL, make_technical_analyst(invoker, config))
    workflow.add_node(NODE_FUNDAMENTAL, make_fundamental_analyst(invoker, config))
    workflow.add_node(NODE_BULL, make_bull_researcher(invoker))
    workflow.add_node(NODE_BEAR, make_bear_researcher(invoker))
    workflow.add_node(NODE_CRO, make_cro(invoker, config))

    workflow.add_edge(START, NODE_INGEST)
    workflow.add_edge(NODE_INGEST, NODE_TECHNICAL)
    workflow.add_edge(NODE_TECHNICAL, NODE_FUNDAMENTAL)
    workflow.add_edge(NODE_FUNDAMENTAL, NODE_BULL)
    workflow.add_edge(NODE_BULL, NODE_BEAR)

    def debate_or_adjudicate(state: DeskState) -> str:
        rounds = len(state.get("bear_arguments") or [])
        if rounds < config.max_debate_rounds:
            return NODE_BULL
        return NODE_CRO

    workflow.add_conditional_edges(
        NODE_BEAR,
        debate_or_adjudicate,
        {NODE_BULL: NODE_BULL, NODE_CRO: NODE_CRO},
    )
    workflow.add_edge(NODE_CRO, END)

    compiled = workflow.compile()
    logger.debug("desk graph compiled with %d nodes", len(workflow.nodes))
    return compiled


def graph_mermaid() -> str:
    """Static diagram of the pipeline (used by the CLI's ``describe`` command)."""

    return (
        "graph TD\n"
        "  START --> A[PSX Data Ingestion]\n"
        "  A --> B[PSX Technical Analyst]\n"
        "  B --> C[PSX Fundamental Analyst]\n"
        "  C --> D[Bull Researcher]\n"
        "  D --> E[Bear Researcher]\n"
        "  E -->|rounds remaining| D\n"
        "  E -->|debate closed| F[CRO Gatekeeper]\n"
        "  F --> END\n"
    )


def node_sequence(config: DeskConfig) -> list[Any]:
    """The exact node order a run will execute, for docs and tests."""

    sequence: list[Any] = [NODE_INGEST, NODE_TECHNICAL, NODE_FUNDAMENTAL]
    for _ in range(config.max_debate_rounds):
        sequence.extend([NODE_BULL, NODE_BEAR])
    sequence.append(NODE_CRO)
    return sequence
