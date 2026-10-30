"""Node 1 — PSX data ingestion.

Turns one ticker and one session into a :class:`~psx_desk.schemas.PSXMarketPacket`:
the session quote, the trailing daily bars, the exchange circuit caps, the
Defaulters/Non-Compliant check, the PUCARS disclosures and the SBP backdrop.

This is the only node allowed to talk to the network, and it is the only node
that can fail a run for lack of data.  Everything downstream is a pure function
of the packet, which is why the analysts cannot quietly reach for a number PSX
did not publish.

GitHub mirrors
--------------
When ``PSXDESK_MIRROR_POLICY`` is ``backfill`` or ``fallback`` this node may
also consult a GitHub mirror (see
:mod:`psx_desk.dataflows.github_mirror`).  The rules the code enforces:

* ``off`` (default): the mirror module is never imported or called.
* ``backfill``: the mirror only fills reference fields — sector, company name,
  index levels — never a price, never a bar.
* ``fallback``: if PSX is unreachable the mirror may supply prices and bars, but
  the packet is forced to ``data_quality="partial"``, every mirror source lands
  in ``sources`` with ``source_kind="mirror"``, and a note records the
  repository and its own snapshot date so a stale mirror can never pass as
  today's tape.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from psx_desk.config import DeskConfig
from psx_desk.dataflows.disclosures import DefaulterIndex, fetch_disclosures, load_defaulter_index
from psx_desk.dataflows.github_mirror import (
    fetch_mirror_history,
    mirror_indices,
    mirror_symbol_info,
    scrape_psx_mirror,
)
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
            if not self._indices and self.config.mirror_policy == "fallback":
                spec = self.config.github_mirror
                if spec:
                    try:
                        snaps = mirror_indices(spec, config=self.config, client=self.client)
                    except (PortalError, ValueError) as exc:
                        logger.info("mirror indices unavailable: %s", exc)
                    else:
                        self._indices = {
                            snap.symbol: snap for snap in snaps if snap.symbol in INDEX_PROXIES
                        }
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
        spec = data.config.github_mirror if data.config.mirror_policy == "fallback" else None
        if spec:
            try:
                mirror_dump = scrape_psx_mirror(
                    spec,
                    symbols=[symbol],
                    config=data.config,
                    client=data.client,
                    via=data.config.github_via,
                    as_of=trade_date,
                )
            except (PortalError, ValueError) as mirror_exc:
                notes.append(f"GitHub mirror {spec} also unavailable: {mirror_exc}")
                logger.info("mirror %s unavailable: %s", spec, mirror_exc)
                return None, None
            as_of_text = mirror_dump.mirror_as_of.isoformat() if mirror_dump.mirror_as_of else "undated"
            notes.append(
                f"PSX board unavailable: prices supplied by the GitHub mirror {mirror_dump.mirror} "
                f"(its own snapshot date {as_of_text}). Mirror data is community-maintained, "
                "not exchange-verified."
            )
            if mirror_dump.mirror_as_of and mirror_dump.mirror_as_of < trade_date:
                notes.append(
                    f"Mirror snapshot is {trade_date - mirror_dump.mirror_as_of} day(s) older than "
                    f"the requested session {trade_date.isoformat()}; treat the close as indicative."
                )
            return mirror_dump.get(symbol), mirror_dump
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
        spec = data.config.github_history_mirror if data.config.mirror_policy == "fallback" else None
        if not spec:
            return None
        try:
            history = fetch_mirror_history(
                symbol,
                mirror=spec,
                config=data.config,
                client=data.client,
                via=data.config.github_via,
                as_of=trade_date,
                days=data.config.history_days,
            )
        except (PortalError, ValueError) as mirror_exc:
            notes.append(f"GitHub history mirror {spec} also unavailable for {symbol}: {mirror_exc}")
            return None
        first = history.bars[0].trade_date.isoformat() if history.bars else "n/a"
        last = history.bars[-1].trade_date.isoformat() if history.bars else "n/a"
        notes.append(
            f"Daily bars for {symbol} taken from the GitHub history dataset {spec} "
            f"({len(history.bars)} bars, {first} to {last}); third-party reconstruction."
        )
        return history


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

    # Reference-only backfill: a mirror may name a sector, never price a scrip.
    if (
        row is not None
        and data.config.mirror_policy in {"backfill", "fallback"}
        and data.config.github_mirror
        and (row.sector is None or row.company_name is None)
    ):
        try:
            directory = mirror_symbol_info(
                data.config.github_mirror, config=data.config, client=data.client
            )
        except (PortalError, ValueError) as exc:
            notes.append(f"Mirror reference directory unavailable: {exc}")
        else:
            info = directory.get(sym)
            if info is not None:
                row = replace(
                    row,
                    sector=row.sector or info.sector,
                    company_name=row.company_name or info.name,
                )
                notes.append(
                    f"Sector/company metadata backfilled from the GitHub mirror "
                    f"{data.config.github_mirror}; prices remain PSX-sourced."
                )

    history = _history_for(sym, trade_date, data, notes)
    if history is not None and history.source is not None:
        sources.append(history.source)

    close = row.close if row and row.close else (history.latest.close if history and history.latest else None)
    if close is None or close <= 0:
        raise IngestionError(
            f"PSX published no usable price for {sym} on {trade_date.isoformat()}. "
            f"Notes: {'; '.join(notes) or 'no detail'}"
        )
    # Without a board row the close can only come from the last bar.  That bar is
    # older than the session whenever a feed lags (a stale mirror, a PSX outage,
    # a weekend backfill), and it must not be presented as the session's print.
    if not (row and row.close) and history is not None and history.latest is not None:
        lag = (trade_date - history.latest.trade_date).days
        if lag > data.config.session_close_max_lag_days:
            raise IngestionError(
                f"No {sym} close for {trade_date.isoformat()}: the most recent bar is "
                f"{history.latest.trade_date.isoformat()} ({lag} days earlier) and the desk's "
                f"limit is {data.config.session_close_max_lag_days} days "
                "(PSXDESK_SESSION_CLOSE_MAX_LAG_DAYS). Refusing to quote a stale close. "
                f"Notes: {'; '.join(notes) or 'no detail'}"
            )
        notes.append(
            f"Close carried from the {history.latest.trade_date.isoformat()} bar "
            f"({lag} day(s) before the requested session); no board row was published."
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

    mirror_sources = [ref for ref in sources if ref.source_kind == "mirror"]
    if dump is not None and dump.from_mirror:
        missing.add("exchange_verified_prices")
    if history is not None and history.source is not None and history.source.source_kind == "mirror":
        missing.add("exchange_verified_history")

    quality = _grade_quality(
        board_ok=row is not None and bool(row.close),
        history_bars=len(history.bars) if history else 0,
        missing=missing,
        macro=macro,
        defaulter_checked=defaulter.checked,
    )
    if mirror_sources:
        # A mirror can never upgrade a packet: the best it can be is partial.
        if quality is DataQuality.VERIFIED:
            quality = DataQuality.PARTIAL
        repos = sorted({repo for ref in mirror_sources if (repo := _mirror_repo(ref.url))})
        notes.append(
            "Packet includes GitHub mirror data ("
            + ", ".join(repos or ["unrecognised mirror"])
            + f"); {len(mirror_sources)} mirror source(s) recorded in sources. "
            "Mirror payloads are community-maintained and not exchange-verified."
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


def _mirror_repo(url: str) -> str | None:
    """``owner/repo`` for a GitHub mirror URL, whatever access path produced it."""

    from urllib.parse import urlparse

    parsed = urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    host = (parsed.hostname or "").lower()
    if "githubusercontent" in host or "codeload" in host:
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    if host == "api.github.com" and len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return None


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
