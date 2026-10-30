"""Test fixtures: a synthetic PSX session, served entirely from recorded payloads.

No test touches the network.  ``FixtureTransport`` answers the exact URLs the
desk asks for (and 404s everything else, like the real portal), so the scraper
walks its probe list for real.
"""

from __future__ import annotations

import io
import json
import random
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pytest

from psx_desk.config import DeskConfig, load_config
from psx_desk.dataflows.http import DiskCache, FixtureTransport, PortalClient
from psx_desk.dataflows.ingest import DeskData
from psx_desk.dataflows.portal import DumpStore
from psx_desk.schemas import (
    Action,
    AllocationStatement,
    CircuitState,
    Conviction,
    DataQuality,
    DebateArgument,
    Decision,
    DefaulterSegment,
    EvidencePoint,
    FreeFloatCheck,
    FundamentalReport,
    LockState,
    MacroBackdrop,
    Stance,
    TechnicalReport,
    TechnicalSignal,
    Verification,
)
from psx_desk.sources import (
    DEFAULTER_PORTAL_URL,
    PSX_PORTAL,
    PUCARS_URL,
    SBP_HOME_URL,
    daily_dataset_url,
)

TRADE_DATE = date(2026, 10, 29)

#: A slice of the real board, with plausible October-2026 prices.
PRICES: dict[str, float] = {
    "SYS": 412.55,
    "ENGRO": 285.10,
    "LUCK": 1210.00,
    "HUBC": 68.40,
    "OGDC": 210.50,
    "PPL": 178.25,
    "MCB": 265.80,
    "HBL": 148.90,
    "UBL": 352.60,
    "BAHL": 96.75,
    "MEBL": 288.40,
    "NBP": 82.15,
    "FFC": 402.30,
    "EFERT": 196.70,
    "FATIMA": 58.90,
    "MLCF": 54.25,
    "PIOC": 118.60,
    "DGKC": 142.35,
    "CHCC": 205.10,
    "ATLH": 88.45,
    "INDU": 1875.00,
    "MTL": 615.40,
    "SEARL": 92.80,
    "AGP": 138.75,
}

SECTORS = {
    "SYS": "Technology & Communication",
    "ENGRO": "Fertilizer",
    "LUCK": "Cement",
    "HUBC": "Power Generation & Distribution",
    "OGDC": "Oil & Gas Exploration Companies",
    "MCB": "Commercial Banks",
    "FFC": "Fertilizer",
}


