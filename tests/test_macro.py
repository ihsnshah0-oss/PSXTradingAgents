"""SBP macro ingestion: live tiles when reachable, a labelled print when not."""

from __future__ import annotations

import pytest

from psx_desk.dataflows.archive import html_text
from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient
from psx_desk.dataflows.macro import (
    configured_backdrop,
    fetch_macro_backdrop,
    parse_sbp_indicators,
)
from psx_desk.schemas import DataQuality
from tests.conftest import TRADE_DATE, sbp_html


def test_sbp_tiles_are_parsed():
    values = parse_sbp_indicators(html_text(sbp_html(policy=12.0, ceiling=13.0, floor=11.0, cpi=10.3)))
    assert values["sbp_policy_rate_pct"] == pytest.approx(12.0)
    assert values["corridor_ceiling_pct"] == pytest.approx(13.0)
    assert values["corridor_floor_pct"] == pytest.approx(11.0)
    assert values["cpi_yoy_pct"] == pytest.approx(10.3)
    assert values["pkr_usd"] == pytest.approx(277.2072)


def test_live_scrape_is_marked_verified_with_a_source(config, client):
    backdrop = fetch_macro_backdrop(client, config, as_of=TRADE_DATE)
    assert backdrop.basis == "live_scrape"
    assert backdrop.quality is DataQuality.VERIFIED
    assert backdrop.sbp_policy_rate_pct == pytest.approx(12.0)
    assert backdrop.sources and "sbp.org.pk" in backdrop.sources[0].url
    assert backdrop.real_policy_rate_pct == pytest.approx(1.7)


def test_unreachable_sbp_falls_back_to_the_labelled_print(config):
    blank = PortalClient(
        config, transport=FixtureTransport({}), cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    backdrop = fetch_macro_backdrop(blank, config, as_of=TRADE_DATE)
    assert backdrop.basis == "configured_fallback"
    assert backdrop.quality is DataQuality.STALE
    assert backdrop.sbp_policy_rate_pct == config.macro.sbp_policy_rate_pct
    assert backdrop.as_of == config.macro.as_of


def test_a_page_without_the_policy_tile_is_not_trusted(config):
    page = "<html><body><p>Site under maintenance</p></body></html>"
    client = PortalClient(
        config,
        transport=FixtureTransport({"https://www.sbp.org.pk/": page}),
        cache=DiskCache(config.cache_dir),
        sleep=lambda _s: None,
    )
    backdrop = fetch_macro_backdrop(client, config, as_of=TRADE_DATE)
    assert backdrop.basis == "configured_fallback"


def test_configured_backdrop_carries_its_date_and_caveat(config):
    backdrop = configured_backdrop(config)
    assert backdrop.as_of is not None
    assert backdrop.quality is DataQuality.STALE
    assert backdrop.sources == []
