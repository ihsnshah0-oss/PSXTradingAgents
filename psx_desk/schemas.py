"""Typed contracts for every node on the desk.

Each node of the LangGraph pipeline returns exactly one of these models through
``with_structured_output``; nothing conversational survives a node boundary.
Field descriptions are part of the prompt surface — the provider turns them into
the model's output instructions — so they are written as instructions to a
senior PSX analyst, not as documentation.

The models also carry the market-structure logic the desk must never lose:

* :class:`CircuitState` — the PSX scrip-level circuit lock (upper/lower band),
  the constraint that uniquely shapes execution on the exchange.
* :class:`DefaulterSegment` — whether the scrip sits on the PSX Defaulters /
  Non-Compliant segment, which makes it untradeable no matter the thesis.
* :class:`MacroBackdrop` — SBP policy rate, corridor and CPI, because a
  Pakistani equity's valuation is meaningless without the risk-free anchor.
* :class:`AllocationStatement` — the CRO's strict JSON allocation, validated
  for internal consistency before it can reach an order pad.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class DataQuality(StrEnum):
    """How much of the requested PSX evidence actually arrived."""

    VERIFIED = "verified"      # every required PSX field parsed from a live payload
    STALE = "stale"            # parsed, but as_of predates the trade date
    PARTIAL = "partial"        # some fields missing; the rest are real
    MISSING = "missing"        # nothing usable; the node must say so


class LockState(StrEnum):
    UPPER = "upper"
    LOWER = "lower"
    NONE = "none"


class Trend(StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    SIDEWAYS = "sideways"


class Stance(StrEnum):
    BULL = "bull"
    BEAR = "bear"


class Verification(StrEnum):
    """Provenance class of a single claim in a research note."""

    HARD_DATA = "hard_data"   # a number read from a PSX endpoint
    DERIVED = "derived"       # computed from PSX numbers (indicator, ratio)
    INFERENCE = "inference"   # analyst judgement, not directly measured


class GateStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    APPROVE_REDUCED = "APPROVE_REDUCED"
    REJECT = "REJECT"


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class Conviction(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #


class SourceRef(BaseModel):
    """A single PSX/SBP payload this run actually read.

    Every numeric block in a report carries at least one of these, so a
    reviewer can open the exact URL and reproduce the number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(description="Human label, e.g. 'DPS EOD timeseries: ENGRO'.")
    url: str = Field(description="Exact https URL on psx.com.pk, dps.psx.com.pk or sbp.org.pk.")
    source_kind: str = Field(default="timeseries", description="Payload class, e.g. market_dump.")
    retrieved_at: datetime = Field(description="UTC timestamp the desk fetched the payload.")
    http_status: int | None = Field(default=None, description="HTTP status, or null for cache hits.")
    sha256: str | None = Field(default=None, description="SHA-256 of the payload bytes.")
    as_of: date | None = Field(default=None, description="Trading date the payload covers.")
    from_cache: bool = Field(default=False, description="True when served from the local cache.")


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #


