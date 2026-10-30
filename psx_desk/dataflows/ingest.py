"""Node 1 — PSX data ingestion.

Turns one ticker and one session into a :class:`~psx_desk.schemas.PSXMarketPacket`:
the session quote, the trailing daily bars, the exchange circuit caps, the
Defaulters/Non-Compliant check, the PUCARS disclosures and the SBP backdrop.

This is the only node allowed to talk to the network, and it is the only node
that can fail a run for lack of data.  Everything downstream is a pure function
of the packet, which is why the analysts cannot quietly reach for a number PSX
did not publish.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from psx_desk.config import DeskConfig
from psx_desk.dataflows.disclosures import DefaulterIndex, fetch_disclosures, load_defaulter_index
from psx_desk.dataflows.http import PortalClient, PortalError
from psx_desk.dataflows.macro import fetch_macro_backdrop
from psx_desk.dataflows.portal import (
    DumpStore,
    MarketDump,
    MarketDumpUnavailable,
    MarketRow,
)
from psx_desk.dataflows.timeseries import PriceHistory, TimeseriesUnavailable, fetch_eod_history
from psx_desk.indicators import average
from psx_desk.market_rules import compute_circuit_state
from psx_desk.schemas import (
    DataQuality,
    IndexSnapshot,
    MacroBackdrop,
    PSXMarketPacket,
    PSXQuote,
    SourceRef,
    coerce_price,
)
from psx_desk.sources import normalize_symbol

logger = logging.getLogger(__name__)

INDEX_PROXIES: tuple[str, ...] = ("KSE100", "KSE30", "ALLSHR")


class IngestionError(RuntimeError):
    """PSX returned nothing usable for the requested symbol and session."""


@dataclass(slots=True)
class DeskData:
    """Shared, scrape-once context for a run."""

    client: PortalClient
    config: DeskConfig
    dumps: DumpStore
    _macro: MacroBackdrop | None = None
    _defaulters: DefaulterIndex | None = None
    _indices: dict[str, IndexSnapshot] | None = None

    def macro(self, as_of: date) -> MacroBackdrop:
        if self._macro is None:
            self._macro = fetch_macro_backdrop(self.client, self.config, as_of=as_of)
        return self._macro

    def defaulters(self, as_of: date) -> DefaulterIndex:
        if self._defaulters is None:
            self._defaulters = load_defaulter_index(self.client, as_of=as_of)
        return self._defaulters

    def indices(self, as_of: date) -> list[IndexSnapshot]:
        if self._indices is None:
            self._indices = fetch_indices(self.client, as_of=as_of)
        return list(self._indices.values())


def build_desk_data(config: DeskConfig, *, client: PortalClient | None = None) -> DeskData:
    client = client or PortalClient(config)
    return DeskData(client=client, config=config, dumps=DumpStore(client))


# --------------------------------------------------------------------------- #
# Indices
# --------------------------------------------------------------------------- #


def fetch_indices(client: PortalClient, *, as_of: date) -> dict[str, IndexSnapshot]:
    """Latest close for the headline PSX indices (best effort, never fatal)."""

    snapshots: dict[str, IndexSnapshot] = {}
    for symbol in INDEX_PROXIES:
        try:
            history = fetch_eod_history(symbol, client, as_of=as_of, days=5)
        except (PortalError, TimeseriesUnavailable, ValueError) as exc:
            logger.info("index %s unavailable: %s", symbol, exc)
            continue
        latest = history.latest
        previous = history.bars[-2] if len(history.bars) > 1 else None
        if latest is None:
            continue
        change = None
        if previous and previous.close:
            change = round(100.0 * (latest.close - previous.close) / previous.close, 3)
        snapshots[symbol] = IndexSnapshot(
            symbol=symbol,
            close=latest.close,
            change_pct=change,
            volume_shares=latest.volume,
            as_of=latest.trade_date,
        )
    return snapshots


# --------------------------------------------------------------------------- #
# Packet assembly
# --------------------------------------------------------------------------- #


def _load_board(
    symbol: str, trade_date: date, data: DeskData, notes: list[str]
) -> tuple[MarketRow | None, MarketDump | None]:
    try:
        dump = data.dumps.get(trade_date, [symbol])
    except MarketDumpUnavailable as exc:
        notes.append(
            f"No PSX board file for {trade_date.isoformat()}; fell back to the EOD timeseries. "
            f"Probed: {len(exc.rejected)} endpoints."
        )
        logger.info("board unavailable for %s: %s", trade_date, exc)
        return None, None
    row = dump.get(symbol)
    if row is None:
        notes.append(f"{symbol} was not present in the {trade_date.isoformat()} PSX board.")
    if dump.missing_fields:
        notes.append(f"Board missing: {', '.join(dump.missing_fields)}.")
    return row, dump


def _history_for(
    symbol: str, trade_date: date, data: DeskData, notes: list[str]
) -> PriceHistory | None:
    try:
        return fetch_eod_history(
            symbol,
            data.client,
            as_of=trade_date,
            days=data.config.history_days,
            min_bars=1,
        )
    except (PortalError, TimeseriesUnavailable, ValueError) as exc:
        notes.append(f"DPS timeseries unavailable for {symbol}: {exc}")
        logger.warning("timeseries unavailable for %s: %s", symbol, exc)
        return None


def _history_dicts(history: PriceHistory | None) -> list[dict]:
    if history is None:
        return []
    return [
        {
            "date": bar.trade_date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in history.bars
    ]


def build_market_packet(
    symbol: str,
    trade_date: date,
    *,
    data: DeskData,
    disclosures_limit: int = 12,
) -> PSXMarketPacket:
    """Assemble the verified PSX packet for one scrip and session.

    Raises:
        IngestionError: neither the board nor the timeseries yielded a close for
            the symbol.  The desk stops here rather than analysing a blank.
    """

    sym = normalize_symbol(symbol)
    notes: list[str] = []
    sources: list[SourceRef] = []
    missing: set[str] = set()

    row, dump = _load_board(sym, trade_date, data, notes)
    if dump is not None:
        sources.extend(dump.sources)

    history = _history_for(sym, trade_date, data, notes)
    if history is not None and history.source is not None:
        sources.append(history.source)

    close = row.close if row and row.close else (history.latest.close if history and history.latest else None)
    if close is None or close <= 0:
        raise IngestionError(
            f"PSX published no usable price for {sym} on {trade_date.isoformat()}. "
            f"Notes: {'; '.join(notes) or 'no detail'}"
        )

    bars = _history_dicts(history)
    prev_close = row.prev_close if row and row.prev_close else None
    if prev_close is None and history is not None and len(history.bars) >= 2:
        prev_close = history.bars[-2].close
        notes.append("Previous close taken from the DPS timeseries (board file had none).")
    if prev_close is None:
        missing.add("prev_close")

    session_open = row.open if row and row.open else (history.latest.open if history and history.latest else None)
    session_high = row.high if row and row.high else (history.latest.high if history and history.latest else None)
    session_low = row.low if row and row.low else (history.latest.low if history and history.latest else None)
    volume = row.volume if row and row.volume is not None else (history.latest.volume if history and history.latest else None)
    value = row.value if row and row.value is not None else None
    if value is None and volume is not None:
        value = round(volume * close, 2)

    for name, item in (
        ("open", session_open),
        ("high", session_high),
        ("low", session_low),
        ("volume", volume),
        ("trades", row.trades if row else None),
        ("free_float_mcap", row.free_float_mcap if row else None),
        ("sector", row.sector if row else None),
    ):
        if item is None:
            missing.add(name)

    circuit = compute_circuit_state(
        close=close,
        prev_close=prev_close,
        history=bars,
        exchange_upper=row.upper_cap if row else None,
        exchange_lower=row.lower_cap if row else None,
        band_pct=data.config.circuit_band_pct,
        band_floor_pkr=data.config.circuit_band_floor_pkr,
        tick=data.config.tick_size_pkr,
    )
    if circuit.cap_source == "computed":
        # No authoritative cap file for this session: the band is arithmetic.
        missing.add("exchange_circuit_caps")

    change = row.change if row and row.change is not None else None
    pct_change = row.pct_change if row and row.pct_change is not None else None
    if change is None and prev_close:
        change = round(close - prev_close, 4)
    if pct_change is None and prev_close:
        pct_change = round(100.0 * (close - prev_close) / prev_close, 4)

    quote = PSXQuote(
        symbol=sym,
        company_name=row.company_name if row else None,
        sector=row.sector if row else None,
        board=row.board if row else "REG",
        trade_date=trade_date,
        open_pkr=coerce_price(session_open),
        high_pkr=coerce_price(session_high),
        low_pkr=coerce_price(session_low),
        close_pkr=close,
        prev_close_pkr=coerce_price(prev_close),
        change_pkr=change,
        change_pct=pct_change,
        volume_shares=coerce_price(volume),
        value_pkr=coerce_price(value),
        trades=row.trades if row else None,
        free_float_shares=row.free_float_shares if row else None,
        free_float_mcap_pkr=row.free_float_mcap if row else None,
        market_cap_pkr=row.market_cap if row else None,
        upper_cap_pkr=row.upper_cap if row else None,
        lower_cap_pkr=row.lower_cap if row else None,
        sources=list(sources),
    )

    defaulter_index = data.defaulters(trade_date)
    defaulter = defaulter_index.check(sym, quote.company_name)
    if defaulter.source is not None:
        sources.append(defaulter.source)
    if not defaulter.checked:
        notes.append("Defaulters portal unverified: the CRO gate reports UNKNOWN, not PASS.")

    macro = data.macro(trade_date)
    if macro.quality is not DataQuality.VERIFIED:
        notes.append(
            "SBP backdrop used the configured print "
            f"(as of {macro.as_of}); treat the policy rate as stale."
        )
    sources.extend(macro.sources)

    disclosures = fetch_disclosures(
        sym,
        data.client,
        company_name=quote.company_name,
        limit=disclosures_limit,
        as_of=trade_date,
    )

    volumes = [bar.volume for bar in (history.bars if history else ()) if bar.volume is not None]
    adv_shares = average(volumes, 20)
    values = [
        bar.volume * bar.close
        for bar in (history.bars[-20:] if history else ())
        if bar.volume is not None
    ]
    adv_value = sum(values) / len(values) if values else None
    year = history.bars[-252:] if history else ()
    high_52w = max((b.high or b.close) for b in year) if year else None
    low_52w = min((b.low or b.close) for b in year) if year else None
    if high_52w is None:
        missing.add("history")
    if adv_shares is None:
        missing.add("adv_shares_20d")

    quality = _grade_quality(
        board_ok=row is not None and bool(row.close),
        history_bars=len(history.bars) if history else 0,
        missing=missing,
        macro=macro,
        defaulter_checked=defaulter.checked,
    )
    if quality is DataQuality.STALE:
        notes.append("Session data is older than the requested trade date.")
    if quality is DataQuality.PARTIAL:
        notes.append("Packet is partial; agents must state which fields were unavailable.")

    return PSXMarketPacket(
        symbol=sym,
        trade_date=trade_date,
        quote=quote,
        circuit=circuit,
        history=bars,
        adv_shares_20d=round(adv_shares, 2) if adv_shares else None,
        adv_value_pkr_20d=round(adv_value, 2) if adv_value else None,
        high_52w_pkr=round(high_52w, 4) if high_52w else None,
        low_52w_pkr=round(low_52w, 4) if low_52w else None,
        indices=data.indices(trade_date),
        defaulter=defaulter,
        macro=macro,
        disclosures=disclosures,
        data_quality=quality,
        missing_fields=sorted(missing),
        notes=notes,
        sources=_dedupe_sources(sources),
    )


def _grade_quality(
    *,
    board_ok: bool,
    history_bars: int,
    missing: set[str],
    macro: MacroBackdrop,
    defaulter_checked: bool,
) -> DataQuality:
    if not board_ok and history_bars == 0:
        return DataQuality.MISSING
    if history_bars < 30:
        return DataQuality.PARTIAL
    critical = {"prev_close", "volume", "history"}
    if critical & missing or macro.quality is not DataQuality.VERIFIED or not defaulter_checked:
        return DataQuality.PARTIAL
    if missing:
        return DataQuality.PARTIAL
    return DataQuality.VERIFIED


def _dedupe_sources(sources: Sequence[SourceRef]) -> list[SourceRef]:
    seen: dict[tuple[str, str | None], SourceRef] = {}
    for ref in sources:
        key = (ref.url, ref.sha256)
        seen.setdefault(key, ref)
    return list(seen.values())


def ingest(symbol: str, trade_date: date, data: DeskData) -> PSXMarketPacket:
    """Convenience wrapper used by the graph node."""

    return build_market_packet(symbol, trade_date, data=data)


def today_pkt() -> date:
    """Today in Karachi (the desk's clock, not the server's)."""

    from zoneinfo import ZoneInfo

    try:
        return datetime.now(ZoneInfo("Asia/Karachi")).date()
    except Exception:  # pragma: no cover - tz database missing
        return datetime.now(UTC).date()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - manual smoke path
    """``python -m psx_desk.dataflows.ingest SYS`` prints the raw packet."""

    import argparse

    from psx_desk.config import load_config

    parser = argparse.ArgumentParser(description="Dump the raw PSX packet for a scrip.")
    parser.add_argument("symbol")
    parser.add_argument("--date", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    trade_date = date.fromisoformat(args.date) if args.date else today_pkt()
    config = load_config()
    packet = build_market_packet(args.symbol, trade_date, data=build_desk_data(config))
    if args.json:
        print(packet.model_dump_json(indent=2))
    else:
        print(f"{packet.symbol} @ {packet.trade_date}: PKR {packet.quote.close_pkr}")
        print(f"circuit: {packet.circuit.locked} ({packet.circuit.band_pct:g}% band)")
        print(f"quality: {packet.data_quality}")
        print(f"missing: {', '.join(packet.missing_fields) or 'none'}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DeskData",
    "IngestionError",
    "INDEX_PROXIES",
    "build_desk_data",
    "build_market_packet",
    "fetch_indices",
    "ingest",
    "today_pkt",
]
