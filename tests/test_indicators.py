"""Technicals: deterministic, bounded, and honest about missing PSX fields."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from psx_desk.dataflows.timeseries import Bar, PriceHistory
from psx_desk.indicators import (
    atr,
    build_snapshot,
    classify_trend,
    momentum_score,
    rate_of_change,
    realised_volatility,
    rsi,
    sma,
    swing_levels,
    volume_zscore,
)


def bars_from_closes(closes: list[float], *, with_hl: bool = False) -> list[Bar]:
    start = date(2026, 1, 1)
    bars = []
    for index, close in enumerate(closes):
        bars.append(
            Bar(
                trade_date=start + timedelta(days=index),
                close=close,
                open=close if with_hl else None,
                high=close * 1.01 if with_hl else None,
                low=close * 0.99 if with_hl else None,
                volume=100_000 + index * 100,
            )
        )
    return bars


def history(closes: list[float], *, with_hl: bool = False) -> PriceHistory:
    return PriceHistory(
        symbol="SYS",
        bars=tuple(bars_from_closes(closes, with_hl=with_hl)),
        has_high_low=with_hl,
    )


def test_sma_needs_a_full_window():
    assert sma([1, 2, 3], 5) is None
    assert sma([1, 2, 3, 4, 5], 5) == pytest.approx(3.0)


def test_rsi_is_bounded_and_saturates_on_a_one_way_tape():
    assert rsi([100 + i for i in range(30)]) == pytest.approx(100.0)
    assert rsi([100 - i for i in range(30)]) == pytest.approx(0.0)
    value = rsi([100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101])
    assert value is not None and 0 <= value <= 100


def test_atr_uses_high_low_when_psx_published_it_and_closes_when_not():
    closes = [100 + (i % 5) for i in range(40)]
    rich = atr(bars_from_closes(closes, with_hl=True), 14, use_high_low=True)
    poor = atr(bars_from_closes(closes, with_hl=False), 14, use_high_low=False)
    assert rich is not None and poor is not None
    assert rich > poor


def test_volatility_and_momentum_are_finite():
    closes = [100 * (1.004**i) * (1.01 if i % 3 == 0 else 0.99) for i in range(120)]
    vol = realised_volatility(closes)
    assert vol is not None and 0 < vol < 100
    assert rate_of_change(closes, 20) is not None


def test_volume_zscore_handles_a_flat_tape():
    assert volume_zscore([100.0] * 30) is None


def test_swing_levels_return_none_rather_than_guessing():
    bars = bars_from_closes([100, 101, 102], with_hl=False)
    support, resistance = swing_levels(bars, close=102.0)
    assert support is not None and support < 102.0
    assert resistance is None  # nothing above the close in the window


def test_momentum_score_is_a_bounded_composite():
    strong = momentum_score(
        close=120, sma_20=110, sma_50=100, sma_200=90, rsi_14=72, roc_20_pct=15, volume_z=1.5
    )
    weak = momentum_score(
        close=80, sma_20=90, sma_50=100, sma_200=110, rsi_14=28, roc_20_pct=-15, volume_z=1.5
    )
    assert 0 <= weak < 50 < strong <= 100


def test_trend_classification():
    assert classify_trend(120, 110, 100, 90) == "uptrend"
    assert classify_trend(80, 90, 100, 110) == "downtrend"
    assert classify_trend(100, 100.4, 100.2, 99.8) == "sideways"


def test_snapshot_flags_a_missing_high_low_instead_of_hiding_it():
    snapshot = build_snapshot(history([100 + i for i in range(120)]))
    assert snapshot.has_high_low is False
    assert any("no high/low" in note for note in snapshot.notes)
    assert snapshot.atr_14 is not None  # close-to-close fallback
    assert snapshot.sma_200 is None
    assert any("200-day" in note for note in snapshot.notes)


def test_snapshot_on_a_full_year_of_bars():
    closes = [150 + 40 * ((i % 60) / 60) for i in range(260)]
    snapshot = build_snapshot(history(closes, with_hl=True))
    assert snapshot.sma_200 is not None
    assert 0 <= snapshot.momentum_score <= 100
    assert snapshot.high_52w >= snapshot.low_52w
    assert snapshot.adv_shares_20 is not None