class PSXQuote(BaseModel):
    """One scrip's session snapshot, in PKR, exactly as PSX reports it."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="PSX scrip code, e.g. SYS.")
    company_name: str | None = Field(default=None, description="Registered company name.")
    sector: str | None = Field(default=None, description="PSX sector classification.")
    board: str = Field(default="REG", description="Trading board: REG is the ready market.")
    trade_date: date = Field(description="Session the snapshot belongs to.")

    open_pkr: float | None = Field(default=None, ge=0, description="Session open, PKR.")
    high_pkr: float | None = Field(default=None, ge=0, description="Session high, PKR.")
    low_pkr: float | None = Field(default=None, ge=0, description="Session low, PKR.")
    close_pkr: float = Field(gt=0, description="Closing/last price in PKR, e.g. 412.55.")
    prev_close_pkr: float | None = Field(default=None, gt=0, description="Previous session close.")
    change_pkr: float | None = Field(default=None, description="Close minus previous close, PKR.")
    change_pct: float | None = Field(default=None, description="Session change in percent, e.g. 3.42.")

    volume_shares: float | None = Field(default=None, ge=0, description="Shares traded.")
    value_pkr: float | None = Field(default=None, ge=0, description="Turnover in PKR.")
    trades: int | None = Field(default=None, ge=0, description="Number of trades.")

    free_float_shares: float | None = Field(default=None, ge=0, description="Free-float shares.")
    free_float_mcap_pkr: float | None = Field(
        default=None, ge=0, description="Free-float market capitalisation in PKR."
    )
    market_cap_pkr: float | None = Field(default=None, ge=0, description="Total market cap, PKR.")

    upper_cap_pkr: float | None = Field(
        default=None, gt=0, description="Exchange-published upper circuit price for the session."
    )
    lower_cap_pkr: float | None = Field(
        default=None, gt=0, description="Exchange-published lower circuit price for the session."
    )

    sources: list[SourceRef] = Field(default_factory=list, description="Payloads behind this row.")


class CircuitState(BaseModel):
    """The PSX scrip-level circuit lock — the binding execution constraint.

    PSX halts a scrip at a fixed band from the previous close (5.0% on the desk's
    default rulebook, widening to 7.5% under Regulation 19.3, with a floor of
    Rs.1 whenever that is higher), rounding to the tick.  A locked scrip is not
    tradeable at the close: the analysis is still valid, the execution is not.
    """

    model_config = ConfigDict(extra="forbid")

    band_pct: float = Field(gt=0, description="Band in force this session, in percent.")
    band_floor_pkr: float = Field(default=1.0, ge=0, description="Absolute floor of the band, PKR.")
    upper_cap_pkr: float = Field(gt=0, description="Upper circuit price, PKR.")
    lower_cap_pkr: float = Field(gt=0, description="Lower circuit price, PKR.")
    close_pkr: float = Field(gt=0, description="Close being tested.")
    locked: LockState = Field(description="upper / lower / none.")
    cap_source: Literal["exchange_file", "computed"] = Field(
        default="computed",
        description="exchange_file when PSX published the caps; computed from the band otherwise.",
    )
    distance_to_upper_pct: float = Field(
        ge=0, description="Room left before the upper cap, percent of close."
    )
    distance_to_lower_pct: float = Field(
        ge=0, description="Room left before the lower cap, percent of close."
    )
    consecutive_locked_sessions: int = Field(
        default=0,
        ge=0,
        description="Trailing sessions locked in the same direction — PSX tightens scrutiny here.",
    )
    tradable_at_close: bool = Field(
        default=True, description="False when a lock makes a closing print unexecutable."
    )
    execution_note: str = Field(
        default="",
        description=(
            "One sentence a dealer needs: when the scrip can actually be worked "
            "(e.g. 'locked upper; wait for the next session's opening auction')."
        ),
    )


class IndexSnapshot(BaseModel):
    """A PSX index level, for market-regime context."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="Index code, e.g. KSE100.")
    close: float = Field(gt=0, description="Index level.")
    change_pct: float | None = Field(default=None, description="Session change, percent.")
    volume_shares: float | None = Field(default=None, ge=0, description="Index volume.")
    as_of: date | None = Field(default=None, description="Session date.")


class PSXMarketPacket(BaseModel):
    """Node 1 output: the raw, verified PSX packet the analysts are allowed to use.

    This is a *data* object, not an opinion.  If a field is ``None`` the desk did
    not receive it, and no downstream agent may fill the gap from memory.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="PSX scrip code under review.")
    trade_date: date = Field(description="Analysis date; all data is as-of this session.")
    quote: PSXQuote = Field(description="Session snapshot.")
    circuit: CircuitState = Field(description="Circuit-lock state for the session.")
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Trailing daily bars [{date, open, high, low, close, volume}] oldest first.",
    )
    adv_shares_20d: float | None = Field(default=None, ge=0, description="20-session average volume.")
    adv_value_pkr_20d: float | None = Field(default=None, ge=0, description="20-session average turnover.")
    high_52w_pkr: float | None = Field(default=None, gt=0, description="52-week high close.")
    low_52w_pkr: float | None = Field(default=None, gt=0, description="52-week low close.")
    indices: list[IndexSnapshot] = Field(default_factory=list, description="KSE-100 / KSE-30 context.")
    defaulter: DefaulterSegment = Field(description="Defaulters/Non-Compliant segment check.")
    macro: MacroBackdrop | None = Field(default=None, description="SBP policy backdrop at run time.")
    disclosures: list[DisclosureItem] = Field(
        default_factory=list, description="PSX/PUCARS notices for this scrip, newest first."
    )
    data_quality: DataQuality = Field(default=DataQuality.VERIFIED)
    missing_fields: list[str] = Field(
        default_factory=list, description="Fields PSX did not return; agents must not invent them."
    )
    notes: list[str] = Field(
        default_factory=list, description="Operational caveats from the scrape (holidays, fallbacks)."
    )
    sources: list[SourceRef] = Field(default_factory=list)


class DefaulterSegment(BaseModel):
    """Whether the scrip is on PSX's Defaulters / Non-Compliant segment."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    company_name: str | None = None
    checked: bool = Field(description="True only when the PSX portal page actually parsed.")
    is_defaulting: bool = Field(
        default=False, description="True if named on the non-compliant/defaulter portal."
    )
    segment: str = Field(
        default="regular",
        description="regular | non_compliant | defaulters | suspended | delisted",
    )
    evidence: str = Field(
        default="",
        description="The matched line and the section it was found in, for auditability.",
    )
    source: SourceRef | None = Field(default=None, description="PSX portal page read.")
    as_of: date | None = Field(default=None, description="Date the portal list was read.")


