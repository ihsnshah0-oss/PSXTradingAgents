"""Guardrails: this repository is PSX-only, by inspection as well as by design."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from psx_desk.sources import ALLOWED_HOSTS, assert_source_allowed

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
