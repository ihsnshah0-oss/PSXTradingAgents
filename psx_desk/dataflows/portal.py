"""Whole-market ingestion from the PSX Data Portal download endpoints.

:func:`scrape_market_dump` is the single entry point the desk uses to turn a
session into typed rows.  It is deliberately narrow and fast:

* it probes a short, ordered list of real endpoints
  (``/download/symbol_price/{date}.zip``, ``/download/mkt_summary/{date}.Z``,
  ``/download/symbol_name/{date}.zip``, then the live ``/market-watch`` board)
  and accepts a payload **only** if it parses into a table with a symbol column
  and a price column;
* it streams each archive member straight into the delimited reader and stops
  at the first payload that yields a usable board, so a normal run costs one to
  three HTTP requests rather than one per scrip;
* it left-joins the exchange's own upper/lower circuit caps and company names
  onto the price board by symbol;
* it refuses to fabricate: any symbol or field PSX did not publish is left
  ``None`` and listed in :attr:`MarketDump.missing_fields`, and a session with
  no usable payload raises :class:`MarketDumpUnavailable` carrying every URL it
  tried and why each was rejected.

The result is a frozen, dict-indexed board — O(1) symbol lookup, no pandas
required unless the caller asks for a frame.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from psx_desk.config import DeskConfig, load_config
from psx_desk.dataflows.archive import (
    Table,
    html_tables,
    parse_table,
    table_as_records,
    text_of,
    zip_members,
)
from psx_desk.dataflows.http import (
    Payload,
    PayloadRejected,
    PortalClient,
    PortalError,
    require_csv_like,
)
from psx_desk.schemas import SourceRef, coerce_pct, coerce_price
from psx_desk.sources import (
    DEFAULT_DUMP_DATASETS,
    MARKET_WATCH_URL,
    PSX_DATASETS,
    SourceKind,
    daily_dataset_url,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

MIN_BOARD_ROWS = 20

#: Datasets that carry reference data (caps, names) rather than a price board.
METADATA_DATASETS: tuple[str, ...] = ("symbol_price", "symbol_name")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")


class MarketDumpUnavailable(PortalError):
    """No PSX payload for the session could be turned into a board.

    ``rejected`` holds ``"<url> :: <reason>"`` for every probe, which is what
    the run report shows instead of a silent empty table.
    """

    def __init__(self, trade_date: date, rejected: Sequence[str]) -> None:
        detail = "\n  ".join(rejected) if rejected else "(no probes recorded)"
        super().__init__(
            f"PSX published no usable market board for {trade_date.isoformat()}:\n  {detail}"
        )
        self.trade_date = trade_date
        self.rejected = list(rejected)


# --------------------------------------------------------------------------- #
# Typed rows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MarketRow:
    """One scrip's session row, every value straight from a PSX payload."""

    symbol: str
    close: float
    company_name: str | None = None
    sector: str | None = None
    board: str = "REG"
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    change: float | None = None
    pct_change: float | None = None
    volume: float | None = None
    value: float | None = None
    trades: int | None = None
    upper_cap: float | None = None
    lower_cap: float | None = None
    free_float_shares: float | None = None
    free_float_mcap: float | None = None
    market_cap: float | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "sector": self.sector,
            "board": self.board,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "prev_close": self.prev_close,
            "change": self.change,
            "pct_change": self.pct_change,
            "volume": self.volume,
            "value": self.value,
            "trades": self.trades,
            "upper_cap": self.upper_cap,
            "lower_cap": self.lower_cap,
            "free_float_shares": self.free_float_shares,
            "free_float_mcap": self.free_float_mcap,
            "market_cap": self.market_cap,
        }


@dataclass(frozen=True, slots=True)
class MarketDump:
    """A session's board, keyed by PSX scrip code."""

    trade_date: date
    rows: Mapping[str, MarketRow]
    sources: tuple[SourceRef, ...] = ()
    datasets_used: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.rows)

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and normalize_symbol(symbol) in self.rows

    def get(self, symbol: str) -> MarketRow | None:
        return self.rows.get(normalize_symbol(symbol))

    def filter(self, symbols: Iterable[str]) -> dict[str, MarketRow]:
        return {s: self.rows[s] for s in (normalize_symbol(x) for x in symbols) if s in self.rows}

    def top_by_value(self, limit: int = 20) -> list[MarketRow]:
        ranked = sorted(self.rows.values(), key=lambda r: r.value or 0.0, reverse=True)
        return ranked[:limit]

    def adv_proxy(self, symbol: str) -> float | None:
        """Session turnover, used only when a 20-day history is unavailable."""

        row = self.get(symbol)
        return row.value if row else None

    def to_frame(self):  # pragma: no cover - convenience for notebooks
        """Materialise the board as a pandas DataFrame (index: symbol)."""

        import pandas as pd

        frame = pd.DataFrame([row.as_dict() for row in self.rows.values()])
        return frame.set_index("symbol") if not frame.empty else frame