class DisclosureItem(BaseModel):
    """One corporate disclosure surfaced from PSX/PUCARS."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Headline of the notice.")
    published: date | None = Field(default=None, description="Publication date.")
    category: str = Field(default="general", description="e.g. financial_results, board_meeting.")
    url: str = Field(description="PSX link to the notice.")
    summary: str = Field(default="", description="Short factual extract; never an interpretation.")
    sentiment_hint: Literal["positive", "negative", "neutral", "unclear"] = Field(default="unclear")


class MacroBackdrop(BaseModel):
    """The Pakistani macro anchor every valuation must be scored against."""

    model_config = ConfigDict(extra="forbid")

    sbp_policy_rate_pct: float = Field(gt=0, description="SBP policy (target) rate, percent p.a.")
    corridor_floor_pct: float | None = Field(default=None, description="Overnight repo floor.")
    corridor_ceiling_pct: float | None = Field(default=None, description="Overnight reverse repo ceiling.")
    cpi_yoy_pct: float | None = Field(default=None, description="Headline CPI, year-on-year percent.")
    pkr_usd: float | None = Field(default=None, gt=0, description="USD/PKR, PKR per dollar.")
    tbill_6m_cutoff_pct: float | None = Field(default=None, description="6-month T-bill cut-off yield.")
    kibor_6m_pct: float | None = Field(default=None, description="6-month KIBOR, percent.")
    real_policy_rate_pct: float | None = Field(
        default=None,
        description="Policy rate minus CPI y/y. A deeply negative real rate is the Pakistani norm "
        "and is exactly why equity earnings yields get re-rated.",
    )
    as_of: date | None = Field(default=None, description="Date of the print.")
    quality: DataQuality = Field(
        default=DataQuality.VERIFIED,
        description="stale when the desk used its labelled fallback print instead of a live SBP read.",
    )
    basis: Literal["live_scrape", "configured_fallback"] = Field(
        default="live_scrape",
        description="live_scrape when parsed from sbp.org.pk this run; configured_fallback when "
        "the desk fell back to its labelled stored print.",
    )
    sources: list[SourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_real_rate(self) -> Self:
        if self.real_policy_rate_pct is None and self.cpi_yoy_pct is not None:
            self.real_policy_rate_pct = round(self.sbp_policy_rate_pct - self.cpi_yoy_pct, 2)
        return self


# --------------------------------------------------------------------------- #
# Node 2 — Technical analyst
# --------------------------------------------------------------------------- #


class TechnicalSignal(BaseModel):
    """One measured technical fact, with the number that produced it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="e.g. 'RSI(14)', '20/50 EMA cross', 'lower circuit lock'.")
    reading: str = Field(description="The measured value as printed, e.g. '71.4' or 'lock lower'.")
    implication: Literal["bullish", "bearish", "neutral"] = Field(description="Directional read.")
    verification: Verification = Field(
        default=Verification.DERIVED, description="hard_data / derived / inference."
    )


