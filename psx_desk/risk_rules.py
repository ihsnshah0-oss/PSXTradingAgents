"""Deterministic risk gates — the CRO's arithmetic, not the model's opinion.

The CRO node asks the model for the *judgement* (approve / reduce / reject, why,
what could go wrong).  Everything that can be computed is computed here first
and handed to the model as measured fact:

* free-float and turnover liquidity, plus how many sessions an exit would take;
* participation of the proposed order in 20-day ADV;
* position size from the risk budget at the stated stop;
* reward-to-risk against the desk floor;
* the Defaulters and circuit-lock vetoes.

Any gate the model cannot overrule is ``blocking=True``; the node's allocation
statement is then validated against these gates, so a persuasive narrative can
never outvote a Rs 3m-a-day tape.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from psx_desk.config import RiskLimits
from psx_desk.market_rules import board_is_tradeable, is_untradeable_segment
from psx_desk.schemas import (
    CircuitState,
    DefaulterSegment,
    FreeFloatCheck,
    GateStatus,
    LockState,
    PSXMarketPacket,
    RiskGate,
)

#: Largest book the desk will ever size, in PKR, regardless of conviction.
ABSOLUTE_TICKET_CAP_PKR = 25_000_000.0


@dataclass(frozen=True, slots=True)
class Sizing:
    """Everything the CRO needs to state a size, computed from levels."""

    shares: int
    allocation_pkr: float
    risk_per_share_pkr: float
    risk_budget_pkr: float
    weight_of_nav_pct: float
    participation_pct_of_adv: float | None
    days_to_exit: float | None
    capped_by: str


def position_size(
    *,
    entry_pkr: float,
    stop_pkr: float,
    limits: RiskLimits,
    adv_shares_20d: float | None,
) -> Sizing:
    """Size a long from the risk budget, then clamp to every desk limit.

    ``capped_by`` names the binding constraint, because "why only 40,000 shares"
    is the question a portfolio manager actually asks.
    """

    if entry_pkr <= 0:
        raise ValueError("entry price must be positive")
    risk_per_share = entry_pkr - stop_pkr
    if risk_per_share <= 0:
        return Sizing(0, 0.0, 0.0, 0.0, 0.0, None, None, "stop at or above entry")

    risk_budget = limits.nav_pkr * limits.max_risk_per_trade_pct / 100.0
    shares = int(risk_budget // risk_per_share)
    capped_by = "risk budget (1R)"

    max_weight_pkr = limits.nav_pkr * limits.max_single_name_weight_pct / 100.0
    if shares * entry_pkr > max_weight_pkr:
        shares = int(max_weight_pkr // entry_pkr)
        capped_by = "single-name weight cap"

    if shares * entry_pkr > ABSOLUTE_TICKET_CAP_PKR:
        shares = int(ABSOLUTE_TICKET_CAP_PKR // entry_pkr)
        capped_by = "absolute ticket cap"

    if adv_shares_20d and adv_shares_20d > 0:
        max_shares = int(adv_shares_20d * limits.max_adv_participation_pct / 100.0)
        if shares > max_shares:
            shares = max_shares
            capped_by = "ADV participation cap"

    allocation = round(shares * entry_pkr, 2)
    participation = (
        round(100.0 * shares / adv_shares_20d, 3) if adv_shares_20d else None
    )
    days_to_exit = None
    if adv_shares_20d and adv_shares_20d > 0 and shares:
        per_session = adv_shares_20d * limits.max_adv_participation_pct / 100.0
        days_to_exit = round(shares / per_session, 2) if per_session else None

    return Sizing(
        shares=shares,
        allocation_pkr=allocation,
        risk_per_share_pkr=round(risk_per_share, 4),
        risk_budget_pkr=round(risk_per_share * shares, 2),
        weight_of_nav_pct=round(100.0 * allocation / limits.nav_pkr, 4),
        participation_pct_of_adv=participation,
        days_to_exit=days_to_exit,
        capped_by=capped_by,
    )


def free_float_check(
    packet: PSXMarketPacket, limits: RiskLimits, *, proposed_shares: int | None = None
) -> FreeFloatCheck:
    """The liquidity gate: float, turnover, and how long an exit would take."""

    row = packet.quote
    adv_shares = packet.adv_shares_20d
    adv_value = packet.adv_value_pkr_20d
    participation = (
        round(100.0 * proposed_shares / adv_shares, 3)
        if proposed_shares and adv_shares
        else None
    )
    days_to_exit = None
    if proposed_shares and adv_shares:
        per_session = max(1.0, adv_shares * limits.max_adv_participation_pct / 100.0)
        days_to_exit = round(proposed_shares / per_session, 2)

    reasons: list[str] = []
    notes: list[str] = []
    float_unknown = row.free_float_mcap_pkr is None
    if float_unknown:
        # PSX does not publish free float on every surface.  A deep, genuine
        # turnover record is acceptable evidence of liquidity on its own; a thin
        # one with no float is not, and is treated as a breach.
        if adv_value is not None and adv_value >= 3 * limits.min_adv_value_pkr:
            notes.append("free float not published; turnover depth substitutes")
        else:
            reasons.append("free float not published and the tape is too thin to substitute")
    elif row.free_float_mcap_pkr < limits.min_free_float_mcap_pkr:
        reasons.append(
            f"free float Rs {row.free_float_mcap_pkr/1e6:,.1f}m below the "
            f"Rs {limits.min_free_float_mcap_pkr/1e6:,.0f}m floor"
        )
    if adv_value is None:
        reasons.append("no 20-session turnover history")
    elif adv_value < limits.min_adv_value_pkr:
        reasons.append(
            f"20-day turnover Rs {adv_value/1e6:,.2f}m below the "
            f"Rs {limits.min_adv_value_pkr/1e6:,.1f}m floor"
        )
    if row.close_pkr < limits.min_price_pkr:
        reasons.append(f"close Rs {row.close_pkr:,.2f} below the Rs {limits.min_price_pkr:,.0f} floor")
    if participation is not None and participation > limits.max_adv_participation_pct:
        reasons.append(
            f"order is {participation:.1f}% of ADV, above the "
            f"{limits.max_adv_participation_pct:.0f}% participation cap"
        )

    return FreeFloatCheck(
        free_float_mcap_pkr=row.free_float_mcap_pkr,
        adv_shares_20d=adv_shares,
        adv_value_pkr_20d=adv_value,
        proposed_shares=proposed_shares,
        participation_pct_of_adv=participation,
        days_to_exit=days_to_exit,
        passes=not reasons,
        comment="; ".join([*reasons, *notes]) if (reasons or notes) else "Liquidity gates met.",
    )


def _gate(
    gate: str,
    *,
    ok: bool,
    measured: str,
    limit: str,
    blocking: bool = True,
    unknown: bool = False,
    note: str = "",
) -> RiskGate:
    status = GateStatus.UNKNOWN if unknown else (GateStatus.PASS if ok else GateStatus.FAIL)
    return RiskGate(
        gate=gate,
        status=status,
        measured=measured,
        limit=limit,
        blocking=blocking,
        note=note,
    )


def build_gates(
    packet: PSXMarketPacket,
    limits: RiskLimits,
    *,
    liquidity: FreeFloatCheck,
    target_pkr: float | None = None,
    entry_pkr: float | None = None,
    stop_pkr: float | None = None,
    horizon_days: int | None = None,
) -> list[RiskGate]:
    """Every pre-trade gate, in the order a Karachi CRO would run them."""

    quote = packet.quote
    circuit: CircuitState = packet.circuit
    defaulter: DefaulterSegment = packet.defaulter
    gates: list[RiskGate] = []

    # 1. Defaulters / Non-Compliant segment — absolute veto.
    gates.append(
        _gate(
            "defaulter_segment",
            ok=not defaulter.is_defaulting,
            unknown=not defaulter.checked,
            measured=f"segment={defaulter.segment}",
            limit="not on the PSX Defaulters / Non-Compliant segment",
            note=defaulter.evidence,
        )
    )

    # 2. Trading board — the mandate is the Ready market only.
    gates.append(
        _gate(
            "trading_board",
            ok=board_is_tradeable(quote.board),
            measured=f"board={quote.board}",
            limit="REG (Ready market)",
            note="" if board_is_tradeable(quote.board) else "Side boards are outside the mandate.",
        )
    )

    # 3. Circuit lock — a locked scrip cannot be worked at the close.
    locked = circuit.locked is not LockState.NONE
    gates.append(
        _gate(
            "circuit_lock",
            ok=not locked,
            measured=f"close Rs {quote.close_pkr:,.2f}, caps "
            f"Rs {circuit.lower_cap_pkr:,.2f}-{circuit.upper_cap_pkr:,.2f}, "
            f"locked={circuit.locked.value}",
            limit=f"inside the {circuit.band_pct:g}% scrip band (PSX Reg. 19.3)",
            blocking=False,
            note=circuit.execution_note,
        )
    )

    # 4. Free float / turnover.
    gates.append(
        _gate(
            "free_float_liquidity",
            ok=liquidity.passes,
            unknown=liquidity.adv_value_pkr_20d is None,
            measured=(
                f"float mcap Rs {(liquidity.free_float_mcap_pkr or 0)/1e6:,.1f}m, "
                f"20d turnover Rs {(liquidity.adv_value_pkr_20d or 0)/1e6:,.2f}m"
            ),
            limit=(
                f"float >= Rs {limits.min_free_float_mcap_pkr/1e6:,.0f}m and turnover >= "
                f"Rs {limits.min_adv_value_pkr/1e6:,.1f}m"
            ),
            note=liquidity.comment,
        )
    )

    # 5. Price floor.
    gates.append(
        _gate(
            "price_floor",
            ok=quote.close_pkr >= limits.min_price_pkr,
            measured=f"close Rs {quote.close_pkr:,.2f}",
            limit=f">= Rs {limits.min_price_pkr:,.2f}",
            blocking=False,
        )
    )

    # 6. Stop discipline and reward-to-risk.
    if entry_pkr and stop_pkr and entry_pkr > stop_pkr:
        stop_pct = round(100.0 * (entry_pkr - stop_pkr) / entry_pkr, 3)
        gates.append(
            _gate(
                "stop_distance",
                ok=stop_pct <= limits.max_stop_pct,
                measured=f"{stop_pct:.2f}% from entry",
                limit=f"<= {limits.max_stop_pct:.1f}%",
                blocking=False,
            )
        )
        if target_pkr and target_pkr > entry_pkr:
            rr = round((target_pkr - entry_pkr) / (entry_pkr - stop_pkr), 3)
            gates.append(
                _gate(
                    "reward_risk",
                    ok=rr >= limits.min_reward_risk,
                    measured=f"{rr:.2f}R",
                    limit=f">= {limits.min_reward_risk:.2f}R",
                    note="A sub-1.5R idea is not worth the desk's attention.",
                )
            )
    else:
        gates.append(
            _gate(
                "reward_risk",
                ok=False,
                unknown=True,
                measured="no entry/stop pair from research",
                limit=f">= {limits.min_reward_risk:.2f}R",
                blocking=False,
                note="The debate did not produce tradable levels.",
            )
        )

    # 7. Holding horizon.
    if horizon_days is not None:
        gates.append(
            _gate(
                "horizon",
                ok=horizon_days <= limits.max_target_horizon_days,
                measured=f"{horizon_days} sessions",
                limit=f"<= {limits.max_target_horizon_days} sessions",
                blocking=False,
            )
        )

    # 8. Data integrity — the desk does not trade a partial packet blind.
    gates.append(
        _gate(
            "data_integrity",
            ok=packet.data_quality.value == "verified",
            measured=f"quality={packet.data_quality.value}, missing={len(packet.missing_fields)}",
            limit="verified PSX packet",
            blocking=False,
            note=", ".join(packet.missing_fields[:6]),
        )
    )

    return gates


def blocking_failures(gates: Iterable[RiskGate]) -> list[RiskGate]:
    """Gates that veto the trade outright."""

    return [gate for gate in gates if gate.blocking and gate.status is GateStatus.FAIL]


def unknown_blocking(gates: Iterable[RiskGate]) -> list[RiskGate]:
    """Blocking gates the desk could not evaluate (treated as a veto for sizing)."""

    return [gate for gate in gates if gate.blocking and gate.status is GateStatus.UNKNOWN]


def veto_reason(gates: Iterable[RiskGate]) -> str | None:
    """First hard veto, phrased for the allocation statement."""

    failures = blocking_failures(gates)
    if failures:
        gate = failures[0]
        return f"{gate.gate} failed: {gate.measured} (limit {gate.limit})"
    return None


def segment_veto(defaulter: DefaulterSegment, board: str | None) -> str | None:
    """The two structural vetoes that need no arithmetic."""

    if defaulter.is_defaulting:
        return f"{defaulter.symbol} sits on the PSX {defaulter.segment} segment."
    if is_untradeable_segment(defaulter.segment):
        return f"{defaulter.symbol} segment '{defaulter.segment}' is outside the mandate."
    if not board_is_tradeable(board):
        return f"{board} board is outside the Ready-market mandate."
    return None
