"""Deterministic technicals computed from PSX bars.

The desk's rule is simple: **numbers come from pandas, words come from the
model.**  Every measured field in :class:`~psx_desk.schemas.TechnicalReport`
(SMA, RSI, ATR, volatility, volume z-score, support/resistance) is computed
here and written back over whatever the model said, so an LLM cannot invent an
RSI.  The model is left with the parts that require judgement: what the tape
means, which signals matter, how confident to be, and where the thesis breaks.

Where PSX's timeseries omits high/low (its EOD rows often do), the desk says so
and falls back to close-to-close ranges instead of fabricating intraday extremes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from psx_desk.dataflows.timeseries import Bar, PriceHistory


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    """Every measured technical value for one symbol, as of one session."""

    symbol: str
    closes: tuple[float, ...]
    volumes: tuple[float, ...]
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    ema_20: float | None = None
    rsi_14: float | None = None
    atr_14: float | None = None
    volatility_annualised_pct: float | None = None
    roc_20_pct: float | None = None
    volume_zscore_20: float | None = None
    adv_shares_20: float | None = None
    adv_value_20: float | None = None
    high_52w: float | None = None
    low_52w: float | None = None
    support: float | None = None
    resistance: float | None = None
    momentum_score: int = 50
    trend: str = "sideways"
    has_high_low: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# Primitive indicator maths (dependency-light, vectorised with numpy)
# --------------------------------------------------------------------------- #


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def ema(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    multiplier = 2.0 / (window + 1)
    value = sum(values[:window]) / window
    for price in values[window:]:
        value = (price - value) * multiplier + value
    return value


def rsi(values: list[float], window: int = 14) -> float | None:
    """Wilder's RSI."""

    if len(values) < window + 1:
        return None
    gains = 0.0
    losses = 0.0
    for index in range(1, window + 1):
        delta = values[index] - values[index - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / window
    avg_loss = losses / window
    for index in range(window + 1, len(values)):
        delta = values[index] - values[index - 1]
        avg_gain = (avg_gain * (window - 1) + max(delta, 0.0)) / window
        avg_loss = (avg_loss * (window - 1) + max(-delta, 0.0)) / window
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def atr(bars: list[Bar], window: int = 14, *, use_high_low: bool = True) -> float | None:
    """Average true range, or close-to-close range when PSX gave no H/L."""

    if len(bars) < window + 1:
        return None
    ranges: list[float] = []
    for index in range(1, len(bars)):
        previous = bars[index - 1].close
        current = bars[index]
        if use_high_low and current.high is not None and current.low is not None:
            ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous),
                    abs(current.low - previous),
                )
            )
        else:
            ranges.append(abs(current.close - previous))
    if len(ranges) < window:
        return None
    return round(sum(ranges[-window:]) / window, 4)


