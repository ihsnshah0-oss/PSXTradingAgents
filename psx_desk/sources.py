"""Canonical PSX / SBP endpoints — the desk's entire data universe.

There is exactly one place in this codebase where a URL is written down.  Every
scraper imports from here, and :func:`assert_source_allowed` refuses any URL
whose host is not the Pakistan Stock Exchange or the State Bank of Pakistan.

Verified surface (October 2026)
-------------------------------
Data Portal (``dps.psx.com.pk``)
  * ``/timeseries/eod/{SYMBOL}``   JSON, descending daily OHLCV for a scrip or index
  * ``/timeseries/int/{SYMBOL}``   JSON, intraday ticks for the current session
  * ``/symbols``                   JSON, every listed symbol with name + sector
  * ``/market-watch``              HTML, the full board (change, volume, value, caps)
  * ``/download/{dataset}/{YYYY-MM-DD}.{ext}``  date-keyed daily files (see
    :data:`PSX_DATASETS`), e.g. ``/download/symbol_price/2026-10-29.zip``

Corporate site (``www.psx.com.pk``)
  * ``/psx/resources-and-tools/Defaulting-Companies-Profile-Portal``  defaulters
  * ``/psx/resources-and-tools/circuit-breakers``                    rules in force
  * ``/psx/announcement/companies``                                  PUCARS notices
  * ``/market-summary/``                                             closing summary
  * ``/psx/regulations/enforcement-actions``                         regulatory actions

Macro (``www.sbp.org.pk``)
  * ``/`` (home) carries the live policy rate, corridor and CPI tiles.

The ``data_dump.csv`` endpoint the desk is asked to consume does not exist as a
single stable public file, so :func:`data_dump_candidates` lists every URL that
may serve a full-market dump and :func:`psx_desk.dataflows.portal.fetch_market_dump`
probes them in order, accepting only payloads that actually parse as a market
table.  Nothing is ever synthesised when a probe fails: the caller gets an
explicit :class:`~psx_desk.dataflows.portal.MarketDumpUnavailable`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Hosts
# --------------------------------------------------------------------------- #

PSX_WEB_HOST = "www.psx.com.pk"
PSX_PORTAL_HOST = "dps.psx.com.pk"
SBP_HOST = "www.sbp.org.pk"

PSX_WEB = f"https://{PSX_WEB_HOST}"
PSX_PORTAL = f"https://{PSX_PORTAL_HOST}"
SBP_WEB = f"https://{SBP_HOST}"

ALLOWED_HOSTS = frozenset({
    PSX_WEB_HOST,
    PSX_PORTAL_HOST,
    "psx.com.pk",
    "dps.psx.com.pk",
    "sbp.org.pk",
    SBP_HOST,
})


class SourceKind(StrEnum):
    """What a payload is, so downstream code knows how to read it."""

    MARKET_DUMP = "market_dump"
    TIMESERIES = "timeseries"
    INTRADAY = "intraday"
    SYMBOL_DIRECTORY = "symbol_directory"
    DAILY_DOWNLOAD = "daily_download"
    DISCLOSURE = "disclosure"
    DEFAULTER_LIST = "defaulter_list"
    CIRCUIT_RULES = "circuit_rules"
    MACRO = "macro"


def assert_source_allowed(url: str) -> str:
    """Return ``url`` if its host is a PSX/SBP host, else raise ``ValueError``.

    This is the guard that makes "all agents derive context exclusively from
    PSX" an enforced property rather than a prompt instruction.
    """

    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            f"source not permitted: {url!r} (host {host!r}). "
            f"Allowed hosts: {', '.join(sorted(ALLOWED_HOSTS))}"
        )
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme in {url!r}")
    return url


# --------------------------------------------------------------------------- #
# Daily download registry — real dataset slugs on dps.psx.com.pk/download/
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Dataset:
    """One date-keyed file on the PSX download portal."""

    key: str
    label: str
    exts: tuple[str, ...]
    kind: SourceKind = SourceKind.DAILY_DOWNLOAD
    section: str = "Market Summary"

    def url(self, trade_date: date, ext: str | None = None) -> str:
        chosen = ext or self.exts[0]
        return f"{PSX_PORTAL}/download/{self.key}/{trade_date.isoformat()}.{chosen}"

    @property
    def primary_ext(self) -> str:
        return self.exts[0]


#: Slugs verified against the portal's Downloads page.  ``quote`` (the full
#: daily quotations report) is the closest thing PSX publishes to a
#: whole-market "data dump"; ``symbol_price`` carries the Exchange's own
#: upper/lower circuit caps for the session, which the desk prefers over any
#: computed band.
PSX_DATASETS: dict[str, Dataset] = {
    d.key: d
    for d in (
        Dataset("quote", "Daily Quotations", ("pdf",), SourceKind.MARKET_DUMP, "Daily Quotations"),
        Dataset(
            "symbol_price",
            "Symbol Price (Upper/Lower)",
            ("zip",),
            SourceKind.DAILY_DOWNLOAD,
            "Market Summary",
        ),
        Dataset("symbol_name", "Symbols Short/Long Name", ("zip",), SourceKind.SYMBOL_DIRECTORY),
        Dataset("mkt_summary", "Market Summary (Closing)", ("Z",), SourceKind.DAILY_DOWNLOAD),
        Dataset("closing_rates", "Closing Rate Summary", ("pdf",), SourceKind.DAILY_DOWNLOAD),
        Dataset("announce", "Daily Announcements (PUCARS)", ("pdf",), SourceKind.DISCLOSURE),
        Dataset("omts", "Off Market Transaction Summary", ("csv",), SourceKind.DAILY_DOWNLOAD),
        Dataset("dvf_trade", "Bai Muajjal Volume", ("csv",), SourceKind.DAILY_DOWNLOAD),
        Dataset("sif_opn_int", "SIF Open Interest", ("csv",), SourceKind.DAILY_DOWNLOAD),
        Dataset("dfc_nbs", "Net Blank Sale Position (DFC)", ("csv",), SourceKind.DAILY_DOWNLOAD),
        Dataset("reval_rates_gis", "GIS Revaluation Rates", ("csv",), SourceKind.DAILY_DOWNLOAD),
        Dataset("indhist", "Index Constituents", ("xls",), SourceKind.DAILY_DOWNLOAD, "Other"),
    )
}

#: The market-structure files the ingestion node pulls by default, in priority
#: order.  ``symbol_price`` first: a session's real caps beat a computed band.
DEFAULT_DUMP_DATASETS: tuple[str, ...] = ("symbol_price", "symbol_name", "mkt_summary")


def daily_dataset_url(key: str, trade_date: date, ext: str | None = None) -> str:
    """URL for a registered dataset, validated against the allow-list."""

    try:
        dataset = PSX_DATASETS[key]
    except KeyError:
        raise KeyError(
            f"unknown PSX dataset {key!r}; known: {', '.join(sorted(PSX_DATASETS))}"
        ) from None
    return assert_source_allowed(dataset.url(trade_date, ext))


# --------------------------------------------------------------------------- #
# Whole-market dump candidates
# --------------------------------------------------------------------------- #


def data_dump_candidates(trade_date: date) -> tuple[str, ...]:
    """Every URL that might serve a whole-market table for ``trade_date``.

    Ordered cheapest-and-most-authoritative first.  ``fetch_market_dump`` tries
    each, sniffs the payload, and only accepts one that parses into rows with a
    symbol and a price.  The generated URLs stay pinned to the PSX allow-list.
    """

    urls = [
        # The canonical exchange download pattern: /download/{dataset}/{date}.{ext}
        daily_dataset_url("symbol_price", trade_date, "zip"),
        daily_dataset_url("mkt_summary", trade_date, "Z"),
        daily_dataset_url("quote", trade_date, "pdf"),
        # Legacy/hand-published dumps that some desks still point at.
        f"{PSX_WEB}/download/history/data_dump.csv",
        f"{PSX_PORTAL}/download/data_dump.csv",
        f"{PSX_WEB}/download/history/data_dump_{trade_date:%Y%m%d}.csv",
        # Live board fallbacks (always current, not date-keyed).
        f"{PSX_PORTAL}/market-watch",
    ]
    return tuple(assert_source_allowed(u) for u in urls)


# --------------------------------------------------------------------------- #
# Fixed endpoints
# --------------------------------------------------------------------------- #


def eod_timeseries_url(symbol: str) -> str:
    return assert_source_allowed(f"{PSX_PORTAL}/timeseries/eod/{normalize_symbol(symbol)}")


def intraday_url(symbol: str) -> str:
    return assert_source_allowed(f"{PSX_PORTAL}/timeseries/int/{normalize_symbol(symbol)}")


SYMBOL_DIRECTORY_URL = f"{PSX_PORTAL}/symbols"
MARKET_WATCH_URL = f"{PSX_PORTAL}/market-watch"
MARKET_SUMMARY_URL = f"{PSX_WEB}/market-summary/"
DEFAULTER_PORTAL_URL = (
    f"{PSX_WEB}/psx/resources-and-tools/Defaulting-Companies-Profile-Portal"
)
CIRCUIT_BREAKERS_URL = f"{PSX_WEB}/psx/resources-and-tools/circuit-breakers"
PUCARS_URL = f"{PSX_WEB}/psx/announcement/companies"
ENFORCEMENT_URL = f"{PSX_WEB}/psx/regulations/enforcement-actions"
SBP_HOME_URL = f"{SBP_WEB}/"
SBP_MONETARY_POLICY_URL = f"{SBP_WEB}/m_policy/"

for _url in (
    SYMBOL_DIRECTORY_URL,
    MARKET_WATCH_URL,
    MARKET_SUMMARY_URL,
    DEFAULTER_PORTAL_URL,
    CIRCUIT_BREAKERS_URL,
    PUCARS_URL,
    ENFORCEMENT_URL,
    SBP_HOME_URL,
    SBP_MONETARY_POLICY_URL,
):
    assert_source_allowed(_url)


# --------------------------------------------------------------------------- #
# Symbol normalisation
# --------------------------------------------------------------------------- #

# A PSX scrip code: letters/digits, occasionally with a hyphen for preferred
# shares or a right (e.g. "AKBLPS", "NBPXD").  We accept a leading exchange
# prefix or a Yahoo-style suffix and strip both, so a caller pasting "PSX:SYS"
# or "SYS.KA" still gets a valid PSX code.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,11}$")
_STRIP_PREFIXES = ("PSX:", "PSX/", "KSE:", "KARACHI:")
_STRIP_SUFFIXES = (".KA", ".PSX", ".PK")

#: Sentinels PSX uses for a non-individual market line.
INDEX_SYMBOLS = frozenset({
    "KSE100", "KSE30", "KSEALL", "ALLSHR", "KMI30", "KMIALL", "KMIALLSHR",
    "BKTI", "OGTI", "NBPPGI", "PSXDIV20", "UPP9", "NITPGI", "MZNPI",
})


def normalize_symbol(raw: str) -> str:
    """Normalise a user-supplied ticker into a PSX scrip code.

    ``"sys"``, ``"PSX:SYS"`` and ``" SYS.KA "`` all become ``"SYS"``.  Anything
    that is not a plausible PSX code raises ``ValueError`` — the desk would
    rather refuse a symbol than quote a price for the wrong company.
    """

    if not isinstance(raw, str):
        raise ValueError(f"symbol must be a string, got {type(raw).__name__}")
    sym = raw.strip().upper()
    if not sym:
        raise ValueError("symbol must not be empty")
    for prefix in _STRIP_PREFIXES:
        if sym.startswith(prefix):
            sym = sym[len(prefix):]
    for suffix in _STRIP_SUFFIXES:
        if sym.endswith(suffix):
            sym = sym[: -len(suffix)]
    sym = sym.strip()
    if not _SYMBOL_RE.match(sym):
        raise ValueError(
            f"{raw!r} is not a valid PSX scrip code; expected e.g. SYS, ENGRO, LUCK, HUBC"
        )
    return sym


def is_index(symbol: str) -> bool:
    return normalize_symbol(symbol) in INDEX_SYMBOLS


# --------------------------------------------------------------------------- #
# Defaulter / non-compliant segment
# --------------------------------------------------------------------------- #

#: Company-name fragments that appear only in PSX's non-compliant/defaulter
#: sections and never as a live tradeable scrip header.  Used to reject a
#: scraped list that has fallen through to a generic page.
DEFAULTER_SECTION_MARKERS = (
    "current non-compliant segment",
    "previously quoted in defaulters' segment",
    "previously quoted in defaulters segment",
    "defaulters' segment",
    "defaulter",
)

#: Trading-board labels the portal uses; anything but ``REG`` is a side board.
BOARD_TYPES = ("REG", "FUT", "CSF", "ODL", "SIF", "BNC")
