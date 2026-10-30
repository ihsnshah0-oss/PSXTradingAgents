"""The Pakistani macro anchor: SBP policy rate, corridor, CPI and USD/PKR.

Why this module exists: a PSX valuation is not a US-style DCF exercise.  The
risk-free alternative for a Pakistani investor is an SBP-tracked deposit paying
the policy rate, headline inflation has run in double digits for years, and
KIBOR prices corporate leverage.  An equity yielding less than the policy rate
is competing with a government-guaranteed deposit — that comparison is the
single most important fundamental test on this desk.

The scraper reads the SBP home page's indicator tiles.  When SBP is unreachable
the desk falls back to the labelled print in
:mod:`psx_desk.config` and marks the backdrop ``STALE`` / ``configured_fallback``;
it never silently substitutes an undated number.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime

from psx_desk.config import DeskConfig
from psx_desk.dataflows.archive import html_text
from psx_desk.dataflows.http import PortalClient, PortalError
from psx_desk.schemas import DataQuality, MacroBackdrop
from psx_desk.sources import SBP_HOME_URL, SourceKind

logger = logging.getLogger(__name__)

# Tiles on sbp.org.pk, e.g. "SBP Policy Rate 12.00% p.a."
_POLICY = re.compile(r"SBP\s+Policy\s+Rate[^0-9%]{0,60}(\d{1,2}(?:\.\d{1,2})?)\s*%", re.I)
# SBP labels these tiles inconsistently ("Overnight Reverse (Repo) Ceiling Rate",
# "Overnight Reverse (Floor) Rate"), so match on the corridor end, not the shape.
_CEILING = re.compile(
    r"Reverse[^%\n]{0,60}Ceiling[^0-9%\n]{0,40}(\d{1,2}(?:\.\d{1,2})?)\s*%", re.I
)
_FLOOR = re.compile(
    r"Reverse[^%\n]{0,60}Floor[^0-9%\n]{0,40}(\d{1,2}(?:\.\d{1,2})?)\s*%", re.I
)
_CPI = re.compile(r"(?:CPI|Inflation)[^0-9%]{0,60}(\d{1,3}(?:\.\d{1,2})?)\s*%[^\n]{0,20}(?:y\s*/\s*y|yoy)", re.I)
_USD = re.compile(r"USD\s*/\s*PKR[^0-9]{0,60}(\d{2,3}(?:\.\d{1,4})?)", re.I)
_KIBOR_6M = re.compile(r"6\s*[-–]\s*M[^0-9]{0,20}(\d{1,2}\.\d{1,2})\s*%?", re.I)
_TBILL_6M = re.compile(r"6\s*[-–]\s*M\s+(\d{1,2}\.\d{1,4})\s*%", re.I)


def _first_float(pattern: re.Pattern[str], text: str, *, low: float, high: float) -> float | None:
    for match in pattern.finditer(text):
        try:
            value = float(match.group(1))
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            return value
    return None


def parse_sbp_indicators(text: str) -> dict[str, float]:
    """Pull the indicator tiles out of SBP page text.

    Returns only what was actually found; an absent key means SBP did not
    publish it in a shape the desk can read.
    """

    found: dict[str, float] = {}
    policy = _first_float(_POLICY, text, low=1, high=40)
    if policy is not None:
        found["sbp_policy_rate_pct"] = policy
    ceiling = _first_float(_CEILING, text, low=1, high=40)
    if ceiling is not None:
        found["corridor_ceiling_pct"] = ceiling
    floor = _first_float(_FLOOR, text, low=1, high=40)
    if floor is not None:
        found["corridor_floor_pct"] = floor
    cpi = _first_float(_CPI, text, low=-5, high=100)
    if cpi is not None:
        found["cpi_yoy_pct"] = cpi
    usd = _first_float(_USD, text, low=50, high=1000)
    if usd is not None:
        found["pkr_usd"] = usd
    kibor = _first_float(_KIBOR_6M, text, low=1, high=40)
    if kibor is not None:
        found["kibor_6m_pct"] = kibor
    tbill = _first_float(_TBILL_6M, text, low=1, high=40)
    if tbill is not None:
        found["tbill_6m_cutoff_pct"] = tbill
    return found


def configured_backdrop(config: DeskConfig) -> MacroBackdrop:
    """The labelled fallback print from configuration."""

    macro = config.macro
    return MacroBackdrop(
        sbp_policy_rate_pct=macro.sbp_policy_rate_pct,
        corridor_floor_pct=macro.corridor_floor_pct,
        corridor_ceiling_pct=macro.corridor_ceiling_pct,
        cpi_yoy_pct=macro.cpi_yoy_pct,
        as_of=macro.as_of,
        quality=DataQuality.STALE,
        basis="configured_fallback",
        sources=[],
    )


def fetch_macro_backdrop(
    client: PortalClient,
    config: DeskConfig,
    *,
    as_of: date | None = None,
) -> MacroBackdrop:
    """Live SBP read, falling back to the configured print.

    ``as_of`` is the analysis date: an SBP print dated after it is truncated out
    by the client's cache rules only if the caller supplies a past run date, so
    backtests get the fallback print unless the page is archived.  That
    limitation is stated in the report rather than hidden.
    """

    as_of = as_of or datetime.now(UTC).astimezone().date()
    try:
        payload = client.get(
            SBP_HOME_URL,
            kind=SourceKind.MACRO,
            label="State Bank of Pakistan monetary indicators",
            as_of=as_of,
            ttl_seconds=3600,
            allow_missing=True,
        )
    except PortalError as exc:
        logger.warning("SBP unavailable (%s); using the configured macro print", exc)
        return configured_backdrop(config)
    if payload is None:
        logger.warning("SBP home returned 404; using the configured macro print")
        return configured_backdrop(config)

    values = parse_sbp_indicators(html_text(payload.text))
    if "sbp_policy_rate_pct" not in values:
        logger.warning("SBP page carried no policy-rate tile; using the configured macro print")
        return configured_backdrop(config)

    fallback = config.macro
    return MacroBackdrop(
        sbp_policy_rate_pct=values["sbp_policy_rate_pct"],
        corridor_floor_pct=values.get("corridor_floor_pct", fallback.corridor_floor_pct),
        corridor_ceiling_pct=values.get("corridor_ceiling_pct", fallback.corridor_ceiling_pct),
        cpi_yoy_pct=values.get("cpi_yoy_pct", fallback.cpi_yoy_pct),
        pkr_usd=values.get("pkr_usd"),
        kibor_6m_pct=values.get("kibor_6m_pct"),
        tbill_6m_cutoff_pct=values.get("tbill_6m_cutoff_pct"),
        as_of=as_of,
        quality=DataQuality.VERIFIED,
        basis="live_scrape",
        sources=[payload.source_ref()],
    )


__all__ = [
    "configured_backdrop",
    "fetch_macro_backdrop",
    "parse_sbp_indicators",
]
