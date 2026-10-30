"""Render a run as the two artifacts a desk actually circulates.

* a **markdown research note** for humans (investment committee, sales desk);
* the **strict allocation JSON** for the order-management side.

Both are generated from the same :class:`~psx_desk.schemas.DeskRun`, so the note
cannot drift from the decision, and every number in the note points back at a
PSX URL in the provenance table.
"""

from __future__ import annotations

from psx_desk.schemas import DeskRun, GateStatus

_MONEY = "{:,.2f}"


def _pkr(value: float | None) -> str:
    return "n/a" if value is None else _MONEY.format(value)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def _gate_marker(status: GateStatus) -> str:
    return {
        GateStatus.PASS: "PASS",
        GateStatus.WARN: "WARN",
        GateStatus.FAIL: "FAIL",
        GateStatus.UNKNOWN: "UNKNOWN",
    }[status]


def render_markdown(run: DeskRun) -> str:
    """The investment-committee note."""

    packet = run.packet
    quote = packet.quote
    alloc = run.allocation
    technical = run.technical
    fundamental = run.fundamental
    debate = run.debate

    lines: list[str] = []
    lines.append(f"# {packet.symbol} — PSX desk note, {run.trade_date.isoformat()}")
    lines.append("")
    lines.append(
        f"**Desk call:** {alloc.decision.value} / {alloc.action.value} "
        f"({alloc.conviction.value} conviction) · "
        f"size PKR {_pkr(alloc.allocation_pkr)} ({alloc.weight_of_nav_pct:.2f}% of NAV) · "
        f"{alloc.shares:,} shares"
    )
    lines.append("")
    lines.append(
        f"**Session print:** PKR {_pkr(quote.close_pkr)} "
        f"({_pct(quote.change_pct)}), volume {quote.volume_shares or 0:,.0f} shares, "
        f"turnover PKR {_pkr(quote.value_pkr)} · board {quote.board}"
    )
    lines.append("")
    lock_text = (
        "no circuit lock"
        if packet.circuit.locked.value == "none"
        else f"{packet.circuit.locked.value} circuit lock"
    )
    lines.append(
        f"**Circuit:** {lock_text} · band {packet.circuit.band_pct:g}% · "
        f"caps PKR {_pkr(packet.circuit.lower_cap_pkr)} – {_pkr(packet.circuit.upper_cap_pkr)} "
        f"({packet.circuit.cap_source}) · {packet.circuit.execution_note}"
    )
    lines.append("")
    lines.append(
        f"**Defaulters segment:** {packet.defaulter.segment} "
        f"({'checked' if packet.defaulter.checked else 'UNVERIFIED'}) — {packet.defaulter.evidence}"
    )
    if packet.macro is not None:
        macro = packet.macro
        lines.append(
            f"**Macro:** SBP policy rate {macro.sbp_policy_rate_pct:.2f}% "
            f"(corridor {macro.corridor_floor_pct or 0:.2f}–{macro.corridor_ceiling_pct or 0:.2f}%), "
            f"CPI {macro.cpi_yoy_pct if macro.cpi_yoy_pct is not None else 'n/a'}% y/y, "
            f"real policy rate {macro.real_policy_rate_pct}% "
            f"[{macro.basis}, {macro.quality.value}]"
        )
    lines.append("")

    lines.append("## Allocation statement")
    lines.append("")
    lines.append("```json")
    lines.append(alloc.to_json())
    lines.append("```")
    lines.append("")

    lines.append("## Risk gates")
    lines.append("")
    lines.append("| Gate | Status | Measured | Limit | Blocking |")
    lines.append("| --- | --- | --- | --- | --- |")
    for gate in alloc.gates:
        lines.append(
            f"| {gate.gate} | {_gate_marker(gate.status)} | {gate.measured} | "
            f"{gate.limit} | {'yes' if gate.blocking else 'no'} |"
        )
    lines.append("")
    if alloc.size_cap_reason:
        lines.append(f"**Size cap:** {alloc.size_cap_reason}")
        lines.append("")
    if alloc.conditions:
        lines.append("**Conditions before the order is worked:**")
        lines.append("")
        lines.extend(f"- {condition}" for condition in alloc.conditions)
        lines.append("")
    if alloc.kill_switches:
        lines.append("**Kill switches:**")
        lines.append("")
        lines.extend(f"- {switch}" for switch in alloc.kill_switches)
        lines.append("")

    lines.append("## CRO rationale")
    lines.append("")
    lines.append(alloc.rationale)
    lines.append("")

    lines.append("## Technical read")
    lines.append("")
    lines.append(
        f"Trend **{technical.trend}**, momentum {technical.momentum_score}/100, "
        f"RSI(14) {technical.rsi_14}, ATR(14) PKR {_pkr(technical.atr_14_pkr)}, "
        f"vol {technical.volatility_annualised_pct}% annualised, "
        f"support PKR {_pkr(technical.support_pkr)}, resistance PKR {_pkr(technical.resistance_pkr)}."
    )
    lines.append("")
    lines.append(technical.narrative)
    lines.append("")
    if technical.signals:
        lines.append("| Signal | Reading | Implication | Basis |")
        lines.append("| --- | --- | --- | --- |")
        for signal in technical.signals:
            lines.append(
                f"| {signal.name} | {signal.reading} | {signal.implication} | "
                f"{signal.verification.value} |"
            )
        lines.append("")

    lines.append("## Fundamental read")
    lines.append("")
    lines.append(
        f"Valuation **{fundamental.verdict}**, score {fundamental.score}/100, "
        f"PE {fundamental.pe_ratio}, earnings yield {fundamental.earnings_yield_pct}%, "
        f"equity risk premium vs SBP {fundamental.equity_risk_premium_pct}%, "
        f"real earnings yield {fundamental.real_earnings_yield_pct}%, "
        f"macro sensitivity {fundamental.macro_sensitivity}."
    )
    lines.append("")
    lines.append(fundamental.narrative)
    lines.append("")
    if fundamental.rate_path_note:
        lines.append(f"**Rate path:** {fundamental.rate_path_note}")
        lines.append("")
    if fundamental.red_flags:
        lines.append("| Red flag | Severity | Evidence |")
        lines.append("| --- | --- | --- |")
        for flag in fundamental.red_flags:
            lines.append(f"| {flag.flag} | {flag.severity} | {flag.evidence} |")
        lines.append("")

    lines.append("## Bull vs bear")
    lines.append("")
    lines.append(debate.summary)
    lines.append("")
    lines.append(
        f"Stronger case: **{debate.stronger_case}** · debate quality: **{debate.debate_quality}** · "
        f"{debate.rounds_completed} round(s)"
    )
    lines.append("")
    for label, turns in (("Bull", debate.bull), ("Bear", debate.bear)):
        for turn in turns:
            target = f" · target PKR {_pkr(turn.target_price_pkr)}" if turn.target_price_pkr else ""
            lines.append(f"**{label} R{turn.round}** (conviction {turn.conviction:.2f}{target})")
            lines.append("")
            lines.append(turn.thesis)
            lines.append("")
            lines.extend(
                f"- {point.claim} — *{point.metric}: {point.value}* ({point.verification.value})"
                for point in turn.key_points
            )
            if turn.rebuttal:
                lines.append("")
                lines.append(f"_Rebuttal:_ {turn.rebuttal}")
            lines.append("")

    if packet.disclosures:
        lines.append("## PSX disclosures")
        lines.append("")
        for item in packet.disclosures:
            when = item.published.isoformat() if item.published else "undated"
            lines.append(f"- {when} · [{item.category}] {item.title} — {item.url}")
        lines.append("")

    lines.append("## Data provenance")
    lines.append("")
    lines.append(f"Packet quality: **{packet.data_quality.value}**")
    lines.append("")
    if packet.missing_fields:
        lines.append(f"Missing fields (not estimated): {', '.join(packet.missing_fields)}")
        lines.append("")
    lines.append("| Source | As of | URL | SHA-256 |")
    lines.append("| --- | --- | --- | --- |")
    for ref in packet.sources:
        as_of = ref.as_of.isoformat() if ref.as_of else "—"
        digest = (ref.sha256 or "")[:12]
        lines.append(f"| {ref.label} | {as_of} | {ref.url} | {digest} |")
    lines.append("")
    if packet.notes or run.notes:
        lines.append("## Desk notes")
        lines.append("")
        for note in [*packet.notes, *run.notes]:
            lines.append(f"- {note}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Run {run.run_id} · generated {run.finished_at.isoformat() if run.finished_at else 'in progress'} "
        f"· all market data from psx.com.pk / dps.psx.com.pk; macro from sbp.org.pk. "
        f"Not investment advice._"
    )
    return "\n".join(lines)


def render_decision_json(run: DeskRun) -> str:
    """The strict allocation artifact handed to the order pad."""

    return run.allocation.to_json()


__all__ = ["render_decision_json", "render_markdown"]
