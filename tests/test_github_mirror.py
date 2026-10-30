"""The GitHub mirror tier: schemas, access paths, provenance and policy gates.

Payload shapes here are the ones the two verified repositories actually serve
(``bigbajwatv-alt/psx-data`` JSON snapshots and
``Muhammad-Wasif/PSX-Stock-Market-Dataset`` CSV history).  The *numbers* are
illustrative, the structure is not.  Nothing in this file touches the network.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from datetime import date

import pytest

from psx_desk.config import load_config
from psx_desk.dataflows.github_mirror import (
    DEFAULT_HISTORY_MIRROR,
    DEFAULT_SNAPSHOT_MIRROR,
    MIRROR_PRESETS,
    MirrorNotConfigured,
    MirrorUnavailable,
    fetch_mirror_history,
    mirror_available,
    mirror_catalog,
    mirror_indices,
    mirror_symbol_info,
    mirror_urls,
    parse_history_csv,
    parse_indices,
    parse_snapshot_board,
    parse_symbol_directory,
    resolve_mirror,
    scrape_psx_mirror,
)
from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient
from psx_desk.sources import MIRROR_HOSTS, assert_source_allowed, mirror_url

SNAPSHOT_REPO = "bigbajwatv-alt/psx-data"
HISTORY_REPO = "Muhammad-Wasif/PSX-Stock-Market-Dataset"
TRADE_DATE = date(2026, 10, 29)

BOARD_JSON = {
    "as_of": "2026-10-29",
    "source": "https://dps.psx.com.pk/market-watch",
    "count": 3,
    "schema": {"sym": "symbol", "ldcp": "last day close"},
    "stocks": [
        {
            "sym": "SYS",
            "sec_code": "082600",
            "listed": True,
            "ldcp": 405.0,
            "open": 406.0,
            "high": 418.0,
            "low": 404.5,
            "close": 412.55,
            "change": 7.55,
            "pct": 1.8642,
            "volume": 1_250_000,
        },
        {
            "sym": "ENGRO",
            "sec_code": "071200",
            "listed": True,
            "ldcp": 283.0,
            "open": 283.5,
            "high": 287.9,
            "low": 282.1,
            "close": 285.1,
            "change": 2.1,
            "pct": 0.742,
            "volume": 640_000,
        },
        {
            "sym": "LUCK",
            "sec_code": "020800",
            "listed": True,
            "ldcp": 1205.0,
            "open": 1206.0,
            "high": 1216.0,
            "low": 1201.0,
            "close": 1210.0,
            "change": 5.0,
            "pct": 0.4149,
            "volume": 210_000,
        },
    ],
}

SYMBOLS_JSON = {
    "as_of": "2026-10-29",
    "source": "https://dps.psx.com.pk/symbols",
    "count": 3,
    "symbols": [
        {
            "symbol": "SYS",
            "name": "Systems Limited",
            "sectorName": "TECHNOLOGY & COMMUNICATION",
            "isETF": False,
            "isDebt": False,
        },
        {
            "symbol": "ENGRO",
            "name": "Engro Corporation Limited",
            "sectorName": "FERTILIZER",
            "isETF": False,
            "isDebt": False,
        },
        {
            "symbol": "LUCK",
            "name": "Lucky Cement Limited",
            "sectorName": "CEMENT",
            "isETF": False,
            "isDebt": False,
        },
    ],
}

INDICES_JSON = {
    "as_of": "2026-10-29",
    "source": "https://dps.psx.com.pk/timeseries/eod/KSE100",
    "indices": {
        "KSE100": {
            "ts": "2026-10-29",
            "close": 81_500.11,
            "prev_close": 81_200.0,
            "change": 300.11,
            "pct": 0.3696,
            "open": 81_250.0,
            "volume": 210_000_000,
        }
    },
}

SNAPSHOT_JSON = {
    "timestamp": "2026-10-29T15:30:26+05:00",
    "source": "dps.psx.com.pk",
    "market_status": "open",
    "watchlist": {
        "SYS": {
            "symbol": "SYS",
            "close": 412.55,
            "prev_close": 405.0,
            "change": 7.55,
            "change_pct": 1.8642,
            "volume": 1_250_000,
            "open": 406.0,
            "day_high": 418.0,
            "day_low": 404.5,
            "as_of": "2026-10-29",
            "source": "market_watch",
        }
    },
}

HISTORY_CSV = """company_name,symbol,date,open,high,low,close,volume,is_anomaly
Systems Limited,SYS,2026-10-23,394.0,401.0,392.5,398.0,880000,False
Systems Limited,SYS,2026-10-26,400.0,410.0,398.0,405.0,900000,False
Systems Limited,SYS,2026-10-27,405.0,412.0,404.0,408.5,950000,False
Systems Limited,SYS,2026-10-28,408.5,415.0,407.0,411.0,1000000,False
Systems Limited,SYS,2026-10-29,411.0,417.0,409.0,412.55,1250000,False
Systems Limited,SYS,2026-10-30,412.55,412.55,412.55,412.55,0,True
"""

COMBINED_CSV = HISTORY_CSV + """Engro Corporation Limited,ENGRO,2026-10-28,282.0,286.0,281.0,283.0,600000,False
Engro Corporation Limited,ENGRO,2026-10-29,283.5,287.9,282.1,285.1,640000,False
"""

MAPPING_CSV = """symbol,full_company_name,filename
SYS,Systems Limited,Systems Limited.csv
ENGRO,Engro Corporation Limited,Engro Corporation Limited.csv
"""


def snapshot_urls() -> dict[str, str]:
    return {
        name: mirror_url(SNAPSHOT_REPO, "main", name, via="raw")
        for name in ("daily_summary.json", "symbols.json", "indices_eod.json", "latest_snapshot.json")
    }


def snapshot_payloads(*, board: dict | str | None = BOARD_JSON) -> dict[str, str]:
    """The mirror's files as served by ``raw.githubusercontent.com``."""

    urls = snapshot_urls()
    payloads: dict[str, str] = {
        urls["symbols.json"]: json.dumps(SYMBOLS_JSON),
        urls["indices_eod.json"]: json.dumps(INDICES_JSON),
    }
    if board is not None:
        payloads[urls["daily_summary.json"]] = (
            board if isinstance(board, str) else json.dumps(board)
        )
    return payloads