# --------------------------------------------------------------------------- #
# Payload parsing
# --------------------------------------------------------------------------- #


def _rows_from_table(table: Table, wanted: set[str] | None) -> dict[str, MarketRow]:
    """Convert a canonical :class:`Table` into rows, in one pass."""

    symbol_column = table.find_symbol_column()
    if symbol_column is None:
        raise PayloadRejected("no symbol column in the parsed table")
    if "close" not in table.headers and "prev_close" not in table.headers:
        raise PayloadRejected("no price column in the parsed table")

    rows: dict[str, MarketRow] = {}
    for record in table.rows:
        raw_symbol = (record.get(symbol_column) or "").strip().upper()
        if not _SYMBOL_RE.match(raw_symbol):
            continue
        if wanted is not None and raw_symbol not in wanted:
            continue
        close = coerce_price(record.get("close")) or coerce_price(record.get("prev_close"))
        if close is None or close <= 0:
            continue
        prev_close = coerce_price(record.get("prev_close"))
        change = coerce_price(record.get("change"))
        if prev_close is None and change is not None and close - change > 0:
            # The live board publishes the day's change, not the previous close;
            # recovering it from PSX's own two numbers keeps the circuit band
            # computable instead of leaving it unknown.
            prev_close = round(close - change, 4)
        if change is None and prev_close:
            change = round(close - prev_close, 4)
        pct = coerce_pct(record.get("pct_change"))
        if pct is None and prev_close:
            pct = round(100.0 * (close - prev_close) / prev_close, 4)
        volume = coerce_price(record.get("volume"))
        value = coerce_price(record.get("value"))
        trades_raw = coerce_price(record.get("trades"))
        rows[raw_symbol] = MarketRow(
            symbol=raw_symbol,
            close=close,
            company_name=(record.get("company_name") or "").strip() or None,
            sector=(record.get("sector") or "").strip() or None,
            board=(record.get("board") or "REG").strip().upper() or "REG",
            open=coerce_price(record.get("open")),
            high=coerce_price(record.get("high")),
            low=coerce_price(record.get("low")),
            prev_close=prev_close,
            change=change,
            pct_change=pct,
            volume=volume,
            value=value if value is not None else (round(volume * close, 2) if volume else None),
            trades=int(trades_raw) if trades_raw is not None else None,
            upper_cap=coerce_price(record.get("upper_cap")),
            lower_cap=coerce_price(record.get("lower_cap")),
            free_float_shares=coerce_price(record.get("free_float")),
            free_float_mcap=coerce_price(record.get("free_float_mcap")),
            market_cap=coerce_price(record.get("market_cap")),
        )
    if len(rows) < 1:
        raise PayloadRejected("table parsed but contained no valid PSX scrips")
    return rows


def _payload_to_table(payload: Payload) -> Table:
    """Decode any accepted payload shape into a canonical table."""

    content = payload.content
    if content[:4] == b"PK\x03\x04":
        name, blob = _pick_member(payload)
        text = text_of(blob, url=f"{payload.url}!{name}")
        return parse_table(text)
    text = text_of(content, url=payload.url)
    if text.lstrip().startswith("<"):
        tables = html_tables(text)
        for candidate in sorted(tables, key=len, reverse=True):
            try:
                return table_as_records(candidate)
            except PayloadRejected:
                continue
        raise PayloadRejected("HTML page had no usable data table")
    return parse_table(text)


def _pick_member(payload: Payload) -> tuple[str, bytes]:
    members = zip_members(payload.content)
    candidates = [
        (name, blob)
        for name, blob in members.items()
        if name.lower().endswith((".csv", ".txt", ".lst")) and len(blob) > 40
    ]
    if not candidates:
        # Some PSX archives hold a single extension-less file.
        candidates = sorted(members.items(), key=lambda kv: -len(kv[1]))[:1]
    if not candidates:
        raise PayloadRejected("archive had no parsable member")
    return max(candidates, key=lambda kv: len(kv[1]))


