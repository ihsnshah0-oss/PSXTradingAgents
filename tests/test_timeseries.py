"""DPS timeseries parsing: shapes, point-in-time truncation, honesty about gaps."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from psx_desk.dataflows.timeseries import (
    TimeseriesUnavailable,
    fetch_eod_history,
    parse_dps_series,
)

KARACHI = ZoneInfo("Asia/Karachi")


def stamp(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 15, 30, tzinfo=KARACHI).timestamp())


def test_positional_rows_newest_first_become_oldest_first():
    payload = json.dumps(
        [
            [stamp(date(2026, 10, 29)), 412.55, 1_200_000, 410.0],
            [stamp(date(2026, 10, 28)), 405.10, 900_000, 402.0],
        ]
    )
    bars = parse_dps_series(payload, "SYS")
    assert [bar.trade_date for bar in bars] == [date(2026, 10, 28), date(2026, 10, 29)]
    assert bars[-1].close == 412.55
    assert bars[-1].volume == 1_200_000
    assert bars[-1].high is None  # PSX published no high: we do not invent one


def test_dict_rows_and_envelopes_are_accepted():
    payload = json.dumps(
        {"data": [{"ts": stamp(date(2026, 10, 29)), "close": 100.5, "vol": 10, "high": 101, "low": 99}]}
    )
    bars = parse_dps_series(payload, "ENGRO")
    assert bars[0].close == 100.5
    assert bars[0].high == 101
    assert bars[0].low == 99


def test_invalid_rows_are_dropped_not_zero_filled():
    payload = json.dumps(
        [
            [stamp(date(2026, 10, 29)), 0, 100, 0],
            [stamp(date(2026, 10, 28)), None, 100, None],
            [stamp(date(2026, 10, 27)), 50.0, 100, 49.0],
        ]
    )
    bars = parse_dps_series(payload, "SYS")
    assert len(bars) == 1
    assert bars[0].close == 50.0


def test_garbage_is_an_explicit_failure():
    with pytest.raises(TimeseriesUnavailable):
        parse_dps_series(b"<html>404</html>", "SYS")
    with pytest.raises(TimeseriesUnavailable):
        parse_dps_series(b"[]", "SYS")


def test_fetch_truncates_at_the_analysis_date(config, world, client):
    history = fetch_eod_history("SYS", client, as_of=date(2026, 10, 20), days=400)
    assert history.latest.trade_date <= date(2026, 10, 20)
    assert all(bar.trade_date <= date(2026, 10, 20) for bar in history.bars)
    assert history.source is not None
    assert history.source.url.endswith("/timeseries/eod/SYS")


def test_fetch_reports_partial_quality_without_high_low(config, world, client):
    history = fetch_eod_history("SYS", client, as_of=date(2026, 10, 29), days=60)
    assert len(history.bars) == 60
    assert history.has_high_low is False
    assert history.quality.value == "partial"