def mirror_client(config, payloads: dict[str, str | bytes], **kwargs) -> PortalClient:
    transport = FixtureTransport(payloads, **kwargs)
    return PortalClient(
        config,
        transport=transport,
        cache=DiskCache(config.cache_dir),
        sleep=lambda _s: None,
    )


def mirror_config(config, **overrides):
    base = {
        "mirror_policy": "fallback",
        "github_mirror": DEFAULT_SNAPSHOT_MIRROR,
        "github_history_mirror": DEFAULT_HISTORY_MIRROR,
    }
    base.update(overrides)
    return load_config(work_dir=config.work_dir, throttle_seconds=0.0, http_retries=1, **base)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_both_verified_repositories_are_presets():
    assert set(MIRROR_PRESETS) == {"psx-data", "psx-history"}
    catalog = {entry["key"]: entry for entry in mirror_catalog()}
    assert catalog["psx-data"]["kind"] == "snapshot"
    assert catalog["psx-history"]["kind"] == "history"
    assert catalog["psx-history"]["repo"] == HISTORY_REPO


def test_resolve_mirror_understands_keys_and_repo_strings():
    assert resolve_mirror("psx-data").repo == SNAPSHOT_REPO
    assert resolve_mirror("github:psx-history").repo == HISTORY_REPO
    generic = resolve_mirror("someone/psx-fork")
    assert generic.repo == "someone/psx-fork"
    assert generic.kind == "snapshot"
    with pytest.raises(MirrorNotConfigured):
        resolve_mirror(None)
    with pytest.raises(MirrorNotConfigured):
        resolve_mirror("not a mirror")


def test_every_mirror_url_is_a_github_host_only():
    for preset in MIRROR_PRESETS.values():
        for key, url in mirror_urls(preset).items():
            host = url.split("/")[2]
            assert host in MIRROR_HOSTS, key
            assert assert_source_allowed(url, allow_mirror=True) == url
            with pytest.raises(ValueError):
                assert_source_allowed(url)  # mirrors are not on the default list


def test_policy_off_refuses_to_scrape_a_mirror(config):
    with pytest.raises(MirrorNotConfigured):
        scrape_psx_mirror(config=config, client=mirror_client(config, snapshot_payloads()))
    assert config.mirror_policy == "off"


def test_a_configured_policy_still_needs_a_mirror():
    with pytest.raises(MirrorNotConfigured):
        resolve_mirror(None)


