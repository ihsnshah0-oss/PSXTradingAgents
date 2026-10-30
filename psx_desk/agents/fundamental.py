"""Node 3 — PSX Fundamental Analyst.

The model reads whatever corporate evidence the packet holds (disclosures,
reported figures where PSX published them) and argues the valuation against the
SBP policy rate and Pakistani inflation.  The desk then recomputes the three
macro ratios itself — earnings yield, equity risk premium versus the policy rate,
and the real (inflation-adjusted) earnings yield — because those are exactly the
numbers an LLM gets subtly wrong, and exactly the numbers the CRO prices.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psx_desk.agents.llm import StructuredInvoker
from psx_desk.agents.prompts import FUNDAMENTAL_SYSTEM, fundamental_user
from psx_desk.config import DeskConfig
from psx_desk.indicators import TechnicalSnapshot
from psx_desk.schemas import DataQuality, FundamentalReport, coerce_price

logger = logging.getLogger(__name__)


def derive_valuation_ratios(
    *,
    pe_ratio: float | None,
    pb_ratio: float | None,
    policy_rate_pct: float,
    cpi_yoy_pct: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Earnings yield, its spread over the policy rate, and the real yield."""

    earnings_yield = round(100.0 / pe_ratio, 4) if pe_ratio and pe_ratio > 0 else None
    premium = (
        round(earnings_yield - policy_rate_pct, 4) if earnings_yield is not None else None
    )
    real = (
        round(earnings_yield - cpi_yoy_pct, 4)
        if earnings_yield is not None and cpi_yoy_pct is not None
        else None
    )
    return earnings_yield, premium, real


def make_fundamental_analyst(
    invoker: StructuredInvoker, config: DeskConfig
) -> Callable[[dict], dict]:
    """Build the fundamental node bound to one invoker and configuration."""

    def fundamental_analyst(state: dict) -> dict:
        packet = state["packet"]
        snapshot: TechnicalSnapshot | None = state.get("snapshot")

        market_context: dict[str, Any] = {
            "close_pkr": packet.quote.close_pkr,
            "sector": packet.quote.sector,
            "free_float_mcap_pkr": packet.quote.free_float_mcap_pkr,
            "market_cap_pkr": packet.quote.market_cap_pkr,
            "adv_value_pkr_20d": packet.adv_value_pkr_20d,
            "range_52w": {"high": packet.high_52w_pkr, "low": packet.low_52w_pkr},
            "volatility_annualised_pct": snapshot.volatility_annualised_pct if snapshot else None,
            "indices": [index.model_dump(mode="json") for index in packet.indices],
            "disclosure_count": len(packet.disclosures),
            "note": (
                "PSX publishes periodic financial reports as PDFs in the disclosures list; "
                "only figures visible in the packet may be used."
            ),
        }

        report = invoker.invoke(
            FundamentalReport,
            system=FUNDAMENTAL_SYSTEM,
            user=fundamental_user(packet, market_context),
            tier="deep",
        )

        # --- identity and macro come from the packet, not the model ----- #
        report.symbol = packet.symbol
        report.as_of = packet.trade_date
        report.company_name = packet.quote.company_name
        report.sector = report.sector or packet.quote.sector
        if packet.macro is not None:
            report.macro = packet.macro

        macro = report.macro
        report.eps_pkr = coerce_price(report.eps_pkr)
        report.pe_ratio = coerce_price(report.pe_ratio)
        if report.pe_ratio is None and report.eps_pkr and report.pe_ratio != 0:
            spot = packet.quote.close_pkr
            if report.eps_pkr > 0:
                report.pe_ratio = round(spot / report.eps_pkr, 3)

        earnings_yield, premium, real = derive_valuation_ratios(
            pe_ratio=report.pe_ratio,
            pb_ratio=report.pb_ratio,
            policy_rate_pct=macro.sbp_policy_rate_pct,
            cpi_yoy_pct=macro.cpi_yoy_pct,
        )
        report.earnings_yield_pct = earnings_yield
        report.equity_risk_premium_pct = premium
        report.real_earnings_yield_pct = real

        # A model that claims "verified" without any disclosure trail is not
        # verified; the packet's own quality bounds the claim.
        if not packet.disclosures and report.data_quality is DataQuality.VERIFIED:
            report.data_quality = DataQuality.PARTIAL
            report.narrative += (
                " No corporate results were found in the PSX disclosures for this "
                "session, so the fundamental score is built from price, float and "
                "sector context only."
            )
        if packet.data_quality is DataQuality.PARTIAL and report.data_quality is DataQuality.VERIFIED:
            report.data_quality = DataQuality.PARTIAL

        report.sources = list(packet.sources)
        return {"fundamental": report}

    return fundamental_analyst