def _validate_board(payload: Payload) -> bool:
    """Cheap gate before the expensive parse: does this look like a board?"""

    if payload.content[:4] == b"PK\x03\x04":
        return True
    text = payload.content.decode("utf-8", errors="replace").lower()
    return "symbol" in text and len(text) > 200


def _parse_dump_dataset(
    dataset_key: str, payload: Payload, wanted: set[str] | None
) -> dict[str, MarketRow]:
    table = _payload_to_table(payload)
    rows = _rows_from_table(table, wanted)
    if len(rows) < MIN_BOARD_ROWS and payload.url.endswith((".csv", ".Z", ".zip")):
        # A real closing file covers the whole board; a handful of rows means
        # we parsed something else (an index summary, a sector page).
        raise PayloadRejected(
            f"{dataset_key}: only {len(rows)} scrips parsed, expected the whole board"
        )
    return rows


# --------------------------------------------------------------------------- #
# The scraper
# --------------------------------------------------------------------------- #


def scrape_market_dump(
    trade_date: date,
    *,
    symbols: Sequence[str] | None = None,
    datasets: Sequence[str] = DEFAULT_DUMP_DATASETS,
    client: PortalClient | None = None,
    config: DeskConfig | None = None,
    allow_live_board: bool = True,
    ttl_seconds: int | None = None,
) -> MarketDump:
    """Scrape a PSX session's board from the Data Portal download endpoints.

    Args:
        trade_date: Session to load.  Date-keyed downloads are immutable, so a
            past date is cached permanently.
        symbols: Optional allow-list; parsing skips every other scrip, which
            keeps the pass O(wanted) in memory instead of O(board).
        datasets: Dataset keys to try.  ``symbol_price`` (exchange circuit caps)
            and ``symbol_name`` (company names) are merged as metadata; any
            other key is treated as a candidate price board and the first one
            that parses wins.
        client: Reuse a :class:`~psx_desk.dataflows.http.PortalClient` to share
            its cache, throttle and keep-alive pool across nodes.
        config: Desk configuration when no client is supplied.
        allow_live_board: Fall back to the live ``/market-watch`` table when no
            date-keyed file is published (holidays, T+0 intraday runs).
        ttl_seconds: Cache freshness for the live board.  Date-keyed files are
            always cached permanently.

    Returns:
        A :class:`MarketDump` whose rows carry the union of everything the
        accepted payloads published, plus provenance for each.

    Raises:
        MarketDumpUnavailable: No probe produced a board.  The exception lists
            every URL tried and the reason it was rejected — the desk reports
            that verbatim rather than inventing prices.
    """

    wanted = {normalize_symbol(s) for s in symbols} if symbols else None
    client = client or PortalClient(config or load_config())
    rejected: list[str] = []
    sources: list[SourceRef] = []
    used: list[str] = []
    missing: set[str] = set()

    metadata: dict[str, dict[str, MarketRow]] = {}
    board: dict[str, MarketRow] = {}

    # -- 1. date-keyed datasets, in priority order -------------------------- #
    for key in datasets:
        if key not in PSX_DATASETS:
            rejected.append(f"{key} :: not a registered PSX dataset")
            continue

        if key in METADATA_DATASETS:
            rows, payload = _metadata_dataset(client, key, trade_date, wanted, rejected)
            if rows and payload is not None:
                metadata[key] = rows
                sources.append(payload.source_ref())
                used.append(key)
            continue

        if board:
            continue  # a good price board already won; do not spend requests

        for ext in PSX_DATASETS[key].exts:
            url = daily_dataset_url(key, trade_date, ext)
            try:
                payload = client.get(
                    url,
                    kind=SourceKind.DAILY_DOWNLOAD,
                    label=f"PSX {PSX_DATASETS[key].label} {trade_date.isoformat()}",
                    as_of=trade_date,
                    ttl_seconds=None,
                    allow_missing=True,
                )
            except PortalError as exc:
                rejected.append(f"{url} :: {exc}")
                continue
            if payload is None:
                rejected.append(f"{url} :: 404 (not published)")
                continue
            if not _validate_board(payload):
                rejected.append(f"{url} :: payload does not look like a market board")
                continue
            try:
                board = _parse_dump_dataset(key, payload, wanted)
            except (PayloadRejected, ValueError) as exc:
                rejected.append(f"{url} :: {exc}")
                continue
            sources.append(payload.source_ref())
            used.append(key)
            break

    # -- 2. live board fallback -------------------------------------------- #
    if not board and allow_live_board:
        try:
            payload = client.get(
                MARKET_WATCH_URL,
                kind=SourceKind.MARKET_DUMP,
                label="PSX live market-watch board",
                as_of=trade_date,
                ttl_seconds=ttl_seconds if ttl_seconds is not None else 120,
                allow_missing=True,
            )
        except PortalError as exc:
            payload = None
            rejected.append(f"{MARKET_WATCH_URL} :: {exc}")
        else:
            if payload is None:
                rejected.append(f"{MARKET_WATCH_URL} :: 404 (live board unavailable)")
        if payload is not None:
            try:
                board = _rows_from_table(_payload_to_table(payload), wanted)
            except (PayloadRejected, ValueError) as exc:
                rejected.append(f"{MARKET_WATCH_URL} :: {exc}")
            else:
                sources.append(payload.source_ref())
                used.append("market_watch")

    if not board:
        raise MarketDumpUnavailable(trade_date, rejected)

    # -- 3. merge the exchange's own caps and names ------------------------- #
    for key in METADATA_DATASETS:
        rows = metadata.get(key)
        if rows is None:
            # The dataset may not have been requested; fetch it only when the
            # board actually lacks what it carries.
            missing_caps = key == "symbol_price" and any(
                row.upper_cap is None for row in board.values()
            )
            missing_names = key == "symbol_name" and any(
                row.company_name is None for row in board.values()
            )
            if not (missing_caps or missing_names):
                continue
            rows, payload = _metadata_dataset(client, key, trade_date, wanted, rejected)
            if rows and payload is not None:
                sources.append(payload.source_ref())
                used.append(key)
        if rows:
            board = _merge_metadata(board, rows, key)

    # -- 4. record what PSX did not publish -------------------------------- #
    if any(row.free_float_mcap is None for row in board.values()):
        missing.add("free_float_mcap")
    if any(row.upper_cap is None for row in board.values()):
        missing.add("exchange_circuit_caps")
    if any(row.trades is None for row in board.values()):
        missing.add("trades")
    if any(row.sector is None for row in board.values()):
        missing.add("sector")

    if wanted:
        board = {k: v for k, v in board.items() if k in wanted}

    return MarketDump(
        trade_date=trade_date,
        rows=board,
        sources=tuple(sources),
        datasets_used=tuple(dict.fromkeys(used)),
        rejected=tuple(rejected),
        missing_fields=tuple(sorted(missing)),
    )