# --------------------------------------------------------------------------- #
# Board parsing
# --------------------------------------------------------------------------- #


def test_daily_summary_schema_becomes_board_rows(config):
    clients = mirror_client(config, snapshot_payloads())
    dump = scrape_psx_mirror("psx-data", config=mirror_config(config), client=clients)

    assert set(dump.rows) == {"SYS", "ENGRO", "LUCK"}
    sys_row = dump.rows["SYS"]
    assert sys_row.close == pytest.approx(412.55)
    assert sys_row.prev_close == pytest.approx(405.0)
    assert sys_row.change == pytest.approx(7.55)
    assert sys_row.pct_change == pytest.approx(1.8642)
    assert sys_row.volume == 1_250_000
    assert sys_row.high == pytest.approx(418.0)
    assert sys_row.value == pytest.approx(412.55 * 1_250_000, rel=1e-6)
    # Reference metadata is merged in from symbols.json, never from thin air.
    assert sys_row.sector == "TECHNOLOGY & COMMUNICATION"
    assert sys_row.company_name == "Systems Limited"


def test_latest_snapshot_schema_is_also_understood(config):
    urls = snapshot_urls()
    payloads = {urls["latest_snapshot.json"]: json.dumps(SNAPSHOT_JSON)}
    dump = scrape_psx_mirror(
        "psx-data", config=mirror_config(config), client=mirror_client(config, payloads)
    )
    assert dump.rows["SYS"].close == pytest.approx(412.55)
    assert dump.rows["SYS"].high == pytest.approx(418.0)
    assert dump.rows["SYS"].prev_close == pytest.approx(405.0)
    assert dump.mirror_as_of == TRADE_DATE


def test_mirror_dump_is_labelled_and_never_claims_exchange_provenance(config):
    dump = scrape_psx_mirror(
        "psx-data", config=mirror_config(config), client=mirror_client(config, snapshot_payloads())
    )
    assert dump.mirror == SNAPSHOT_REPO
    assert dump.from_mirror is True
    assert dump.mirror_as_of == TRADE_DATE
    assert dump.datasets_used == (f"mirror:{SNAPSHOT_REPO}",)
    assert dump.sources, "mirror fetches must be citable"
    assert all(ref.source_kind == "mirror" for ref in dump.sources)
    assert all(var in dump.missing_fields for var in ("free_float_mcap", "trades", "exchange_circuit_caps"))
    assert any("not exchange-verified" in note for note in dump.rejected)


def test_snapshot_older_than_the_requested_session_is_called_out(config):
    dump = scrape_psx_mirror(
        "psx-data",
        config=mirror_config(config),
        client=mirror_client(config, snapshot_payloads()),
        as_of=date(2026, 10, 30),
    )
    assert any("older than the requested session" in note for note in dump.rejected)
    assert dump.mirror_as_of == TRADE_DATE


def test_the_freshest_dated_board_file_wins(config):
    """A repo can hold a stale daily_summary and a newer latest_snapshot."""

    urls = snapshot_urls()
    stale = json.loads(json.dumps(BOARD_JSON))
    stale["as_of"] = "2026-05-22"
    stale["stocks"][0]["close"] = 151.0
    fresh = json.loads(json.dumps(SNAPSHOT_JSON))
    fresh["timestamp"] = "2026-10-29T15:30:26+05:00"
    payloads = {
        urls["daily_summary.json"]: json.dumps(stale),
        urls["latest_snapshot.json"]: json.dumps(fresh),
    }
    dump = scrape_psx_mirror(
        "psx-data", config=mirror_config(config), client=mirror_client(config, payloads)
    )
    assert dump.mirror_as_of == TRADE_DATE
    assert dump.rows["SYS"].close == pytest.approx(412.55), "the fresh file must win"


def test_a_snapshot_beyond_the_staleness_budget_is_refused(config):
    urls = snapshot_urls()
    stale = json.loads(json.dumps(BOARD_JSON))
    stale["as_of"] = "2026-05-22"
    with pytest.raises(MirrorUnavailable) as excinfo:
        scrape_psx_mirror(
            "psx-data",
            config=mirror_config(config),
            client=mirror_client(config, {urls["daily_summary.json"]: json.dumps(stale)}),
            as_of=date(2026, 10, 29),
        )
    assert "days behind" in str(excinfo.value)

    # The budget is configuration, not a hard-coded constant.
    lenient = mirror_config(config, mirror_max_staleness_days=200)
    dump = scrape_psx_mirror(
        "psx-data",
        config=lenient,
        client=mirror_client(lenient, {urls["daily_summary.json"]: json.dumps(stale)}),
        as_of=date(2026, 10, 29),
    )
    assert dump.mirror_as_of == date(2026, 5, 22)
    assert any("older than the requested session" in note for note in dump.rejected)


