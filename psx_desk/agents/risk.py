"""Node 5 — the CRO gatekeeper.

The CRO is the only node that produces something executable, so it is the node
with the least model discretion.  Order of operations:

1. build the debate transcript deterministically;
2. pull tradable levels from the stronger side of the debate;
3. size the position from the desk's risk budget and clamp it to every limit;
4. compute every gate (Defaulters segment, board, circuit, float, turnover,
   reward-to-risk, data integrity);
5. ask the model to adjudicate — approve, reduce or reject — with all of the
   above as measured facts;
6. **then** overwrite the model's numbers with the desk's: a failed gate forces
   REJECT, size is clamped to the computed maximum, shares and weight are
   recomputed, and the schema validators run last.

A fluent paragraph cannot outvote a Rs 3m-a-day tape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psx_desk.agents.llm import StructuredInvoker
from psx_desk.agents.prompts import CRO_SYSTEM, cro_user
from psx_desk.agents.research import build_transcript
from psx_desk.config import DeskConfig
from psx_desk.risk_rules import (
    build_gates,
    free_float_check,
    position_size,
    segment_veto,
    unknown_blocking,
    veto_reason,
)
from psx_desk.schemas import (
    Action,
    AllocationStatement,
    Conviction,
    DebateArgument,
    Decision,
    GateStatus,
    PSXMarketPacket,
)

logger = logging.getLogger(__name__)


def _reference_levels(
    packet: PSXMarketPacket, debate_args: list[DebateArgument]
) -> tuple[float, float | None, float | None]:
    """Entry, stop and target implied by the strongest argument.

    Entry is the session close — the desk trades what it can see, not a level it
    hopes for.  Stop and target come from the winning side's own turn.
    """

    spot = packet.quote.close_pkr
    stop = None
    target = None
    for argument in reversed(debate_args):
        if argument.stop_pkr:
            stop = argument.stop_pkr
        if argument.target_price_pkr:
            target = argument.target_price_pkr
        if stop and target:
            break
    return spot, stop, target


def _limits_payload(config: DeskConfig) -> dict[str, Any]:
    limits = config.risk
    return {
        "nav_pkr": limits.nav_pkr,
        "max_single_name_weight_pct": limits.max_single_name_weight_pct,
        "max_risk_per_trade_pct": limits.max_risk_per_trade_pct,
        "max_adv_participation_pct": limits.max_adv_participation_pct,
        "min_free_float_mcap_pkr": limits.min_free_float_mcap_pkr,
        "min_adv_value_pkr": limits.min_adv_value_pkr,
        "min_price_pkr": limits.min_price_pkr,
        "max_stop_pct": limits.max_stop_pct,
        "min_reward_risk": limits.min_reward_risk,
        "max_target_horizon_days": limits.max_target_horizon_days,
        "circuit_band_pct": config.circuit_band_pct,
    }


def make_cro(invoker: StructuredInvoker, config: DeskConfig) -> Callable[[dict], dict]:
    """Build the CRO node bound to one invoker and configuration."""

    def cro(state: dict) -> dict:
        packet: PSXMarketPacket = state["packet"]
        technical = state["technical"]
        fundamental = state["fundamental"]
        limits = config.risk

        bull: list[DebateArgument] = list(state.get("bull_arguments") or [])
        bear: list[DebateArgument] = list(state.get("bear_arguments") or [])
        transcript = build_transcript(packet.symbol, bull, bear)

        # ---- 1. levels and size -------------------------------------- #
        if transcript.stronger_case == "bear":
            side = bear
        elif transcript.stronger_case == "bull":
            side = bull
        else:
            side = [*bull, *bear]

        spot, stop, target = _reference_levels(packet, side)
        if stop is None or stop >= spot:
            # A long with no valid stop cannot be sized; allow a technical
            # invalidation level to stand in when it is below spot.
            candidate = technical.invalidation_pkr
            if candidate and candidate < spot:
                stop = candidate
            else:
                stop = round(spot * (1 - limits.max_stop_pct / 100.0), 2)

        sizing = position_size(
            entry_pkr=spot,
            stop_pkr=stop,
            limits=limits,
            adv_shares_20d=packet.adv_shares_20d,
        )
        liquidity = free_float_check(packet, limits, proposed_shares=sizing.shares)
        gates = build_gates(
            packet,
            limits,
            liquidity=liquidity,
            entry_pkr=spot,
            stop_pkr=stop,
            target_pkr=target,
            horizon_days=bull[-1].horizon_days if bull and bull[-1].horizon_days else None,
        )
        hard_veto = segment_veto(packet.defaulter, packet.quote.board) or veto_reason(gates)
        unknown = unknown_blocking(gates)

        # ---- 2. the model adjudicates -------------------------------- #
        statement = invoker.invoke(
            AllocationStatement,
            system=CRO_SYSTEM,
            user=cro_user(
                packet,
                technical,
                fundamental,
                transcript,
                [gate.model_dump(mode="json") for gate in gates],
                {
                    "entry_pkr": spot,
                    "stop_pkr": stop,
                    "target_pkr": target,
                    "shares": sizing.shares,
                    "allocation_pkr": sizing.allocation_pkr,
                    "risk_budget_pkr": sizing.risk_budget_pkr,
                    "weight_of_nav_pct": sizing.weight_of_nav_pct,
                    "participation_pct_of_adv": sizing.participation_pct_of_adv,
                    "days_to_exit": sizing.days_to_exit,
                    "capped_by": sizing.capped_by,
                },
                _limits_payload(config),
            ),
            tier="deep",
        )

        # ---- 3. the desk overwrites what it measured ----------------- #
        statement.symbol = packet.symbol
        statement.as_of = packet.trade_date
        statement.nav_pkr = limits.nav_pkr
        statement.gates = gates
        statement.free_float = liquidity
        statement.defaulter = packet.defaulter
        statement.circuit = packet.circuit
        statement.data_quality = packet.data_quality
        statement.stronger_case = transcript.stronger_case

        notes: list[str] = []
        if hard_veto:
            if statement.decision is not Decision.REJECT:
                notes.append(f"Overridden to REJECT: {hard_veto}")
            statement.decision = Decision.REJECT
            statement.action = Action.NO_TRADE
            statement.allocation_pkr = 0.0
            statement.shares = 0
            statement.kill_switches = [
                *statement.kill_switches,
                f"Veto active: {hard_veto}",
            ]
        else:
            if statement.decision is Decision.REJECT and not statement.allocation_pkr:
                pass  # the model may still stand down on judgement; honour it
            else:
                cap = sizing.allocation_pkr
                if statement.decision is Decision.APPROVE_REDUCED:
                    cap = round(cap / 2.0, 2)
                if unknown:
                    cap = round(cap / 2.0, 2)
                    notes.append(
                        "Size halved: gate(s) "
                        + ", ".join(gate.gate for gate in unknown)
                        + " could not be measured."
                    )
                over_cap = statement.allocation_pkr > cap or statement.allocation_pkr <= 0
                if over_cap and statement.decision is not Decision.REJECT:
                    if statement.allocation_pkr > cap:
                        notes.append(
                            f"Allocation clamped from PKR {statement.allocation_pkr:,.0f} "
                            f"to PKR {cap:,.0f} ({sizing.capped_by})."
                        )
                    statement.allocation_pkr = cap
                if statement.decision is Decision.APPROVE and cap <= 0:
                    statement.decision = Decision.REJECT
                    statement.action = Action.NO_TRADE
                    statement.allocation_pkr = 0.0
                    notes.append("No size available under the desk's limits; stood down.")

        # Levels: the CRO may state its own, but a long must sit above its stop.
        statement.entry_limit_pkr = statement.entry_limit_pkr or spot
        if statement.action in {Action.BUY, Action.HOLD}:
            statement.stop_pkr = statement.stop_pkr or stop
            statement.target_pkr = statement.target_pkr or target
        statement.max_hold_days = statement.max_hold_days or limits.max_target_horizon_days
        if statement.size_cap_reason:
            statement.size_cap_reason = " | ".join([sizing.capped_by, statement.size_cap_reason])
        else:
            statement.size_cap_reason = sizing.capped_by

        if notes:
            statement.conditions = list(dict.fromkeys([*statement.conditions, *notes]))
            statement.rationale = statement.rationale.rstrip() + " " + " ".join(notes)

        # Any DISTINCT/WARN gate worth surfacing goes into the kill switches.
        for gate in gates:
            if gate.status is GateStatus.WARN:
                statement.kill_switches.append(f"{gate.gate}: {gate.measured}")

        # Re-validate: the model's fields now have to satisfy the invariants.
        statement = AllocationStatement.model_validate(statement.model_dump())

        logger.info(
            "%s: %s %s size=PKR %.0f shares=%d gates=%s",
            packet.symbol,
            statement.decision.value,
            statement.action.value,
            statement.allocation_pkr,
            statement.shares,
            ",".join(f"{g.gate}:{g.status.value}" for g in gates),
        )
        return {"allocation": statement, "debate": transcript, "conviction": statement.conviction}

    return cro


def conviction_from_score(score: float) -> Conviction:
    if score >= 0.7:
        return Conviction.HIGH
    if score >= 0.4:
        return Conviction.MEDIUM
    return Conviction.LOW