class TechnicalReport(BaseModel):
    """Node 2 output: momentum, price action and circuit-lock surveillance."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    as_of: date
    trend: Trend = Field(description="Primary trend on the daily chart.")
    momentum_score: int = Field(
        ge=0, le=100, description="0 = washed out, 50 = neutral, 100 = vertical. Be decisive."
    )
    rsi_14: float | None = Field(default=None, description="14-session RSI.")
    sma_20: float | None = Field(default=None, description="20-session simple moving average, PKR.")
    sma_50: float | None = Field(default=None, description="50-session SMA, PKR.")
    sma_200: float | None = Field(default=None, description="200-session SMA, PKR.")
    atr_14_pkr: float | None = Field(default=None, description="14-session ATR in PKR.")
    volatility_annualised_pct: float | None = Field(
        default=None, description="Annualised realised volatility, percent."
    )
    support_pkr: float | None = Field(default=None, description="Nearest support, PKR.")
    resistance_pkr: float | None = Field(default=None, description="Nearest resistance, PKR.")
    volume_zscore_20: float | None = Field(
        default=None, description="Session volume vs its 20-day mean, in standard deviations."
    )
    circuit: CircuitState = Field(description="Circuit-lock surveillance, passed through verbatim.")
    signals: list[TechnicalSignal] = Field(
        default_factory=list, description="Every signal you used, with its reading."
    )
    verdict: Literal["bullish", "neutral", "bearish"] = Field(description="Directional call.")
    confidence: float = Field(ge=0, le=1, description="0-1 confidence in the verdict.")
    invalidation_pkr: float | None = Field(
        default=None, description="Price at which the technical thesis is wrong, PKR."
    )
    tradability: Literal["open", "locked_upper", "locked_lower", "illiquid"] = Field(
        default="open", description="Execution state after circuit and liquidity checks."
    )
    narrative: str = Field(
        description="5-8 sentences in the voice of a Karachi technical analyst: price action, "
        "the circuit tape, volume behaviour, and what would change your mind."
    )
    data_quality: DataQuality = DataQuality.VERIFIED
    sources: list[SourceRef] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Node 3 — Fundamental analyst
# --------------------------------------------------------------------------- #


class FinancialMetric(BaseModel):
    """One reported or derived fundamental metric."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="e.g. 'EPS (FY25, PKR)', 'Debt/Equity', 'Dividend yield'.")
    value: float | None = Field(default=None, description="Numeric value; null if not disclosed.")
    unit: str = Field(default="", description="PKR, percent, x, times ...")
    period: str = Field(default="", description="e.g. FY25, 9MFY26, 1QFY27.")
    verification: Verification = Field(default=Verification.HARD_DATA)


class RedFlag(BaseModel):
    """A governance or balance-sheet concern the desk must price."""

    model_config = ConfigDict(extra="forbid")

    flag: str = Field(description="Short label, e.g. 'auditor qualification', 'negative equity'.")
    severity: Literal["low", "medium", "high"] = Field(description="How much it matters.")
    evidence: str = Field(description="The disclosure or ratio behind it — no speculation.")


