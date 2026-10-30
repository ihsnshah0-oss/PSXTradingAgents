"""Guardrails: this repository is PSX-only, by inspection as well as by design."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from psx_desk.sources import ALLOWED_HOSTS, MIRROR_HOSTS, assert_source_allowed

PACKAGE = Path(__file__).resolve().parent.parent / "psx_desk"
FORBIDDEN_MODULES = {
    "yfinance",
    "finnhub",
    "alpha_vantage",
    "sec_edgar",
    "polygon",
    "pandas_datareader",
    "stockstats",
    "reddit",
    "praw",
    "stocktwits",
}
FORBIDDEN_HOSTS = (
    "yahoo.com",
    "finnhub.io",
    "sec.gov",
    "alphavantage.co",
    "polygon.io",
    "nasdaq.com",
    "reuters.com",
    "bloomberg.com",
    "fred.stlouisfed.org",
    "reddit.com",
    "stocktwits.com",
)
URL_RE = re.compile(r"https?://[^\s\"'`)]+")


def python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_no_foreign_data_vendor_is_imported():
    offenders = []
    for path in python_files():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in FORBIDDEN_MODULES:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_every_url_in_the_package_is_an_allowed_source():
    offenders = []
    for path in python_files():
        for url in URL_RE.findall(path.read_text("utf-8")):
            if any(host in url for host in FORBIDDEN_HOSTS):
                offenders.append(f"{path.name}: {url}")
            if "${" in url or "{" in url:  # templates are checked where they are built
                continue
            try:
                assert_source_allowed(url.rstrip(".,;"))
            except ValueError:
                offenders.append(f"{path.name}: {url}")
    assert offenders == []


def test_allow_list_is_only_psx_and_sbp():
    assert all("psx.com.pk" in host or "sbp.org.pk" in host for host in ALLOWED_HOSTS)


def test_prompts_prime_the_karachi_desk():
    from psx_desk.agents import prompts

    corpus = " ".join(
        [
            prompts.DESK_IDENTITY,
            prompts.HOUSE_RULES,
            prompts.TECHNICAL_SYSTEM,
            prompts.FUNDAMENTAL_SYSTEM,
            prompts.BULL_SYSTEM,
            prompts.BEAR_SYSTEM,
            prompts.CRO_SYSTEM,
        ]
    )
    for needle in (
        "Karachi",
        "Pakistan Stock Exchange",
        "PSX",
        "SBP",
        "circuit",
        "Defaulters",
        "Ready market",
        "PKR",
    ):
        assert needle in corpus, needle
    # The US framing the desk replaced may appear only inside the explicit
    # prohibition ("do not cite ..."), never as an allowed source.
    reflowed = " ".join(corpus.split())
    sentences = re.split(r"(?<=[.!?])\s+", reflowed)
    for forbidden in ("S&P 500", "Federal Reserve", "SEC filing", "10-K", "NYSE"):
        for sentence in sentences:
            if forbidden not in sentence:
                continue
            assert (
                "Do not cite" in sentence
                or "no access to" in sentence
                or "not against" in sentence
            ), sentence


def test_house_rules_name_the_exchange_and_forbid_foreign_vendors():
    from psx_desk.agents import prompts

    assert "do not cite" in prompts.HOUSE_RULES.lower()
    assert "dps.psx.com.pk" in prompts.HOUSE_RULES


def test_prompt_context_is_built_from_the_packet_only():
    from psx_desk.agents.prompts import render_packet

    assert callable(render_packet)


@pytest.mark.parametrize(
    "url",
    [
        "https://finance.yahoo.com/quote/SYS.KA",
        "https://finnhub.io/api/v1/quote?symbol=SYS",
        "https://www.sec.gov/Archives/edgar/data",
        "https://www.alphavantage.co/query",
    ],
)
def test_legacy_vendors_are_unreachable_through_the_client(url):
    from psx_desk.config import load_config
    from psx_desk.dataflows.http import PortalClient

    client = PortalClient(load_config())
    with pytest.raises(ValueError):
        client.get(url)


# --------------------------------------------------------------------------- #
# The GitHub mirror tier: a separate, opt-in allow-list
# --------------------------------------------------------------------------- #


def test_mirror_hosts_are_a_disjoint_tier():
    assert not (MIRROR_HOSTS & ALLOWED_HOSTS)
    assert set(MIRROR_HOSTS) == {
        "raw.githubusercontent.com",
        "api.github.com",
        "codeload.github.com",
    }


def test_mirror_urls_require_an_explicit_opt_in():
    from psx_desk.sources import mirror_url

    for via in ("raw", "api", "tarball"):
        url = mirror_url("bigbajwatv-alt/psx-data", "main", "daily_summary.json", via=via)
        with pytest.raises(ValueError):
            assert_source_allowed(url), "the mirror tier must never be on the default list"
        assert assert_source_allowed(url, allow_mirror=True) == url


def test_mirror_url_builder_rejects_bad_input():
    from psx_desk.sources import mirror_url

    with pytest.raises(ValueError):
        mirror_url("not-a-repo", "main", "x.json")
    with pytest.raises(ValueError):
        mirror_url("owner/repo", "main", "")
    with pytest.raises(ValueError):
        mirror_url("owner/repo", "main", "x.json", via="ftp")


@pytest.mark.parametrize(
    "url",
    [
        "https://finance.yahoo.com/quote/SYS.KA",
        "https://finnhub.io/api/v1/quote?symbol=SYS",
        "https://www.sec.gov/Archives/edgar/data",
        "https://www.alphavantage.co/query",
        "https://raw.githubusercontent.com.evil.example/x/y",
        "https://api.github.com.evil.example/repos/x/y/contents/z",
    ],
)
def test_foreign_vendors_stay_blocked_even_with_the_mirror_opt_in(url):
    with pytest.raises(ValueError):
        assert_source_allowed(url, allow_mirror=True)


def test_only_the_mirror_tier_asks_for_mirror_permission():
    """Exactly the mirror scraper, the client it borrows and the URL builder."""

    allowed_files = {"github_mirror.py", "http.py", "sources.py"}
    offenders = []
    for path in python_files():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"assert_source_allowed", "get"}:
                continue
            for keyword in node.keywords:
                opted_in = (
                    keyword.arg == "allow_mirror"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                )
                if opted_in and path.name not in allowed_files:
                    offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_client_defaults_to_the_strict_allow_list():
    from psx_desk.config import load_config
    from psx_desk.dataflows.http import PortalClient

    client = PortalClient(load_config())
    with pytest.raises(ValueError):
        client.get("https://raw.githubusercontent.com/bigbajwatv-alt/psx-data/main/symbols.json")


def test_psx_sbp_hosts_remain_the_primary_universe():
    for host in ("dps.psx.com.pk", "www.psx.com.pk", "www.sbp.org.pk"):
        assert assert_source_allowed(f"https://{host}/anything") == f"https://{host}/anything"