def test_missing_scrips_are_absent_not_invented(config):
    with pytest.raises(MirrorUnavailable):
        scrape_psx_mirror(
            "psx-data",
            symbols=["NOTLISTED"],
            config=mirror_config(config),
            client=mirror_client(config, snapshot_payloads()),
        )


def test_a_mirror_with_no_board_file_explains_itself(config):
    with pytest.raises(MirrorUnavailable) as excinfo:
        scrape_psx_mirror(
            "psx-data",
            config=mirror_config(config),
            client=mirror_client(config, snapshot_payloads(board=None)),
        )
    assert "no usable board" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Access paths
# --------------------------------------------------------------------------- #


def test_raw_404_falls_back_to_the_contents_api(config):
    api_url = mirror_url(SNAPSHOT_REPO, "main", "daily_summary.json", via="api")
    body = json.dumps(BOARD_JSON).encode()
    api_payload = json.dumps(
        {
            "name": "daily_summary.json",
            "path": "daily_summary.json",
            "size": len(body),
            "encoding": "base64",
            "content": base64.b64encode(body).decode(),
            "sha": "0" * 40,
        }
    )
    client = mirror_client(config, {api_url: api_payload})
    dump = scrape_psx_mirror("psx-data", config=mirror_config(config), client=client)

    assert dump.rows["SYS"].close == pytest.approx(412.55)
    raw_url = mirror_url(SNAPSHOT_REPO, "main", "daily_summary.json", via="raw")
    assert raw_url in client.probes and api_url in client.probes
    # The provenance points at the API, which is where the bytes actually came from.
    assert any("GitHub API" in ref.label for ref in dump.sources)


def test_contents_api_refuses_a_file_it_cannot_inline(config):
    api_url = mirror_url(SNAPSHOT_REPO, "main", "daily_summary.json", via="api")
    payload = json.dumps({"size": 12_623_735, "encoding": "none", "content": ""})
    client = mirror_client(config, {api_url: payload})
    with pytest.raises(MirrorUnavailable) as excinfo:
        scrape_psx_mirror(
            "psx-data", config=mirror_config(config), client=client, via="api"
        )
    assert "tarball" in str(excinfo.value)


def test_tarball_access_reads_a_member(config):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        blob = json.dumps(BOARD_JSON).encode()
        info = tarfile.TarInfo("psx-data-main/daily_summary.json")
        info.size = len(blob)
        archive.addfile(info, io.BytesIO(blob))
    tarball_url = mirror_url(SNAPSHOT_REPO, "main", "", via="tarball")
    client = mirror_client(config, {tarball_url: buffer.getvalue()})

    dump = scrape_psx_mirror(
        "psx-data", config=mirror_config(config), client=client, via="tarball"
    )
    assert dump.rows["SYS"].close == pytest.approx(412.55)
    assert client.probes == [tarball_url], "one request should serve every file"


def test_every_access_path_failing_is_reported_with_reasons(config):
    with pytest.raises(MirrorUnavailable) as excinfo:
        scrape_psx_mirror(
            "psx-data", config=mirror_config(config), client=mirror_client(config, {})
        )
    message = str(excinfo.value)
    assert "raw" in message and "api" in message and "tarball" in message


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #


def test_symbol_directory_and_indices(config):
    client = mirror_client(config, snapshot_payloads())
    cfg = mirror_config(config)
    info = mirror_symbol_info("psx-data", config=cfg, client=client)
    assert info["LUCK"].sector == "CEMENT"
    assert info["LUCK"].is_etf is False

    indices = mirror_indices("psx-data", config=cfg, client=client)
    assert [snap.symbol for snap in indices] == ["KSE100"]
    assert indices[0].close == pytest.approx(81_500.11)
    assert indices[0].change_pct == pytest.approx(0.3696)
    assert indices[0].as_of == TRADE_DATE