# --------------------------------------------------------------------------- #
# Merge helpers
# --------------------------------------------------------------------------- #


def _merge(base: Mapping[str, MarketRow], extra: Mapping[str, MarketRow]) -> dict[str, MarketRow]:
    """Union two boards, keeping the first board's numbers and filling gaps."""

    merged = dict(base)
    for symbol, row in extra.items():
        existing = merged.get(symbol)
        if existing is None:
            merged[symbol] = row
            continue
        merged[symbol] = MarketRow(
            symbol=symbol,
            close=existing.close,
            company_name=existing.company_name or row.company_name,
            sector=existing.sector or row.sector,
            board=existing.board or row.board,
            open=existing.open if existing.open is not None else row.open,
            high=existing.high if existing.high is not None else row.high,
            low=existing.low if existing.low is not None else row.low,
            prev_close=existing.prev_close if existing.prev_close is not None else row.prev_close,
            change=existing.change if existing.change is not None else row.change,
            pct_change=existing.pct_change if existing.pct_change is not None else row.pct_change,
            volume=existing.volume if existing.volume is not None else row.volume,
            value=existing.value if existing.value is not None else row.value,
            trades=existing.trades if existing.trades is not None else row.trades,
            upper_cap=existing.upper_cap if existing.upper_cap is not None else row.upper_cap,
            lower_cap=existing.lower_cap if existing.lower_cap is not None else row.lower_cap,
            free_float_shares=existing.free_float_shares
            if existing.free_float_shares is not None
            else row.free_float_shares,
            free_float_mcap=existing.free_float_mcap
            if existing.free_float_mcap is not None
            else row.free_float_mcap,
            market_cap=existing.market_cap if existing.market_cap is not None else row.market_cap,
        )
    return merged


