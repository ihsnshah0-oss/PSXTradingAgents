"""The source allow-list is what makes this a PSX-only desk, so it is tested hard."""

from __future__ import annotations

from datetime import date

import pytest

from psx_desk import sources
from psx_desk.sources import (
    ALLOWED_HOSTS,
    PSX_DATASETS,
    assert_source_allowed,
    daily_dataset_url,
    data_dump_candidates,
    eod_timeseries_url,
    is_index,
    normalize_symbol,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://finance.yahoo.com/quote/SYS.KA",
        "https://finnhub.io/api/v1/quote?symbol=SYS",
        "https://www.sec.gov/cgi-bin/browse-edgar",
        "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY",
        "https://api.polygon.io/v2/aggs/ticker/SYS",
        "http://localhost:8000/data",
        "ftp://dps.psx.com.pk/file",
    ],
)
def test_foreign_sources_are_refused(url):
    with pytest.raises(ValueError):
        assert_source_allowed(url)


def test_every_psx_surface_is_allowed():
    for url in (
        sources.MARKET_WATCH_URL,
        sources.SYMBOL_DIRECTORY_URL,
        sources.DEFAULTER_PORTAL_URL,
        sources.CIRCUIT_BREAKERS_URL,
        sources.PUCARS_URL,
        sources.SBP_HOME_URL,
    ):
        assert assert_source_allowed(url) == url
        assert url.split("/")[2] in ALLOWED_HOSTS


def test_dataset_urls_follow_the_portal_download_pattern():
    url = daily_dataset_url("symbol_price", date(2026, 10, 29), "zip")
    assert url == "https://dps.psx.com.pk/download/symbol_price/2026-10-29.zip"


def test_unknown_dataset_is_a_clear_error():
    with pytest.raises(KeyError):
        daily_dataset_url("nasdaq_dump", date(2026, 10, 29))


def test_registered_datasets_are_the_real_portal_slugs():
    assert {"quote", "symbol_price", "symbol_name", "mkt_summary", "announce"} <= set(PSX_DATASETS)


def test_dump_candidates_are_all_psx_and_ordered():
    candidates = data_dump_candidates(date(2026, 10, 29))
    assert candidates[0].startswith("https://dps.psx.com.pk/download/")
    assert all("psx.com.pk" in url for url in candidates)
    assert all(url.startswith("https://") for url in candidates)


def test_symbol_normalisation():
    assert normalize_symbol("sys") == "SYS"
    assert normalize_symbol(" PSX:engro ") == "ENGRO"
    assert normalize_symbol("LUCK.KA") == "LUCK"
    for bad in ("", "  ", "SYS; DROP TABLE", "1234", "A" * 20):
        with pytest.raises(ValueError):
            normalize_symbol(bad)


def test_index_detection():
    assert is_index("KSE100")
    assert not is_index("SYS")


def test_timeseries_url_is_built_from_the_symbol():
    assert eod_timeseries_url("sys") == "https://dps.psx.com.pk/timeseries/eod/SYS"
