"""Defaulters segment and PUCARS: the checks that can veto a trade by themselves."""

from __future__ import annotations

from psx_desk.dataflows.disclosures import (
    load_defaulter_index,
    normalize_company,
    parse_defaulter_page,
)
from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient
from tests.conftest import TRADE_DATE, defaulter_html


def test_company_normalisation_folds_legal_suffixes():
    assert normalize_company("Systems Limited") == normalize_company("SYSTEMS LTD.")
    assert normalize_company("Hub Power Company Limited") == "hub power"


def test_defaulters_page_splits_current_from_past():
    current, past, note = parse_defaulter_page(defaulter_html())
    assert "worldcall telecom" in current
    assert "dewan salman fibre" in past
    assert note == ""


def test_a_listed_company_is_flagged_as_defaulting(client):
    index = load_defaulter_index(client, as_of=TRADE_DATE)
    assert index.checked
    verdict = index.check("WTL", "Worldcall Telecom Limited")
    assert verdict.is_defaulting
    assert verdict.segment == "non_compliant"
    assert "Current Non-Compliant" in verdict.evidence


def test_a_clean_company_passes_with_evidence(client):
    index = load_defaulter_index(client, as_of=TRADE_DATE)
    verdict = index.check("SYS", "Systems Limited")
    assert verdict.checked
    assert not verdict.is_defaulting
    assert verdict.segment == "regular"


def test_an_unreadable_portal_is_unknown_not_clean(config):
    blank = PortalClient(
        config, transport=FixtureTransport({}), cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )
    index = load_defaulter_index(blank, as_of=TRADE_DATE)
    assert index.checked is False
    verdict = index.check("SYS", "Systems Limited")
    assert verdict.checked is False
    assert verdict.segment == "unknown"
    assert verdict.is_defaulting is False  # unknown is not an accusation
    assert "unverified" in verdict.evidence.lower()


def test_a_page_without_sections_is_rejected():
    current, past, note = parse_defaulter_page("<html><body><p>Maintenance</p></body></html>")
    assert not current and not past
    assert "no company names" in note


def test_announcements_are_matched_to_the_issuer(client):
    from psx_desk.dataflows.disclosures import fetch_disclosures

    items = fetch_disclosures("SYS", client, company_name="Systems Limited", as_of=TRADE_DATE)
    titles = " ".join(item.title for item in items)
    assert "Financial Results" in titles
    assert all("another issuer" not in item.title for item in items)
    assert items[0].category == "financial_results"
    assert items[0].url.startswith("https://www.psx.com.pk/")