def weekdays(end: date, count: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def eod_payload(symbol: str, *, bars: int = 320, end: date = TRADE_DATE, lock: str | None = None) -> bytes:
    """A DPS-style EOD series: ``[[ts, close, volume, open], ...]`` newest first."""

    price = PRICES[symbol]
    rng = random.Random(hash(symbol) & 0xFFFF)
    sessions = weekdays(end, bars)
    closes: list[float] = []
    value = price * 0.62
    for _ in sessions[:-1]:
        value *= 1 + rng.uniform(-0.018, 0.021)
        closes.append(round(value, 2))
    # Land the series on the board close so the packet is internally consistent.
    prev = closes[-1]
    if lock == "upper":
        last = round(prev * 1.05, 2)
    elif lock == "lower":
        last = round(prev * 0.95, 2)
    else:
        last = price
    closes.append(last)

    rows = []
    for session, close in zip(sessions, closes, strict=True):
        stamp = int(datetime(session.year, session.month, session.day, 15, 30).timestamp())
        volume = int(rng.uniform(400_000, 2_400_000))
        rows.append([stamp, close, volume, round(close * rng.uniform(0.995, 1.005), 2)])
    return json.dumps(list(reversed(rows))).encode()


def market_watch_html(*, include_float: bool = False) -> str:
    """The live board table as PSX renders it (subset of columns)."""

    head = ["SYMBOL", "OPEN", "HIGH", "LOW", "CURRENT", "CHANGE", "%CHANGE", "VOLUME", "VALUE"]
    if include_float:
        head.append("FREE FLOAT")
    rows = []
    for index, (symbol, close) in enumerate(PRICES.items()):
        prev = round(close / (1 + ((index % 7) - 3) / 100.0), 2)
        change = round(close - prev, 2)
        pct = round(100 * change / prev, 2)
        cells = [
            symbol,
            f"{prev:.2f}",
            f"{max(close, prev) * 1.01:.2f}",
            f"{min(close, prev) * 0.99:.2f}",
            f"{close:.2f}",
            f"{change:.2f}",
            f"{pct:.2f}",
            f"{1_200_000 + index * 13_500:,}",
            f"{3_900_000_000 + index * 25_000_000:,}",
        ]
        if include_float:
            cells.append(f"{8_500_000_000 + index * 1_100_000_000:,}")
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")

    header = "".join(f"<th>{name}</th>" for name in head)
    return f"""<!doctype html><html><body>
<table class="tbl"><thead><tr>{header}</tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def zipped_csv(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def symbol_price_zip(symbols: Sequence[str], *, lock: str | None = None) -> bytes:
    lines = ["SYMBOL,UPPER CIRCUIT,LOWER CIRCUIT"]
    for symbol in symbols:
        prev = PRICES[symbol]
        if lock == "upper" and symbol == "HUBC":
            upper, lower = round(prev, 2), round(prev * 0.95, 2)
        else:
            upper, lower = round(prev * 1.05, 2), round(prev * 0.95, 2)
        lines.append(f"{symbol},{upper:.2f},{lower:.2f}")
    return zipped_csv("symbol_price.csv", "\n".join(lines))


def symbol_name_zip(symbols: Sequence[str]) -> bytes:
    names = {
        "SYS": "Systems Limited",
        "ENGRO": "Engro Corporation Limited",
        "LUCK": "Lucky Cement Limited",
        "HUBC": "Hub Power Company Limited",
        "OGDC": "Oil & Gas Development Company Limited",
        "MCB": "MCB Bank Limited",
        "FFC": "Fauji Fertilizer Company Limited",
    }
    lines = ["SYMBOL,COMPANY NAME"]
    for symbol in symbols:
        lines.append(f"{symbol},{names.get(symbol, f'{symbol} Industries Limited')}")
    return zipped_csv("symbol_name.csv", "\n".join(lines))


def defaulter_html(extra_defaulters: Sequence[str] = ()) -> str:
    current = ["Worldcall Telecom Limited", "Pace (Pakistan) Limited", *extra_defaulters]
    past = ["Dewan Salman Fibre Limited", "Zeal Pak Cement Factory Limited"]
    current_html = "".join(f"<div class='row'>{name}</div>" for name in current)
    past_html = "".join(f"<div class='row'>{name}</div>" for name in past)
    return f"""<!doctype html><html><body>
<h2>Non-Compliant Companies and Securities Profile Portal</h2>
<h3>Current Non-Compliant Segment</h3>
{current_html}
<h3>Companies Previously Quoted in Defaulters' Segment</h3>
{past_html}
</body></html>"""


def sbp_html(policy: float = 12.0, ceiling: float = 13.0, floor: float = 11.0, cpi: float = 10.3) -> str:
    return f"""<!doctype html><html><body>
<div class="tile">SBP Policy Rate <strong>{policy:.2f}%</strong> p.a.</div>
<div class="tile">SBP Overnight Reverse (Repo) Ceiling Rate <strong>{ceiling:.2f}%</strong> p.a.</div>
<div class="tile">SBP Overnight Reverse (Floor) Rate <strong>{floor:.2f}%</strong> p.a.</div>
<div class="tile">Inflation {cpi:.2f}% YoY</div>
<div class="tile">USD/PKR Weighted Average Rate <strong>277.2072</strong></div>
</body></html>"""


def pucars_html(company: str = "Systems Limited") -> str:
    return f"""<!doctype html><html><body><ul>
<li><a href="/psx/announcement/12345">Financial Results for the Quarter Ended September 30, 2026 - {company}</a></li>
<li><a href="/psx/announcement/12346">Board Meeting of {company} scheduled for 28 October 2026</a></li>
<li><a href="/psx/announcement/12347">Unrelated notice for another issuer</a></li>
</ul></body></html>"""


@dataclass
class World:
    """The synthetic PSX universe a test runs against."""

    lock: str | None = None
    defaulters: tuple[str, ...] = ()
    include_float: bool = False
    missing_caps_file: bool = False
    transport: FixtureTransport = field(init=False)

    def __post_init__(self) -> None:
        mapping: dict[str, bytes | str] = {
            f"{PSX_PORTAL}/market-watch": market_watch_html(include_float=self.include_float),
            DEFAULTER_PORTAL_URL: defaulter_html(self.defaulters),
            PUCARS_URL: pucars_html(),
            SBP_HOME_URL: sbp_html(),
            f"{PSX_PORTAL}/symbols": json.dumps([{"symbol": s} for s in PRICES]),
        }
        for symbol in PRICES:
            mapping[f"{PSX_PORTAL}/timeseries/eod/{symbol}"] = eod_payload(symbol, lock=self.lock)
        mapping[f"{PSX_PORTAL}/timeseries/eod/KSE100"] = eod_payload("OGDC", bars=40)
        mapping[f"{PSX_PORTAL}/timeseries/eod/KSE30"] = eod_payload("MCB", bars=40)
        mapping[f"{PSX_PORTAL}/timeseries/eod/ALLSHR"] = eod_payload("LUCK", bars=40)

        for trade_date in (TRADE_DATE,):
            mapping[daily_dataset_url("symbol_name", trade_date, "zip")] = symbol_name_zip(list(PRICES))
            if not self.missing_caps_file:
                mapping[daily_dataset_url("symbol_price", trade_date, "zip")] = symbol_price_zip(
                    list(PRICES), lock=self.lock
                )
        self.transport = FixtureTransport(mapping)


@pytest.fixture()
def world() -> World:
    return World()


@pytest.fixture()
def work_dir(tmp_path):
    return tmp_path / "desk"


@pytest.fixture()
def config(work_dir) -> DeskConfig:
    return load_config(
        work_dir=work_dir,
        throttle_seconds=0.0,
        http_retries=1,
        offline=False,
        max_debate_rounds=1,
    )


@pytest.fixture()
def client(config, world) -> PortalClient:
    return PortalClient(
        config, transport=world.transport, cache=DiskCache(config.cache_dir), sleep=lambda _s: None
    )


@pytest.fixture()
def desk_data(config, client) -> DeskData:
    return DeskData(client=client, config=config, dumps=DumpStore(client))


# --------------------------------------------------------------------------- #
# Scripted LLM
# --------------------------------------------------------------------------- #


def dummy_circuit(close: float = 412.55) -> CircuitState:
    return CircuitState(
        band_pct=5.0,
        upper_cap_pkr=round(close * 1.05, 2),
        lower_cap_pkr=round(close * 0.95, 2),
        close_pkr=close,
        locked=LockState.NONE,
        distance_to_upper_pct=5.0,
        distance_to_lower_pct=5.0,
    )


def dummy_defaulter(symbol: str = "SYS") -> DefaulterSegment:
    return DefaulterSegment(symbol=symbol, checked=True, segment="regular")


class ScriptedInvoker:
    """A StructuredInvoker that answers from a script instead of a model."""

    def __init__(
        self,
        *,
        bull_conviction: float = 0.68,
        bear_conviction: float = 0.42,
        target: float = 480.0,
        stop: float = 392.0,
        decision: Decision = Decision.APPROVE,
        action: Action = Action.BUY,
        allocation_pkr: float = 5_000_000.0,
    ) -> None:
        self.bull_conviction = bull_conviction
        self.bear_conviction = bear_conviction
        self.target = target
        self.stop = stop
        self.decision = decision
        self.action = action
        self.allocation_pkr = allocation_pkr
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    def invoke(self, schema, *, system: str, user: str, tier: str = "quick"):
        self.calls.append(schema.__name__)
        self.prompts[schema.__name__] = user

        if schema is TechnicalReport:
            return TechnicalReport(
                symbol="SYS",
                as_of=TRADE_DATE,
                trend="uptrend",
                momentum_score=62,
                verdict="bullish",
                confidence=0.6,
                signals=[
                    TechnicalSignal(
                        name="20/50 SMA", reading="above", implication="bullish",
                        verification=Verification.DERIVED,
                    ),
                    TechnicalSignal(
                        name="circuit", reading="open", implication="neutral",
                        verification=Verification.HARD_DATA,
                    ),
                ],
                narrative="Tape is constructive; the scrip is working inside its band.",
                circuit=dummy_circuit(),
            )

        if schema is FundamentalReport:
            return FundamentalReport(
                symbol="SYS",
                as_of=TRADE_DATE,
                pe_ratio=18.5,
                eps_pkr=22.3,
                dividend_yield_pct=2.4,
                macro=MacroBackdrop(sbp_policy_rate_pct=12.0, cpi_yoy_pct=10.3),
                macro_sensitivity="medium",
                rate_path_note="A 12% policy rate keeps the discount rate high.",
                score=64,
                verdict="fair",
                narrative=(
                    "Earnings yield of 5.4% sits below the policy rate, so the case rests "
                    "on growth rather than carry."
                ),
                data_quality=DataQuality.PARTIAL,
            )

        if schema is DebateArgument:
            stance = Stance.BEAR if "BEAR CASE" in user else Stance.BULL
            if stance is Stance.BULL:
                return DebateArgument(
                    stance=stance, symbol="SYS", round=1,
                    thesis="Systems is compounding revenue while the float stays tight.",
                    key_points=[
                        EvidencePoint(claim="Turnover is deep", metric="adv_value_pkr_20d",
                                      value="Rs 300m", verification=Verification.HARD_DATA),
                        EvidencePoint(claim="Price is above its 50-day", metric="sma_50",
                                      value="PKR 390", verification=Verification.DERIVED),
                    ],
                    target_price_pkr=self.target, stop_pkr=self.stop,
                    horizon_days=20, conviction=self.bull_conviction,
                )
            return DebateArgument(
                stance=stance, symbol="SYS", round=1,
                thesis="A 5.4% earnings yield does not beat a 12% policy rate deposit.",
                key_points=[
                    EvidencePoint(claim="Earnings yield below policy rate",
                                  metric="earnings_yield_pct", value="5.4%",
                                  verification=Verification.DERIVED),
                    EvidencePoint(claim="Turnover is deep", metric="adv_value_pkr_20d",
                                  value="Rs 300m", verification=Verification.HARD_DATA),
                ],
                target_price_pkr=370.0, stop_pkr=430.0, horizon_days=20,
                conviction=self.bear_conviction,
                rebuttal="Growth does not pay the desk's cost of capital today.",
            )

        if schema is AllocationStatement:
            return AllocationStatement(
                symbol="SYS",
                as_of=TRADE_DATE,
                decision=self.decision,
                action=self.action,
                conviction=Conviction.MEDIUM,
                stronger_case="bull",
                nav_pkr=50_000_000.0,
                allocation_pkr=self.allocation_pkr,
                weight_of_nav_pct=10.0,
                entry_limit_pkr=412.55,
                stop_pkr=self.stop,
                target_pkr=self.target,
                gates=[],
                free_float=FreeFloatCheck(passes=True),
                defaulter=dummy_defaulter(),
                circuit=dummy_circuit(),
                rationale="Debate favoured the bull case; liquidity gates passed.",
            )

        raise AssertionError(f"unscripted schema: {schema.__name__}")


@pytest.fixture()
def invoker() -> ScriptedInvoker:
    return ScriptedInvoker()