class FundamentalReport(BaseModel):
    """Node 3 output: corporate quality scored against Pakistani macro reality."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    company_name: str | None = None
    sector: str | None = None
    as_of: date

    eps_pkr: float | None = Field(default=None, description="Trailing EPS, PKR.")
    pe_ratio: float | None = Field(default=None, description="Price / trailing EPS.")
    pb_ratio: float | None = Field(default=None, description="Price / book value.")
    dividend_yield_pct: float | None = Field(default=None, description="Trailing dividend yield, percent.")
    roe_pct: float | None = Field(default=None, description="Return on equity, percent.")
    net_margin_pct: float | None = Field(default=None, description="Net profit margin, percent.")
    debt_to_equity: float | None = Field(default=None, description="Total debt / equity, times.")
    interest_cover_x: float | None = Field(default=None, description="EBIT / finance cost, times.")
    revenue_growth_yoy_pct: float | None = Field(default=None, description="Revenue growth, percent.")
    pat_growth_yoy_pct: float | None = Field(default=None, description="Profit after tax growth, percent.")

    earnings_yield_pct: float | None = Field(
        default=None, description="1/PE as a percent — the number to compare with the policy rate."
    )
    equity_risk_premium_pct: float | None = Field(
        default=None,
        description="Earnings yield minus SBP policy rate. Negative means cash pays more than the equity.",
    )
    real_earnings_yield_pct: float | None = Field(
        default=None, description="Earnings yield minus CPI y/y — inflation-adjusted shareholder return."
    )

    macro: MacroBackdrop = Field(description="The SBP backdrop used for scoring.")
    macro_sensitivity: Literal["high", "medium", "low"] = Field(
        description="How hard SBP rate moves hit this business (leveraged cyclicals = high)."
    )
    rate_path_note: str = Field(
        description="2-3 sentences on what the policy rate path does to this balance sheet and valuation."
    )

    score: int = Field(ge=0, le=100, description="Quality/valuation score, 50 = market-average.")
    verdict: Literal["cheap", "fair", "expensive"] = Field(description="Valuation verdict.")
    red_flags: list[RedFlag] = Field(default_factory=list)
    metrics: list[FinancialMetric] = Field(default_factory=list, description="Metrics you actually used.")
    narrative: str = Field(
        description="6-10 sentences as a Karachi buy-side analyst: earnings quality, leverage "
        "versus KIBOR, dividend cover, sector macro, and the valuation argument."
    )
    data_quality: DataQuality = Field(
        default=DataQuality.PARTIAL,
        description="verified only if the annual/periodical report figures were actually read from PSX.",
    )
    sources: list[SourceRef] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Node 4 — Adversarial research
# --------------------------------------------------------------------------- #


class EvidencePoint(BaseModel):
    """A single claim in a debate turn, tied to a PSX number."""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(description="One sentence, falsifiable.")
    metric: str = Field(default="", description="The PSX metric it rests on, e.g. 'ADV value 20d'.")
    value: str = Field(default="", description="The printed value, e.g. 'Rs 214m'.")
    verification: Verification = Field(default=Verification.HARD_DATA)


class DebateArgument(BaseModel):
    """One researcher's turn in the bull/bear debate."""

    model_config = ConfigDict(extra="forbid")

    stance: Stance
    symbol: str
    round: int = Field(ge=1, description="Debate round this turn belongs to.")
    thesis: str = Field(description="The core position in two sentences. No hedging.")
    key_points: list[EvidencePoint] = Field(
        min_length=2, description="At least two evidence points drawn from the PSX packet."
    )
    rebuttal: str = Field(
        default="", description="Direct answer to the opposing side's strongest point from the prior round."
    )
    target_price_pkr: float | None = Field(default=None, gt=0, description="Price target, PKR.")
    horizon_days: int | None = Field(default=None, ge=1, le=365, description="Horizon in trading days.")
    expected_return_pct: float | None = Field(default=None, description="Target versus spot, percent.")
    stop_pkr: float | None = Field(default=None, gt=0, description="Invalidation level, PKR.")
    conviction: float = Field(ge=0, le=1, description="0-1 conviction.")
    concedes: list[str] = Field(
        default_factory=list, description="Points you concede to the other side; honesty is scored."
    )