def test_csv_symbol_directory_is_accepted():
    directory = parse_symbol_directory(
        "sector_code,sector_name,advance,decline,unchanged,turnover,market_cap_b\n"
        "0804,CEMENT,9,9,0,19621715,1511.68\n"
    )
    assert directory == {}  # a sectors summary is not a symbol directory


def test_index_payload_without_indices_is_empty_not_fatal():
    assert parse_indices(json.dumps({"as_of": "2026-10-29"})) == []


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_history_csv_parses_oldest_first_and_drops_anomalies():
    bars, dropped = parse_history_csv(HISTORY_CSV, symbol="SYS")
    assert dropped == 1
    assert [bar.trade_date.isoformat() for bar in bars] == [
        "2026-10-23",
        "2026-10-26",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
    ]
    assert bars[-1].close == pytest.approx(412.55)
    assert bars[-1].high == pytest.approx(417.0)


def test_history_is_truncated_at_the_requested_session():
    bars, _ = parse_history_csv(HISTORY_CSV, symbol="SYS", as_of=date(2026, 10, 27))
    assert bars[-1].trade_date == date(2026, 10, 27)
    assert all(bar.trade_date <= date(2026, 10, 27) for bar in bars)


def test_history_window_keeps_the_most_recent_bars():
    bars, _ = parse_history_csv(HISTORY_CSV, symbol="SYS", days=2)
    assert len(bars) == 2
    assert bars[-1].trade_date == TRADE_DATE


def test_history_fetches_the_per_company_file_via_the_symbol_map(config):
    payloads = {
        mirror_url(HISTORY_REPO, "main", "metadata/PSX_Company_Name_To_Symbol_Mapping.csv", via="raw"): MAPPING_CSV,
        mirror_url(HISTORY_REPO, "main", "stocks/Systems Limited.csv", via="raw"): HISTORY_CSV,
    }
    client = mirror_client(config, payloads)
    history = fetch_mirror_history("sys", config=mirror_config(config), client=client)

    assert history.symbol == "SYS"
    assert len(history.bars) == 5
    assert history.bars[-1].close == pytest.approx(412.55)
    assert history.quality.value == "partial"
    assert history.source is not None and history.source.source_kind == "mirror"
    assert history.bars[0].trade_date == date(2026, 10, 23)


def test_history_falls_back_to_the_combined_file(config):
    payloads = {
        mirror_url(HISTORY_REPO, "main", "metadata/PSX_Company_Name_To_Symbol_Mapping.csv", via="raw"): MAPPING_CSV,
        mirror_url(HISTORY_REPO, "main", "combined/PSX_KSE100_Full_Historical_Daily.csv", via="raw"): COMBINED_CSV,
    }
    history = fetch_mirror_history(
        "ENGRO", config=mirror_config(config), client=mirror_client(config, payloads)
    )
    assert len(history.bars) == 2
    assert history.bars[-1].close == pytest.approx(285.1)


def test_history_for_a_symbol_the_dataset_does_not_carry_raises(config):
    payloads = {
        mirror_url(HISTORY_REPO, "main", "metadata/PSX_Company_Name_To_Symbol_Mapping.csv", via="raw"): MAPPING_CSV,
    }
    with pytest.raises(MirrorUnavailable):
        fetch_mirror_history(
            "NESTLE", config=mirror_config(config), client=mirror_client(config, payloads)
        )


def test_history_policy_off_is_refused(config):
    with pytest.raises(MirrorNotConfigured):
        fetch_mirror_history("SYS", config=config)


# --------------------------------------------------------------------------- #
# Derived helpers
# --------------------------------------------------------------------------- #


def test_mirror_available_returns_false_instead_of_raising(config):
    assert mirror_available("psx-data", config=mirror_config(config), client=mirror_client(config, {})) is False
    assert (
        mirror_available(
            "psx-data",
            config=mirror_config(config),
            client=mirror_client(config, snapshot_payloads()),
        )
        is True
    )


def test_board_parser_rejects_non_json():
    with pytest.raises(MirrorUnavailable):
        parse_snapshot_board("<html>not json</html>")
    with pytest.raises(MirrorUnavailable):
        parse_snapshot_board(json.dumps({"unexpected": True}))


# --------------------------------------------------------------------------- #
# DeskWide integration: the policy gates the node, not just the scraper
# --------------------------------------------------------------------------- #


