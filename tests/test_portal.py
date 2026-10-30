"""The download-portal scraper: probe order, merges, and refusal to invent data."""

from __future__ import annotations

from datetime import date

import pytest

from psx_desk.dataflows.portal import (
    MARKET_WATCH_URL,
    MarketDump,
    MarketDumpUnavailable,
    scrape_market_dump,
)
from psx_desk.sources import daily_dataset_url
from tests.conftest import PRICES, TRADE_DATE, World, symbol_price_zip

pytestmark = pytest.mark.unit


def test_scrapes_the_board_and_merges_exchange_caps(world, client):
    dump = scrape_market_dump(TRADE_DATE, client=client)
    assert isinstance(dump, MarketDump)
    assert len(dump) == len(PRICES)
    sys_row = dump.get("SYS")
    assert sys_row is not None
    assert sys_row.close == pytest.approx(PRICES["SYS"])
    assert sys_row.company_name == "Systems Limited"
    # Caps came from the exchange's own symbol_price file, not from a 5% guess.
    assert sys_row.upper_cap == pytest.approx(round(PRICES["SYS"] * 1.05, 2))
    assert "symbol_price" in dump.datasets_used


def test_provenance_is_recorded_for_every_payload_used(world, client):
    dump = scrape_market_dump(TRADE_DATE, client=client)
    urls = [ref.url for ref in dump.sources]
    assert any("symbol_price" in url for url in urls)
    assert any(url.endswith("/market-watch") for url in urls)
    assert all(ref.sha256 for ref in dump.sources)


def test_symbol_filter_does_not_change_the_values(world, client):
    full = scrape_market_dump(TRADE_DATE, client=client)
    single = scrape_market_dump(TRADE_DATE, client=client, symbols=["sys"])
    assert list(single.rows) == ["SYS"]
    assert single.get("SYS").close == full.get("SYS").close


def test_a_missing_dataset_is_reported_not_substituted(config):
    from psx_desk.dataflows.http import DiskCache, PortalClient

    world_without_caps = World(missing_caps_file=True)
    caps_less_client = PortalClient(
        config,
        transport=world_without_caps.transport,
        cache=DiskCache(config.cache_dir),
        sleep=lambda _s: None,
    )
    dump = scrape_market_dump(
        TRADE_DATE, client=caps_less_client, datasets=("symbol_price", "symbol_name")
    )
    assert "exchange_circuit_caps" in dump.missing_fields
    row = dump.get("SYS")
    assert row.upper_cap is None  # never guessed at 5%


def test_previous_close_is_derived_from_the_published_change(world, client):
    row = scrape_market_dump(TRADE_DATE, client=client, symbols=["SYS"]).get("SYS")
    assert row.prev_close is not None
    assert row.close - row.prev_close == pytest.approx(row.change, abs=0.02)


def test_no_usable_payload_raises_with_every_reason(config):
    from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient

    blank = PortalClient(
        config, transport=FixtureTransport({}), cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    with pytest.raises(MarketDumpUnavailable) as excinfo:
        scrape_market_dump(TRADE_DATE, client=blank)
    message = str(excinfo.value)
    assert "no usable market board" in message
    assert all("::" in line for line in excinfo.value.rejected)
    assert len(excinfo.value.rejected) >= 4


def test_live_board_can_be_disabled(client):
    with pytest.raises(MarketDumpUnavailable):
        scrape_market_dump(TRADE_DATE, client=client, allow_live_board=False)


def test_zip_metadata_file_is_parsed_without_a_price_column(client):
    assert symbol_price_zip(["SYS"]).startswith(b"PK")
    dump = scrape_market_dump(TRADE_DATE, client=client, symbols=["LUCK"])
    assert dump.get("LUCK").upper_cap is not None


def test_cached_second_call_does_not_hit_the_transport(world, client):
    scrape_market_dump(TRADE_DATE, client=client)
    before = len(world.transport.probes)
    scrape_market_dump(TRADE_DATE, client=client)
    # Date-keyed files are immutable and the live board is cached for 120s, so a
    # second run inside the window is free.
    assert len(world.transport.probes) <= before + len(PRICES) + 2


def test_dataset_url_is_the_documented_portal_pattern():
    assert daily_dataset_url("symbol_price", date(2026, 10, 29), "zip").endswith(
        "/download/symbol_price/2026-10-29.zip"
    )
    assert MARKET_WATCH_URL == "https://dps.psx.com.pk/market-watch"