def _merge_metadata(
    board: Mapping[str, MarketRow], meta: Mapping[str, MarketRow], key: str
) -> dict[str, MarketRow]:
    """Attach caps/names from a metadata file onto an existing price board."""

    merged = dict(board)
    for symbol, row in meta.items():
        existing = merged.get(symbol)
        if existing is None:
            merged[symbol] = row
            continue
        updates: dict[str, object] = {}
        if key == "symbol_price":
            if row.upper_cap is not None:
                updates["upper_cap"] = row.upper_cap
            if row.lower_cap is not None:
                updates["lower_cap"] = row.lower_cap
        else:
            if row.company_name:
                updates["company_name"] = row.company_name
        if updates:
            merged[symbol] = _replace(existing, **updates)
    return merged


def _replace(row: MarketRow, **updates) -> MarketRow:
    from dataclasses import replace

    return replace(row, **updates)


def _metadata_dataset(
    client: PortalClient,
    key: str,
    trade_date: date,
    wanted: set[str] | None,
    rejected: list[str],
) -> tuple[dict[str, MarketRow], Payload | None]:
    dataset = PSX_DATASETS[key]
    for ext in dataset.exts:
        url = daily_dataset_url(key, trade_date, ext)
        try:
            payload = client.get(
                url,
                kind=SourceKind.DAILY_DOWNLOAD,
                label=f"PSX {dataset.label} {trade_date.isoformat()}",
                as_of=trade_date,
                ttl_seconds=None,
                allow_missing=True,
            )
        except PortalError as exc:
            rejected.append(f"{url} :: {exc}")
            continue
        if payload is None:
            rejected.append(f"{url} :: 404 (not published)")
            continue
        try:
            table = _payload_to_table(payload)
        except (PayloadRejected, ValueError) as exc:
            rejected.append(f"{url} :: {exc}")
            continue
        rows: dict[str, MarketRow] = {}
        symbol_column = table.find_symbol_column()
        if symbol_column is None:
            rejected.append(f"{url} :: no symbol column")
            continue
        for record in table.rows:
            symbol = (record.get(symbol_column) or "").strip().upper()
            if not _SYMBOL_RE.match(symbol) or (wanted is not None and symbol not in wanted):
                continue
            rows[symbol] = MarketRow(
                symbol=symbol,
                close=coerce_price(record.get("close")) or 0.0,
                company_name=(record.get("company_name") or "").strip() or None,
                upper_cap=coerce_price(record.get("upper_cap")),
                lower_cap=coerce_price(record.get("lower_cap")),
            )
        if rows:
            return rows, payload
        rejected.append(f"{url} :: no scrips parsed")
    return {}, None


# --------------------------------------------------------------------------- #
# Cached convenience wrapper
# --------------------------------------------------------------------------- #


class DumpStore:
    """Per-process memo so several nodes can share one scrape."""

    def __init__(self, client: PortalClient) -> None:
        self.client = client
        self._cache: dict[tuple[date, tuple[str, ...] | None], MarketDump] = {}

    def get(self, trade_date: date, symbols: Sequence[str] | None = None) -> MarketDump:
        key = (trade_date, tuple(sorted(normalize_symbol(s) for s in symbols)) if symbols else None)
        if key not in self._cache:
            self._cache[key] = scrape_market_dump(
                trade_date, symbols=symbols, client=self.client
            )
        return self._cache[key]


@lru_cache(maxsize=8)
def _cached_board(trade_date: date) -> MarketDump:
    return scrape_market_dump(trade_date)


def fetch_market_dump(trade_date: date, *, symbols: Sequence[str] | None = None) -> MarketDump:
    """Module-level convenience wrapper around :func:`scrape_market_dump`.

    Kept for scripts and notebooks; the graph uses an injected
    :class:`DumpStore` so one scrape feeds every node.
    """

    return _cached_board(trade_date) if symbols is None else scrape_market_dump(
        trade_date, symbols=symbols
    )


def validate_csv_like(payload: Payload) -> None:
    """Reject a 200-with-HTML on a CSV endpoint before parsing it."""

    require_csv_like(payload)
