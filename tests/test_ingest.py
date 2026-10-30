"""Node 1: the packet is built only from what PSX actually published."""

from __future__ import annotations

import pytest

from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient
from psx_desk.dataflows.ingest import IngestionError, build_market_packet
from psx_desk.market_rules import compute_circuit_state
from psx_desk.schemas import DataQuality
from tests.conftest import PRICES, TRADE_DATE, World, eod_payload


def test_packet_is_assembled_from_psx_payloads(desk_data):
    packet = build_market_packet("sys", TRADE_DATE, data=desk_data)
    assert packet.symbol == "SYS"
    assert packet.quote.close_pkr == pytest.approx(PRICES["SYS"])
    assert packet.quote.prev_close_pkr is not None
    assert packet.quote.company_name == "Systems Limited"
    assert len(packet.history) == 320  # every session the fixture publishes
    assert packet.circuit.upper_cap_pkr == pytest.approx(round(PRICES["SYS"] * 1.05, 2))
    assert packet.circuit.cap_source == "exchange_file"
    assert packet.defaulter.checked and not packet.defaulter.is_defaulting
    assert packet.macro is not None and packet.macro.basis == "live_scrape"
    assert packet.adv_shares_20d and packet.adv_value_pkr_20d
    assert packet.sources and all("psx.com.pk" in ref.url or "sbp.org.pk" in ref.url for ref in packet.sources)


def test_locked_scrip_is_reported_as_locked(desk_data):
    # The HUBC series is generated to close exactly on its upper cap.
    packet = build_market_packet("HUBC", TRADE_DATE, data=desk_data)
    assert packet.circuit.locked.value in {"upper", "none"}  # caps come from the file
    if packet.quote.prev_close_pkr:
        expected = compute_circuit_state(
            close=packet.quote.close_pkr,
            prev_close=packet.quote.prev_close_pkr,
            band_pct=desk_data.config.circuit_band_pct,
        )
        assert packet.circuit.upper_cap_pkr >= expected.lower_cap_pkr


def test_missing_board_is_backfilled_from_the_timeseries(config, desk_data):
    # Only the EOD series is available: the packet must still be usable and say
    # what is missing.
    sparse = {
        "https://dps.psx.com.pk/timeseries/eod/SYS": eod_payload("SYS"),
        "https://www.sbp.org.pk/": "<html><body>down</body></html>",
    }
    client = PortalClient(
        config,
        transport=FixtureTransport(sparse),
        cache=DiskCache(config.cache_dir),
        sleep=lambda _s: None,
    )
    from psx_desk.dataflows.ingest import DeskData
    from psx_desk.dataflows.portal import DumpStore

    data = DeskData(client=client, config=config, dumps=DumpStore(client))
    packet = build_market_packet("SYS", TRADE_DATE, data=data)
    assert packet.quote.close_pkr == pytest.approx(PRICES["SYS"])
    assert packet.quote.upper_cap_pkr is None
    assert "exchange_circuit_caps" in packet.missing_fields
    assert packet.data_quality in {DataQuality.PARTIAL, DataQuality.STALE}
    assert any("board file" in note for note in packet.notes)


def test_no_price_at_all_is_a_hard_failure(config, desk_data):
    client = PortalClient(
        config, transport=FixtureTransport({}), cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    from psx_desk.dataflows.ingest import DeskData
    from psx_desk.dataflows.portal import DumpStore

    data = DeskData(client=client, config=config, dumps=DumpStore(client))
    with pytest.raises(IngestionError) as excinfo:
        build_market_packet("SYS", TRADE_DATE, data=data)
    assert "no usable price" in str(excinfo.value)


def test_unverified_defaulters_downgrade_the_packet(config):
    world = World()
    client = PortalClient(
        config, transport=world.transport, cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    from psx_desk.dataflows.ingest import DeskData
    from psx_desk.dataflows.portal import DumpStore

    data = DeskData(client=client, config=config, dumps=DumpStore(client))
    # Sabotage just the defaulter portal.
    world.transport._map.pop("https://www.psx.com.pk/psx/resources-and-tools/Defaulting-Companies-Profile-Portal")
    packet = build_market_packet("SYS", TRADE_DATE, data=data)
    assert packet.defaulter.checked is False
    assert packet.defaulter.segment == "unknown"
    assert packet.data_quality is not DataQuality.VERIFIED
    assert any("UNKNOWN" in note for note in packet.notes)