class DebateTranscript(BaseModel):
    """The full adversarial record handed to the CRO."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    rounds_completed: int = Field(ge=1)
    bull: list[DebateArgument] = Field(default_factory=list)
    bear: list[DebateArgument] = Field(default_factory=list)
    consensus: list[str] = Field(default_factory=list, description="Facts both sides accepted.")
    open_disagreements: list[str] = Field(default_factory=list)
    stronger_case: Literal["bull", "bear", "balanced"] = Field(
        description="Which side carried the argument on evidence, not rhetoric."
    )
    debate_quality: Literal["strong", "thin", "circular"] = Field(
        default="strong",
        description="thin when either side recycled claims without new PSX evidence; circular when "
        "the same point was restated to fill a round.",
    )
    summary: str = Field(description="4-6 sentence synthesis the CRO reads first.")


# --------------------------------------------------------------------------- #
# Node 5 — CRO gatekeeper
# --------------------------------------------------------------------------- #


class RiskGate(BaseModel):
    """One deterministic pre-trade gate and its measured value."""

    model_config = ConfigDict(extra="forbid")

    gate: str = Field(description="e.g. free_float_liquidity, defaulter_segment, circuit_lock.")
    status: GateStatus
    measured: str = Field(description="What the desk measured, with units.")
    limit: str = Field(description="The desk limit or rule this was tested against.")
    blocking: bool = Field(description="True if a FAIL here vetoes the trade outright.")
    note: str = Field(default="")


class FreeFloatCheck(BaseModel):
    """Liquidity test: can the desk actually get in and out at size?"""

    model_config = ConfigDict(extra="forbid")

    free_float_mcap_pkr: float | None = Field(default=None, description="Free-float market cap, PKR.")
    adv_shares_20d: float | None = Field(default=None, description="20-session average volume.")
    adv_value_pkr_20d: float | None = Field(default=None, description="20-session average turnover, PKR.")
    proposed_shares: float | None = Field(default=None, description="Shares the allocation would buy.")
    participation_pct_of_adv: float | None = Field(
        default=None, description="Proposed shares as a percent of 20-day ADV."
    )
    days_to_exit: float | None = Field(
        default=None, description="Sessions to liquidate at the participation limit."
    )
    passes: bool = Field(description="True when every liquidity gate passed.")
    comment: str = Field(default="", description="One sentence for the dealer.")


class AllocationStatement(BaseModel):
    """The CRO's strict JSON allocation — the desk's only executable artifact.

    Internal consistency is enforced here, not trusted from the model: a REJECT
    cannot carry size, a BUY cannot sit below its own stop, and the risk budget
    is recomputed from the levels rather than accepted as stated.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["psx-alloc/1.0"] = "psx-alloc/1.0"
    symbol: str = Field(description="PSX scrip code.")
    as_of: date = Field(description="Session date the allocation was struck.")
    decision: Decision = Field(
        description="APPROVE (full size), APPROVE_REDUCED (half or less, with a reason), REJECT."
    )
    action: Action = Field(description="BUY / SELL / HOLD / NO_TRADE.")
    conviction: Conviction = Field(description="high / medium / low.")
    stronger_case: Literal["bull", "bear", "balanced"] = Field(
        description="Which debate side the CRO found decisive."
    )

    nav_pkr: float = Field(gt=0, description="Desk NAV the weights are measured against.")
    allocation_pkr: float = Field(ge=0, description="Capital allocated, PKR. Zero on REJECT/NO_TRADE.")
    weight_of_nav_pct: float = Field(ge=0, description="allocation_pkr / nav_pkr, percent.")
    shares: int = Field(default=0, ge=0, description="Whole shares, at the entry limit price.")

    entry_limit_pkr: float | None = Field(default=None, gt=0, description="Limit price, PKR.")
    stop_pkr: float | None = Field(default=None, gt=0, description="Hard stop, PKR.")
    target_pkr: float | None = Field(default=None, gt=0, description="First profit target, PKR.")
    risk_per_share_pkr: float | None = Field(default=None, ge=0, description="Entry minus stop, PKR.")
    risk_budget_pkr: float = Field(default=0, ge=0, description="1R in PKR, from the desk limit.")
    reward_risk: float | None = Field(default=None, ge=0, description="(Target-entry)/(Entry-stop).")
    max_hold_days: int | None = Field(default=None, ge=1, description="Time stop, trading days.")

    gates: list[RiskGate] = Field(default_factory=list, description="Every gate tested, pass or fail.")
    free_float: FreeFloatCheck = Field(description="Liquidity test detail.")
    defaulter: DefaulterSegment = Field(description="Defaulters/Non-Compliant segment check.")
    circuit: CircuitState = Field(description="Circuit state at the close.")
    size_cap_reason: str = Field(
        default="", description="Why the size was trimmed, e.g. 'ADV participation cap'."
    )
    kill_switches: list[str] = Field(
        default_factory=list,
        description="Conditions that force the desk out regardless of P&L (e.g. lower circuit twice).",
    )
    rationale: str = Field(
        description="6-10 sentences: which evidence survived scrutiny, what the gates measured, "
        "and why this size and not more."
    )
    conditions: list[str] = Field(
        default_factory=list, description="Pre-conditions before the order is worked."
    )
    approved_by: str = Field(default="Chief Risk Officer, PSX Equities")
    data_quality: DataQuality = Field(default=DataQuality.VERIFIED)

    # -- deterministic consistency ---------------------------------------- #

    @model_validator(mode="after")
    def _enforce_consistency(self) -> Self:
        notes: list[str] = []

        # 1. A veto can never carry size or a live direction.
        if self.decision == Decision.REJECT:
            if self.allocation_pkr or self.shares:
                notes.append("REJECT forced to zero size.")
            self.allocation_pkr = 0.0
            self.shares = 0
            self.weight_of_nav_pct = 0.0
            self.action = Action.NO_TRADE
            self.risk_budget_pkr = 0.0

        # 2. The defaulter segment is an absolute veto, whichever way the
        #    model voted.
        if self.defaulter.is_defaulting:
            if self.decision != Decision.REJECT:
                notes.append("Defaulters segment overrode the decision to REJECT.")
            self.decision = Decision.REJECT
            self.action = Action.NO_TRADE
            self.allocation_pkr = 0.0
            self.shares = 0
            self.weight_of_nav_pct = 0.0
            self.risk_budget_pkr = 0.0

        # 3. Weight is a derived quantity.
        if self.nav_pkr > 0:
            self.weight_of_nav_pct = round(100.0 * self.allocation_pkr / self.nav_pkr, 4)

        # 4. A locked tape cannot be executed at the closing print.
        if self.circuit.locked != LockState.NONE and self.action in {Action.BUY, Action.SELL}:
            if self.allocation_pkr:
                notes.append("Circuit lock deferred execution; size retained behind a pre-open condition.")
            self.conditions = list(dict.fromkeys([
                *self.conditions,
                f"Scrip locked {self.circuit.locked.value}: work nothing until the next opening auction.",
            ]))

        # 5. Levels must be ordered, and the derived risk figures must agree
        #    with the levels the CRO actually stated.
        long_with_levels = (
            self.action in {Action.BUY, Action.HOLD} and self.entry_limit_pkr and self.stop_pkr
        )
        if long_with_levels and self.stop_pkr >= self.entry_limit_pkr:
            notes.append("Stop at or above entry for a long: stop dropped.")
            self.stop_pkr = None
        if self.entry_limit_pkr and self.stop_pkr:
            self.risk_per_share_pkr = round(self.entry_limit_pkr - self.stop_pkr, 4)
            if self.risk_per_share_pkr > 0 and self.shares:
                self.risk_budget_pkr = round(self.risk_per_share_pkr * self.shares, 2)
            if self.target_pkr and self.risk_per_share_pkr > 0:
                self.reward_risk = round(
                    (self.target_pkr - self.entry_limit_pkr) / self.risk_per_share_pkr, 3
                )
                if self.reward_risk < 0:
                    self.reward_risk = 0.0

        # 6. Shares follow from size and price, never from the model's guess.
        if self.entry_limit_pkr and self.allocation_pkr and self.action in {Action.BUY, Action.SELL}:
            implied = int(self.allocation_pkr // self.entry_limit_pkr)
            if implied != self.shares:
                if self.shares:
                    notes.append("Shares recomputed from allocation and entry limit.")
                self.shares = implied

        if notes:
            self.size_cap_reason = " | ".join(filter(None, [self.size_cap_reason, *notes]))
        return self

    def to_json(self) -> str:
        """Canonical strict JSON, sorted keys, no NaN."""
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False)