def realised_volatility(closes: list[float], window: int = 60, periods: int = 252) -> float | None:
    """Annualised standard deviation of daily log returns, in percent."""

    if len(closes) < max(3, window // 4):
        return None
    sample = closes[-(window + 1):]
    returns = [
        math.log(sample[i] / sample[i - 1])
        for i in range(1, len(sample))
        if sample[i] > 0 and sample[i - 1] > 0
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return round(math.sqrt(variance) * math.sqrt(periods) * 100.0, 2)


def rate_of_change(closes: list[float], window: int = 20) -> float | None:
    if len(closes) < window + 1 or closes[-window - 1] <= 0:
        return None
    return round(100.0 * (closes[-1] / closes[-window - 1] - 1.0), 3)


def volume_zscore(volumes: list[float], window: int = 20) -> float | None:
    if len(volumes) < window + 1:
        return None
    sample = volumes[-window - 1: -1]
    mean = sum(sample) / len(sample)
    variance = sum((v - mean) ** 2 for v in sample) / max(1, len(sample) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return round((volumes[-1] - mean) / std, 3)


def average(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def swing_levels(
    bars: list[Bar], *, lookback: int = 20, close: float
) -> tuple[float | None, float | None]:
    """Nearest horizontal support below and resistance above the close.

    Uses the recent session extremes when PSX published high/low, and closes
    otherwise.  Returns ``None`` for a side the tape has not defined rather
    than guessing a round number.
    """

    window = bars[-lookback:]
    if not window:
        return None, None
    lows = [b.low if b.low is not None else b.close for b in window]
    highs = [b.high if b.high is not None else b.close for b in window]
    below = [level for level in lows if level < close]
    above = [level for level in highs if level > close]
    support = max(below) if below else None
    resistance = min(above) if above else None
    return (
        round(support, 4) if support is not None else None,
        round(resistance, 4) if resistance is not None else None,
    )


def momentum_score(
    *,
    close: float,
    sma_20: float | None,
    sma_50: float | None,
    sma_200: float | None,
    rsi_14: float | None,
    roc_20_pct: float | None,
    volume_z: float | None,
) -> int:
    """Composite 0-100 momentum read, symmetric around 50.

    Deliberately transparent: each component contributes a bounded number of
    points, so a reviewer can reproduce the score by hand from the report.
    """

    score = 50.0
    if sma_20:
        score += 12.0 if close > sma_20 else -12.0
    if sma_20 and sma_50:
        score += 12.0 if sma_20 > sma_50 else -12.0
    if sma_200:
        score += 8.0 if close > sma_200 else -8.0
    if rsi_14 is not None:
        score += max(-12.0, min(12.0, (rsi_14 - 50.0) * 0.4))
    if roc_20_pct is not None:
        score += max(-10.0, min(10.0, roc_20_pct * 0.5))
    if volume_z is not None:
        # Volume confirms direction: heavy tape amplifies whichever way price went.
        direction = 1.0 if (roc_20_pct or 0.0) >= 0 else -1.0
        score += max(-6.0, min(6.0, volume_z * 2.0)) * direction
    return int(max(0, min(100, round(score))))


def classify_trend(
    close: float, sma_20: float | None, sma_50: float | None, sma_200: float | None
) -> str:
    votes = 0
    for level in (sma_20, sma_50, sma_200):
        if level is None:
            continue
        votes += 1 if close > level else -1
    if votes >= 2:
        return "uptrend"
    if votes <= -2:
        return "downtrend"
    return "sideways"


# --------------------------------------------------------------------------- #
# Snapshot builder
# --------------------------------------------------------------------------- #


def build_snapshot(history: PriceHistory, *, volatility_window: int = 60) -> TechnicalSnapshot:
    """Compute every measured field from a :class:`PriceHistory`."""

    bars = list(history.bars)
    if not bars:
        raise ValueError(f"{history.symbol}: no bars to analyse")
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars if bar.volume is not None]
    close = closes[-1]
    notes: list[str] = []

    has_hl = history.has_high_low
    if not has_hl:
        notes.append(
            "PSX EOD rows carried no high/low; ATR and swing levels use close-to-close ranges."
        )

    sma_20 = sma(closes, 20)
    sma_50 = sma(closes, 50)
    sma_200 = sma(closes, 200)
    if sma_200 is None:
        notes.append(f"only {len(closes)} sessions available; 200-day average not computable.")

    rsi_14 = rsi(closes, 14)
    atr_14 = atr(bars, 14, use_high_low=has_hl)
    vol = realised_volatility(closes, window=volatility_window)
    roc_20 = rate_of_change(closes, 20)
    volume_z = volume_zscore(volumes, 20) if volumes else None
    adv_shares = average(volumes, 20) if volumes else None
    adv_value = None
    if adv_shares is not None:
        window = bars[-20:]
        traded_value = [
            (bar.volume * bar.close) for bar in window if bar.volume is not None
        ]
        adv_value = sum(traded_value) / len(traded_value) if traded_value else None

    year = bars[-252:]
    high_52w = max((b.high if b.high is not None else b.close) for b in year)
    low_52w = min((b.low if b.low is not None else b.close) for b in year)
    support, resistance = swing_levels(bars[:-1], lookback=20, close=close)

    score = momentum_score(
        close=close,
        sma_20=sma_20,
        sma_50=sma_50,
        sma_200=sma_200,
        rsi_14=rsi_14,
        roc_20_pct=roc_20,
        volume_z=volume_z,
    )

    return TechnicalSnapshot(
        symbol=history.symbol,
        closes=tuple(closes),
        volumes=tuple(volumes),
        sma_20=round(sma_20, 4) if sma_20 else None,
        sma_50=round(sma_50, 4) if sma_50 else None,
        sma_200=round(sma_200, 4) if sma_200 else None,
        ema_20=round(ema(closes, 20), 4) if ema(closes, 20) else None,
        rsi_14=rsi_14,
        atr_14=atr_14,
        volatility_annualised_pct=vol,
        roc_20_pct=roc_20,
        volume_zscore_20=volume_z,
        adv_shares_20=round(adv_shares, 2) if adv_shares else None,
        adv_value_20=round(adv_value, 2) if adv_value else None,
        high_52w=round(high_52w, 4),
        low_52w=round(low_52w, 4),
        support=support,
        resistance=resistance,
        momentum_score=score,
        trend=classify_trend(close, sma_20, sma_50, sma_200),
        has_high_low=has_hl,
        notes=tuple(notes),
    )
