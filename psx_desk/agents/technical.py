"""Node 2 — PSX Technical Analyst.

The model interprets; the desk measures.  Every numeric field in the returned
:class:`~psx_desk.schemas.TechnicalReport` is overwritten with the value computed
from PSX bars, and the circuit block is copied verbatim from the packet's
:class:`~psx_desk.schemas.CircuitState`.  The model is only trusted with what it
is actually good at: which signals matter, the call, the confidence, and the
paragraph a portfolio manager will read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psx_desk.agents.llm import StructuredInvoker
from psx_desk.agents.prompts import TECHNICAL_SYSTEM, technical_user
from psx_desk.config import DeskConfig
from psx_desk.indicators import TechnicalSnapshot
from psx_desk.market_rules import board_is_tradeable
from psx_desk.schemas import DataQuality, LockState, TechnicalReport, Trend

logger = logging.getLogger(__name__)


def snapshot_payload(snapshot: TechnicalSnapshot | None) -> dict[str, Any]:
    """The measured indicators, as handed to the model."""

    if snapshot is None:
        return {"available": False, "reason": "PSX published no usable daily bars for this scrip."}
    return {
        "available": True,
        "trend": snapshot.trend,
        "momentum_score": snapshot.momentum_score,
        "sma_20": snapshot.sma_20,
        "sma_50": snapshot.sma_50,
        "sma_200": snapshot.sma_200,
        "rsi_14": snapshot.rsi_14,
        "atr_14_pkr": snapshot.atr_14,
        "volatility_annualised_pct": snapshot.volatility_annualised_pct,
        "roc_20_pct": snapshot.roc_20_pct,
        "volume_zscore_20": snapshot.volume_zscore_20,
        "adv_shares_20d": snapshot.adv_shares_20,
        "adv_value_pkr_20d": snapshot.adv_value_20,
        "support_pkr": snapshot.support,
        "resistance_pkr": snapshot.resistance,
        "high_52w_pkr": snapshot.high_52w,
        "low_52w_pkr": snapshot.low_52w,
        "has_high_low": snapshot.has_high_low,
        "notes": list(snapshot.notes),
    }


def resolve_tradability(packet, config: DeskConfig) -> str:
    """Execution state after the circuit and liquidity checks."""

    if packet.circuit.locked is LockState.UPPER:
        return "locked_upper"
    if packet.circuit.locked is LockState.LOWER:
        return "locked_lower"
    adv_value = packet.adv_value_pkr_20d
    if adv_value is not None and adv_value < config.risk.min_adv_value_pkr:
        return "illiquid"
    if not board_is_tradeable(packet.quote.board):
        return "illiquid"
    return "open"


def make_technical_analyst(
    invoker: StructuredInvoker, config: DeskConfig
) -> Callable[[dict], dict]:
    """Build the technical node bound to one invoker and configuration."""

    def technical_analyst(state: dict) -> dict:
        packet = state["packet"]
        snapshot: TechnicalSnapshot | None = state.get("snapshot")

        report = invoker.invoke(
            TechnicalReport,
            system=TECHNICAL_SYSTEM,
            user=technical_user(packet, snapshot_payload(snapshot)),
            tier="quick",
        )

        # --- the desk's numbers win ------------------------------------- #
        report.symbol = packet.symbol
        report.as_of = packet.trade_date
        report.circuit = packet.circuit
        report.volume_zscore_20 = snapshot.volume_zscore_20 if snapshot else None
        if snapshot is not None:
            report.sma_20 = snapshot.sma_20
            report.sma_50 = snapshot.sma_50
            report.sma_200 = snapshot.sma_200
            report.rsi_14 = snapshot.rsi_14
            report.atr_14_pkr = snapshot.atr_14
            report.volatility_annualised_pct = snapshot.volatility_annualised_pct
            report.support_pkr = snapshot.support
            report.resistance_pkr = snapshot.resistance
            report.trend = Trend(snapshot.trend)
            report.momentum_score = snapshot.momentum_score
        else:
            report.data_quality = DataQuality.PARTIAL
            report.signals = [
                signal
                for signal in report.signals
                if signal.verification.value != "derived"
            ]
            report.narrative += (
                " Note: PSX served no daily price series for this scrip, so no indicator "
                "could be computed; the view rests on the session print alone."
            )

        report.tradability = resolve_tradability(packet, config)  # type: ignore[assignment]

        if packet.data_quality is not DataQuality.VERIFIED and report.data_quality is DataQuality.VERIFIED:
            report.data_quality = DataQuality.PARTIAL
        report.sources = list(packet.sources)
        return {"technical": report}

    return technical_analyst
