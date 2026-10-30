"""Daily and intraday price history from ``dps.psx.com.pk/timeseries``.

The portal returns a JSON array, newest row first.  Observed row shapes are
positional — ``[unix_ts, close, volume, open]`` — with some scrips carrying
extra columns, and occasionally a dict per row.  Both are handled, and a field
PSX did not publish stays ``None`` rather than being back-filled: if the series
has no high/low, :class:`PriceHistory.has_high_low` is ``False`` and every
indicator that needs it says so instead of pretending.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from psx_desk.dataflows.http import Payload, PortalClient, PortalError, require_not_html
from psx_desk.schemas import DataQuality, SourceRef, coerce_price
from psx_desk.sources import (
    PSX_PORTAL_HOST,
    SourceKind,
    eod_timeseries_url,
    intraday_url,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

KARACHI = ZoneInfo("Asia/Karachi")

#: Positional field order of the DPS timeseries rows, as published.
_POSITIONAL = {
    4: ("ts", "close", "volume", "open"),
    5: ("ts", "close", "volume", "open", "high"),
    6: ("ts", "close", "volume", "open", "high", "low"),
}

_KEY_ALIASES = {
    "ts": "ts",
    "timestamp": "ts",
    "date": "date",
    "time": "ts",
    "close": "close",
    "c": "close",
    "closing": "close",
    "open": "open",
    "o": "open",
    "high": "high",
    "h": "high",
    "low": "low",
    "l": "low",
    "volume": "volume",
    "vol": "volume",
    "v": "volume",
}


class TimeseriesUnavailable(PortalError):
    """PSX published no usable history for the symbol."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One daily session, PKR."""

    trade_date: date
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "date": self.trade_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class PriceHistory:
    """A symbol's trailing daily bars, oldest first."""

    symbol: str
    bars: tuple[Bar, ...]
    source: SourceRef | None = None
    has_high_low: bool = False
    quality: DataQuality = DataQuality.VERIFIED

    @property
    def latest(self) -> Bar | None:
        return self.bars[-1] if self.bars else None

    @property
    def closes(self) -> list[float]:
        return [bar.close for bar in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [bar.volume for bar in self.bars if bar.volume is not None]

    def as_dicts(self) -> list[dict[str, float | str | None]]:
        return [bar.as_dict() for bar in self.bars]

    def last_n(self, n: int) -> list[Bar]:
        return list(self.bars[-n:]) if n > 0 else []


def _session_date(value) -> date | None:
    """Convert a DPS timestamp (unix seconds, ms, or ISO string) to a PKT date."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=KARACHI).date()
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _session_date(float(text))
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(KARACHI).date()
    except ValueError:
        return None


def parse_dps_series(payload: Payload | bytes | str, symbol: str = "") -> list[Bar]:
    """Parse a DPS timeseries payload into bars, oldest first.

    Handles ``[[ts, close, volume, open], ...]``, ``[{"ts":..., "close":...}]``
    and ``{"data": [...]}`` envelopes.  Rows without a usable timestamp or a
    positive close are dropped — never zero-filled.
    """

    if isinstance(payload, Payload):
        require_not_html(payload)
        raw_bytes = payload.content
    elif isinstance(payload, str):
        raw_bytes = payload.encode()
    else:
        raw_bytes = payload

    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        raise TimeseriesUnavailable(f"empty timeseries payload for {symbol}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TimeseriesUnavailable(f"timeseries payload for {symbol} is not JSON: {exc}") from exc

    if isinstance(data, dict):
        for key in ("data", "timeseries", "result", "rows"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise TimeseriesUnavailable(f"unexpected timeseries shape for {symbol}")

    bars: list[Bar] = []
    for row in data:
        if isinstance(row, dict):
            lowered = {_KEY_ALIASES.get(str(k).strip().lower(), str(k).strip().lower()): v for k, v in row.items()}
            session = _session_date(lowered.get("date") or lowered.get("ts"))
            close = coerce_price(lowered.get("close"))
            if session is None or close is None or close <= 0:
                continue
            bars.append(
                Bar(
                    trade_date=session,
                    close=close,
                    open=coerce_price(lowered.get("open")),
                    high=coerce_price(lowered.get("high")),
                    low=coerce_price(lowered.get("low")),
                    volume=coerce_price(lowered.get("volume")),
                )
            )
            continue

        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        fields = _POSITIONAL.get(len(row))
        if fields is None:
            # Longer rows: assume the documented head order and ignore the tail.
            fields = ("ts", "close", "volume", "open", "high", "low")[: len(row)]
        record = dict(zip(fields, row, strict=False))
        session = _session_date(record.get("ts"))
        close = coerce_price(record.get("close"))
        if session is None or close is None or close <= 0:
            continue
        bars.append(
            Bar(
                trade_date=session,
                close=close,
                open=coerce_price(record.get("open")),
                high=coerce_price(record.get("high")),
                low=coerce_price(record.get("low")),
                volume=coerce_price(record.get("volume")),
            )
        )

    if not bars:
        raise TimeseriesUnavailable(f"no valid bars in the timeseries for {symbol}")

    # DPS publishes newest first; the desk works oldest first.  De-duplicate on
    # date, keeping the last value seen for that session.
    by_date = {bar.trade_date: bar for bar in reversed(bars)}
    ordered = tuple(by_date[key] for key in sorted(by_date))
    if len(ordered) != len(bars):
        logger.debug("%s: collapsed %d duplicate sessions", symbol, len(bars) - len(ordered))
    return list(ordered)


def fetch_eod_history(
    symbol: str,
    client: PortalClient,
    *,
    as_of: date | None = None,
    days: int | None = None,
    min_bars: int = 1,
) -> PriceHistory:
    """Fetch daily bars for ``symbol``, truncated at ``as_of`` (point-in-time).

    Truncation happens here rather than in the agents so a backtest can never
    see a session after its decision date.
    """

    sym = normalize_symbol(symbol)
    url = eod_timeseries_url(sym)
    payload = client.get(
        url,
        kind=SourceKind.TIMESERIES,
        label=f"DPS EOD timeseries: {sym}",
        as_of=as_of,
        ttl_seconds=None,
        allow_missing=False,
    )
    assert payload is not None  # allow_missing=False raises instead
    try:
        bars = parse_dps_series(payload, sym)
    except TimeseriesUnavailable:
        raise

    if as_of is not None:
        bars = [bar for bar in bars if bar.trade_date <= as_of]
    if not bars:
        raise TimeseriesUnavailable(f"{sym}: no sessions at or before {as_of}")

    quality = DataQuality.VERIFIED
    has_hl = all(bar.high is not None and bar.low is not None for bar in bars[-60:])
    if not has_hl:
        quality = DataQuality.PARTIAL

    limit = days or 0
    if limit:
        bars = bars[-limit:]
    if len(bars) < min_bars:
        raise TimeseriesUnavailable(f"{sym}: {len(bars)} bars available, {min_bars} required")

    if as_of is not None and bars[-1].trade_date < as_of:
        logger.info("%s: latest session is %s, requested %s", sym, bars[-1].trade_date, as_of)

    return PriceHistory(
        symbol=sym,
        bars=tuple(bars),
        source=payload.source_ref(),
        has_high_low=has_hl,
        quality=quality,
    )


def fetch_intraday(symbol: str, client: PortalClient) -> list[tuple[datetime, float, float | None]]:
    """Today's tick series for a scrip: ``(timestamp PKT, price, volume)``.

    Used only to describe the tape in the technical narrative; the analysis
    itself runs on daily closes so it stays reproducible after the bell.
    """

    sym = normalize_symbol(symbol)
    payload = client.get(
        intraday_url(sym),
        kind=SourceKind.INTRADAY,
        label=f"DPS intraday tape: {sym}",
        ttl_seconds=60,
        allow_missing=True,
    )
    if payload is None:
        return []
    try:
        data = json.loads(payload.content.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("data") or []
    ticks: list[tuple[datetime, float, float | None]] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = coerce_price(row[1])
        if price is None or price <= 0:
            continue
        try:
            stamp = datetime.fromtimestamp(float(row[0]), tz=KARACHI).astimezone(UTC)
        except (TypeError, ValueError, OSError):
            continue
        volume = coerce_price(row[2]) if len(row) > 2 else None
        ticks.append((stamp, price, volume))
    return ticks


def session_from_history(history: PriceHistory) -> Bar | None:
    """Last completed bar, used when the board file has no row for the scrip."""

    return history.latest


__all__ = [
    "Bar",
    "PriceHistory",
    "TimeseriesUnavailable",
    "fetch_eod_history",
    "fetch_intraday",
    "parse_dps_series",
    "session_from_history",
    "PSX_PORTAL_HOST",
]