# --------------------------------------------------------------------------- #
# Run record
# --------------------------------------------------------------------------- #


class DeskRun(BaseModel):
    """Everything one run produced, in the order the graph produced it."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    trade_date: date
    started_at: datetime
    finished_at: datetime | None = None
    packet: PSXMarketPacket
    technical: TechnicalReport
    fundamental: FundamentalReport
    debate: DebateTranscript
    allocation: AllocationStatement
    run_id: str = ""
    notes: list[str] = Field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True, allow_nan=False)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def coerce_price(value: Any) -> float | None:
    """Best-effort numeric parse for a scraped or model-written price.

    Handles ``"1,234.50"``, ``"Rs. 412.55"``, ``"412.55%"`` -> ``412.55`` and
    returns ``None`` for placeholders rather than raising.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value == value else None  # drop NaN
    text = str(value).strip()
    if not text or text.lower() in {"-", "n/a", "na", "none", "nil", "null", "tbd"}:
        return None
    for junk in ("Rs.", "Rs", "PKR", "₨", ",", "%", " "):
        text = text.replace(junk, "")
    try:
        return float(text)
    except ValueError:
        return None


def coerce_pct(value: Any) -> float | None:
    """Normalise a scraped percentage.

    PSX publishes changes as ``"(3.42)"`` for a fall and ``"3.42"`` for a rise;
    both are returned signed.  A trailing ``%`` is stripped.
    """

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("(") and text.endswith(")"):
            parsed = coerce_price(text[1:-1])
            return None if parsed is None else -abs(parsed)
        return coerce_price(text)
    return coerce_price(value)


# Forward references: PSXMarketPacket is declared above the market-structure
# models it embeds, so resolve those once at import time.
for _model in (PSXMarketPacket, DeskRun):
    _model.model_rebuild()