def full_mirror_payloads() -> dict[str, str]:
    payloads = snapshot_payloads()
    payloads.update(
        {
            mirror_url(HISTORY_REPO, "main", "metadata/PSX_Company_Name_To_Symbol_Mapping.csv", via="raw"): MAPPING_CSV,
            mirror_url(HISTORY_REPO, "main", "stocks/Systems Limited.csv", via="raw"): HISTORY_CSV,
        }
    )
    return payloads


def test_fallback_policy_produces_a_partial_packet_from_mirror_data(config):
    from psx_desk.dataflows.ingest import build_desk_data, build_market_packet

    cfg = mirror_config(config)
    client = mirror_client(cfg, full_mirror_payloads())
    packet = build_market_packet("SYS", TRADE_DATE, data=build_desk_data(cfg, client=client))

    assert packet.quote.close_pkr == pytest.approx(412.55)
    assert packet.quote.close_pkr == pytest.approx(packet.history[-1]["close"])
    assert packet.data_quality.value == "partial", "mirror data can never be verified"
    assert "exchange_verified_prices" in packet.missing_fields
    assert "exchange_verified_history" in packet.missing_fields
    joined = " ".join(packet.notes)
    assert "GitHub mirror" in joined
    assert any(ref.source_kind == "mirror" for ref in packet.sources)
    assert any(SNAPSHOT_REPO in ref.url or HISTORY_REPO in ref.url for ref in packet.sources)


def test_off_policy_never_touches_a_configured_mirror(config):
    from psx_desk.dataflows.ingest import IngestionError, build_desk_data, build_market_packet

    cfg = mirror_config(config, mirror_policy="off")
    client = mirror_client(cfg, full_mirror_payloads())
    with pytest.raises(IngestionError):
        build_market_packet("SYS", TRADE_DATE, data=build_desk_data(cfg, client=client))
    assert [url for url in client.probes if "github" not in url] == client.probes
    assert not any("github" in url for url in client.probes)


def test_backfill_lets_a_mirror_name_a_sector_but_never_price_a_scrip(config, world):
    from psx_desk.dataflows.http import FixtureTransport as Ft
    from psx_desk.dataflows.ingest import build_desk_data, build_market_packet
    from tests.conftest import PRICES

    cfg = mirror_config(config, mirror_policy="backfill", github_mirror="psx-data")
    liar = json.loads(json.dumps(BOARD_JSON))
    for entry in liar["stocks"]:
        entry["close"] = 999.0  # a mirror price that must never reach the packet

    mapping: dict[str, str | bytes] = dict(world.transport._map)  # noqa: SLF001 - test seam
    mapping.update(snapshot_payloads(board=liar))
    client = PortalClient(
        cfg,
        transport=Ft(mapping),
        cache=DiskCache(cfg.cache_dir),
        sleep=lambda _s: None,
    )
    packet = build_market_packet("SYS", TRADE_DATE, data=build_desk_data(cfg, client=client))

    assert packet.quote.close_pkr == pytest.approx(PRICES["SYS"]), "PSX price wins"
    assert packet.quote.close_pkr != pytest.approx(999.0)
    assert packet.quote.sector == "TECHNOLOGY & COMMUNICATION", "sector backfilled from the mirror"
    assert not any(ref.source_kind == "mirror" and "daily_summary" in ref.url for ref in packet.sources)


def test_a_stale_bar_may_not_become_the_session_close(config):
    """Live failure mode: mirror history ends 2026-10-08, session is 2026-10-29."""

    from psx_desk.dataflows.ingest import IngestionError, build_desk_data, build_market_packet

    cfg = mirror_config(config, github_mirror="psx-data")
    payloads = {
        mirror_url(HISTORY_REPO, "main", "metadata/PSX_Company_Name_To_Symbol_Mapping.csv", via="raw"): MAPPING_CSV,
        mirror_url(HISTORY_REPO, "main", "stocks/Systems Limited.csv", via="raw"): HISTORY_CSV,
    }
    client = mirror_client(cfg, payloads)
    with pytest.raises(IngestionError) as excinfo:
        build_market_packet(
            "SYS", date(2026, 11, 24), data=build_desk_data(cfg, client=client)
        )
    assert "stale close" in str(excinfo.value)

    # ... while a session within the lag budget is allowed, with a note.
    packet = build_market_packet(
        "SYS", date(2026, 11, 2), data=build_desk_data(cfg, client=client)
    )
    assert packet.quote.close_pkr == pytest.approx(412.55)
    assert any("Close carried from the 2026-10-29 bar" in note for note in packet.notes)
