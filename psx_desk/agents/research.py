"""Node 4 — the adversarial debate (Bull vs Bear).

Two separate agents argue the same PSX packet over ``max_debate_rounds`` rounds.
Each turn must rebut the other side's last argument and carry at least two
evidence points; the transcript is then assembled deterministically, including a
transparent evidence weighting that names the stronger case and flags debates
that went circular.  The CRO reads the transcript, not the chat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

from psx_desk.agents.llm import StructuredInvoker
from psx_desk.agents.prompts import BEAR_SYSTEM, BULL_SYSTEM, bear_user, bull_user
from psx_desk.schemas import (
    DebateArgument,
    DebateTranscript,
    FundamentalReport,
    PSXMarketPacket,
    Stance,
    TechnicalReport,
    Verification,
)

logger = logging.getLogger(__name__)

#: How much an evidence point counts toward the desk's "who won" weighting.
_VERIFICATION_WEIGHT = {
    Verification.HARD_DATA: 1.0,
    Verification.DERIVED: 0.6,
    Verification.INFERENCE: 0.2,
}


def _expected_return(target: float | None, spot: float) -> float | None:
    if not target or spot <= 0:
        return None
    return round(100.0 * (target - spot) / spot, 3)


def make_bull_researcher(invoker: StructuredInvoker) -> Callable[[dict], dict]:
    def bull_researcher(state: dict) -> dict:
        packet: PSXMarketPacket = state["packet"]
        technical: TechnicalReport = state["technical"]
        fundamental: FundamentalReport = state["fundamental"]
        bull_history: list[DebateArgument] = list(state.get("bull_arguments") or [])
        bear_history: list[DebateArgument] = list(state.get("bear_arguments") or [])
        # The bull opens each round, so its round number follows the bear's turns.
        round_number = len(bear_history) + 1

        argument = invoker.invoke(
            DebateArgument,
            system=BULL_SYSTEM,
            user=bull_user(
                packet,
                technical,
                fundamental,
                round_number=round_number,
                prior_bear=bear_history[-1] if bear_history else None,
                own_prior=bull_history[-1] if bull_history else None,
            ),
            tier="quick",
        )
        argument.symbol = packet.symbol
        argument.stance = Stance.BULL
        argument.round = round_number
        argument.expected_return_pct = _expected_return(
            argument.target_price_pkr, packet.quote.close_pkr
        )
        return {"bull_arguments": [*bull_history, argument]}

    return bull_researcher


def make_bear_researcher(invoker: StructuredInvoker) -> Callable[[dict], dict]:
    def bear_researcher(state: dict) -> dict:
        packet: PSXMarketPacket = state["packet"]
        technical: TechnicalReport = state["technical"]
        fundamental: FundamentalReport = state["fundamental"]
        bull_history: list[DebateArgument] = list(state.get("bull_arguments") or [])
        bear_history: list[DebateArgument] = list(state.get("bear_arguments") or [])
        round_number = max(1, len(bull_history))

        argument = invoker.invoke(
            DebateArgument,
            system=BEAR_SYSTEM,
            user=bear_user(
                packet,
                technical,
                fundamental,
                round_number=round_number,
                prior_bull=bull_history[-1] if bull_history else None,
                own_prior=bear_history[-1] if bear_history else None,
            ),
            tier="quick",
        )
        argument.symbol = packet.symbol
        argument.stance = Stance.BEAR
        argument.round = round_number
        # A bear target is a downside level; express it as a negative return.
        argument.expected_return_pct = _expected_return(
            argument.target_price_pkr, packet.quote.close_pkr
        )
        return {"bear_arguments": [*bear_history, argument]}

    return bear_researcher


# --------------------------------------------------------------------------- #
# Deterministic transcript
# --------------------------------------------------------------------------- #


def evidence_weight(arguments: Sequence[DebateArgument]) -> float:
    """Conviction-weighted, provenance-weighted strength of one side."""

    total = 0.0
    for argument in arguments:
        points = sum(_VERIFICATION_WEIGHT.get(p.verification, 0.3) for p in argument.key_points)
        total += argument.conviction * (1.0 + points)
    return round(total, 4)


def _shared_metrics(bull: Sequence[DebateArgument], bear: Sequence[DebateArgument]) -> list[str]:
    bull_metrics = {
        point.metric.strip().lower()
        for argument in bull
        for point in argument.key_points
        if point.metric.strip()
    }
    bear_metrics = {
        point.metric.strip().lower()
        for argument in bear
        for point in argument.key_points
        if point.metric.strip()
    }
    return sorted(bull_metrics & bear_metrics)


def build_transcript(
    symbol: str,
    bull: Sequence[DebateArgument],
    bear: Sequence[DebateArgument],
) -> DebateTranscript:
    """Assemble the transcript the CRO adjudicates.

    ``stronger_case`` is a documented weighting (conviction x evidence quality),
    not a vibe: hard PSX data counts 1.0, derived indicators 0.6, inference 0.2.
    A tie within 5% is reported as ``balanced``.
    """

    if not bull or not bear:
        raise ValueError(f"{symbol}: debate needs at least one turn from each side")

    bull_weight = evidence_weight(bull)
    bear_weight = evidence_weight(bear)
    if bull_weight <= 0 and bear_weight <= 0 or abs(bull_weight - bear_weight) <= 0.05 * max(bull_weight, bear_weight, 1e-9):
        stronger = "balanced"
    else:
        stronger = "bull" if bull_weight > bear_weight else "bear"

    hard_points = sum(
        1
        for argument in list(bull) + list(bear)
        for point in argument.key_points
        if point.verification is Verification.HARD_DATA
    )
    shared = _shared_metrics(bull, bear)
    total_points = sum(len(a.key_points) for a in list(bull) + list(bear))
    if total_points and hard_points / total_points < 0.25:
        quality = "thin"
    elif len(shared) >= 3 and total_points <= 4:
        quality = "circular"
    else:
        quality = "strong"

    consensus = list(
        dict.fromkeys(
            [point for argument in list(bull) + list(bear) for point in argument.concedes]
        )
    )
    disagreements = [
        f"{metric}: both sides cited it, and they read it differently"
        for metric in shared
    ]

    last_bull, last_bear = bull[-1], bear[-1]
    summary = (
        f"Bull conviction {last_bull.conviction:.2f}"
        + (
            f" with a target of PKR {last_bull.target_price_pkr:,.2f}"
            f" ({last_bull.expected_return_pct:+.1f}%)"
            if last_bull.target_price_pkr and last_bull.expected_return_pct is not None
            else " with no stated target"
        )
        + f" over {len(bull)} round(s); Bear conviction {last_bear.conviction:.2f}"
        + (
            f" arguing down to PKR {last_bear.target_price_pkr:,.2f}"
            if last_bear.target_price_pkr
            else " with no stated downside level"
        )
        + f" over {len(bear)} round(s). Desk weighting: bull {bull_weight:.2f} vs bear "
        f"{bear_weight:.2f}, so the {stronger} case carries the argument. "
        f"{hard_points} hard-data point(s) across {total_points} claim(s); "
        f"{len(shared)} metric(s) remain in dispute; {len(consensus)} point(s) conceded."
    )

    return DebateTranscript(
        symbol=symbol,
        rounds_completed=max(len(bull), len(bear)),
        bull=list(bull),
        bear=list(bear),
        consensus=consensus,
        open_disagreements=disagreements,
        stronger_case=stronger,
        debate_quality=quality,
        summary=summary,
    )
