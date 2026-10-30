"""The PSX client: politeness, retries, caching, offline replay and rejection rules."""

from __future__ import annotations

import time

import pytest

from psx_desk.config import load_config
from psx_desk.dataflows.http import (
    DiskCache,
    FixtureTransport,
    HttpStatusError,
    NetworkError,
    OfflineDataMissing,
    PayloadRejected,
    PortalClient,
    RawResponse,
    require_csv_like,
    require_not_html,
)
from psx_desk.schemas import SourceRef
from psx_desk.sources import MARKET_WATCH_URL, SourceKind

URL = "https://dps.psx.com.pk/download/symbol_price/2026-10-29.zip"


class FlakyTransport:
    """Answers with a scripted status sequence, recording every call."""

    def __init__(self, statuses: list[int], body: bytes = b"SYMBOL,CLOSE\nSYS,412.55\n") -> None:
        self.statuses = list(statuses)
        self.body = body
        self.calls: list[str] = []

    def get(self, url, *, timeout, headers):
        self.calls.append(url)
        status = self.statuses.pop(0) if self.statuses else 200
        if status == 0:
            raise NetworkError("connection reset")
        return RawResponse(url=url, status=status, content=self.body if status == 200 else b"")


def client_for(config, transport, *, sleeps: list[float] | None = None) -> PortalClient:
    return PortalClient(
        config,
        transport=transport,
        cache=DiskCache(config.cache_dir),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def test_404_returns_none_when_the_caller_allows_a_miss(config):
    client = client_for(config, FlakyTransport([404]))
    assert client.get(URL, allow_missing=True) is None


def test_404_raises_when_the_payload_is_required(config):
    client = client_for(config, FlakyTransport([404]))
    with pytest.raises(HttpStatusError):
        client.get(URL, allow_missing=False)


def test_transient_errors_are_retried_with_backoff(config):
    retrying = load_config(work_dir=config.work_dir, http_retries=3, throttle_seconds=0.0)
    sleeps: list[float] = []
    transport = FlakyTransport([503, 200])
    client = client_for(retrying, transport, sleeps=sleeps)
    payload = client.get(URL)
    assert payload is not None
    assert len(transport.calls) == 2
    assert sleeps  # a backoff was applied


def test_network_errors_surface_after_the_retry_budget(config):
    retrying = load_config(work_dir=config.work_dir, http_retries=3, throttle_seconds=0.0)
    client = client_for(retrying, FlakyTransport([0, 0, 0, 0]), sleeps=[])
    with pytest.raises(NetworkError):
        client.get(URL)


def test_second_call_is_served_from_the_cache(config):
    transport = FlakyTransport([200])
    client = client_for(config, transport)
    first = client.get(URL)
    second = client.get(URL)
    assert len(transport.calls) == 1
    assert first is not None and second is not None
    assert first.from_cache is False and second.from_cache is True
    assert first.sha256 == second.sha256


def test_offline_mode_refuses_to_invent_a_payload(config):
    offline_config = load_config(work_dir=config.work_dir, offline=True)
    client = client_for(offline_config, FlakyTransport([200]))
    with pytest.raises(OfflineDataMissing):
        client.get(URL)


def test_offline_mode_replays_what_was_cached(config):
    live = client_for(config, FlakyTransport([200]))
    assert live.get(URL) is not None

    offline_config = load_config(work_dir=config.work_dir, offline=True, cache_ttl_seconds=900)
    replay = client_for(offline_config, FlakyTransport([500]))
    payload = replay.get(URL)
    assert payload is not None
    assert payload.from_cache is True


def test_cache_ttl_expiry_forces_a_refetch(config):
    short = load_config(work_dir=config.work_dir, cache_ttl_seconds=1)
    transport = FlakyTransport([200, 200])
    client = client_for(short, transport)
    client.get(URL, ttl_seconds=1)
    time.sleep(1.1)
    client.get(URL, ttl_seconds=1)
    assert len(transport.calls) == 2


def test_foreign_hosts_are_refused_before_any_request(config):
    transport = FlakyTransport([200])
    client = client_for(config, transport)
    with pytest.raises(ValueError):
        client.get("https://finnhub.io/api/v1/quote?symbol=SYS")
    assert transport.calls == []


def test_payload_carries_an_auditable_source_ref(config):
    client = client_for(config, FlakyTransport([200]))
    payload = client.get(URL, kind=SourceKind.DAILY_DOWNLOAD, as_of=None, label="caps")
    ref = payload.source_ref()
    assert isinstance(ref, SourceRef)
    assert ref.url == URL
    assert len(ref.sha256) == 64
    assert ref.label == "caps"


def test_probe_reports_why_each_candidate_failed(config):
    client = client_for(config, FlakyTransport([404, 503, 200]))
    payload, rejected = client.probe([URL, URL + "x", URL + "y"])
    assert payload is not None
    assert len(rejected) == 2
    assert all("::" in entry for entry in rejected)


def test_html_is_rejected_on_a_data_endpoint(config):
    html = b"<!doctype html><html><body>maintenance</body></html>"
    client = client_for(config, FlakyTransport([200], body=html))
    payload = client.get(URL)
    with pytest.raises(PayloadRejected):
        require_not_html(payload)


def test_html_is_allowed_on_a_page_endpoint(config):
    html = b"<!doctype html><html><body><table></table></body></html>"
    client = client_for(config, FlakyTransport([200], body=html))
    payload = client.get(MARKET_WATCH_URL, ttl_seconds=None)
    require_not_html(payload)  # must not raise for an HTML page we asked for


def test_a_short_or_unlabelled_file_is_not_accepted_as_csv(config):
    client = client_for(config, FlakyTransport([200], body=b"SYMBOL\nSYS\n"))
    payload = client.get(URL)
    with pytest.raises(PayloadRejected):
        require_csv_like(payload)

    no_header = client_for(config, FlakyTransport([200], body=b"just some words\n" * 8))
    with pytest.raises(PayloadRejected):
        require_csv_like(no_header.get(URL))


def test_fixture_transport_records_every_probe():
    transport = FixtureTransport({URL: b"SYMBOL,CLOSE\nSYS,412.55\n"})
    assert transport.get(URL, timeout=1, headers={}).status == 200
    assert transport.get(URL + "missing", timeout=1, headers={}).status == 404
    assert transport.probes == [URL, URL + "missing"]
