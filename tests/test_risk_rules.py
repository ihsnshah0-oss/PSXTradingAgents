"""The CRO's arithmetic: sizing clamps, liquidity gates and structural vetoes."""

from __future__ import annotations

import pytest

from psx_desk.config import RiskLimits
from psx_desk.risk_rules import (
    blocking_failures,
    build_gates,
    free_float_check,
    position_size,
    segment_veto,
    unknown_blocking,
    veto_reason,
)
from psx_desk.schemas import (
    CircuitState,
    DataQuality,
    DefaulterSegment,
    GateStatus,
    LockState,
    MacroBackdrop,
    PSXMarketPacket,
    PSXQuote,
)

LIMITS = RiskLimits()


def circuit(**kwargs) -> CircuitState:
    base = {
        "band_pct": 5.0, "upper_cap_pkr": 105.0, "lower_cap_pkr": 95.0, "close_pkr": 100.0,
        "locked": LockState.NONE, "distance_to_upper_pct": 5.0, "distance_to_lower_pct": 5.0,
    }
    base.update(kwargs)
    return CircuitState(**base)


def packet(**overrides):
    base = {
        "symbol": "SYS",
        "trade_date": "2026-10-29",
        "quote": PSXQuote(
            symbol="SYS",
            trade_date="2026-10-29",
            close_pkr=100.0,
            prev_close_pkr=98.0,
            free_float_mcap_pkr=9_000_000_000.0,
            board="REG",
        ),
        "circuit": circuit(),
        "defaulter": DefaulterSegment(symbol="SYS", checked=True, segment="regular"),
        "macro": MacroBackdrop(sbp_policy_rate_pct=12.0, cpi_yoy_pct=10.3),
        "adv_shares_20d": 1_000_000,
        "adv_value_pkr_20d": 100_000_000.0,
        "data_quality": DataQuality.VERIFIED,
    }
    base.update(overrides)
    return PSXMarketPacket(**base)


def test_size_is_driven_by_the_risk_budget():
    sizing = position_size(entry_pkr=100.0, stop_pkr=90.0, limits=LIMITS, adv_shares_20d=None)
    # 1% of a Rs 50m NAV is Rs 500k, and 10% of risk per share -> 50,000 shares.
    assert sizing.shares == 50_000
    assert sizing.allocation_pkr == pytest.approx(5_000_000.0)
    assert sizing.risk_budget_pkr == pytest.approx(500_000.0)
    assert sizing.weight_of_nav_pct == pytest.approx(10.0)


def test_concentration_cap_binds_a_very_tight_stop():
    wide_limits = RiskLimits(nav_pkr=50_000_000.0, max_single_name_weight_pct=2.0)
    sizing = position_size(
        entry_pkr=100.0, stop_pkr=99.0, limits=wide_limits, adv_shares_20d=10_000_000
    )
    assert sizing.allocation_pkr <= 1_000_000
    assert sizing.capped_by == "single-name weight cap"


def test_adv_participation_cap_binds_an_illiquid_tape():
    sizing = position_size(entry_pkr=100.0, stop_pkr=90.0, limits=LIMITS, adv_shares_20d=100_000)
    assert sizing.shares == 15_000
    assert sizing.capped_by == "ADV participation cap"
    assert sizing.participation_pct_of_adv == pytest.approx(15.0)


def test_a_stop_above_entry_produces_no_size():
    sizing = position_size(entry_pkr=100.0, stop_pkr=105.0, limits=LIMITS, adv_shares_20d=None)
    assert sizing.shares == 0
    assert sizing.capped_by == "stop at or above entry"


def test_liquidity_gate_passes_a_deep_name():
    check = free_float_check(packet(), LIMITS, proposed_shares=50_000)
    assert check.passes
    assert check.days_to_exit is not None


def test_liquidity_gate_fails_a_thin_name():
    thin = packet(adv_value_pkr_20d=1_000_000.0, adv_shares_20d=10_000)
    check = free_float_check(thin, LIMITS, proposed_shares=50_000)
    assert not check.passes
    assert "below the" in check.comment


def test_unpublished_float_can_be_substituted_by_turnover_depth():
    no_float = packet(quote=PSXQuote(symbol="SYS", trade_date="2026-10-29", close_pkr=100.0))
    check = free_float_check(no_float, LIMITS, proposed_shares=10_000)
    assert check.passes
    assert "turnover depth substitutes" in check.comment


def test_unpublished_float_with_a_thin_tape_fails():
    both_missing = packet(
        quote=PSXQuote(symbol="SYS", trade_date="2026-10-29", close_pkr=100.0),
        adv_value_pkr_20d=2_000_000.0,
    )
    check = free_float_check(both_missing, LIMITS, proposed_shares=1_000)
    assert not check.passes


def test_gates_cover_the_structural_checks():
    liquidity = free_float_check(packet(), LIMITS, proposed_shares=10_000)
    gates = build_gates(
        packet(), LIMITS, liquidity=liquidity, entry_pkr=100.0, stop_pkr=92.0, target_pkr=120.0
    )
    names = [gate.gate for gate in gates]
    assert names[0] == "defaulter_segment"
    assert {"trading_board", "circuit_lock", "free_float_liquidity", "reward_risk"} <= set(names)
    assert all(gate.status is not GateStatus.FAIL for gate in gates)


def test_defaulters_segment_produces_a_blocking_failure_and_a_veto():
    defaulting = packet(
        defaulter=DefaulterSegment(
            symbol="SYS", checked=True, is_defaulting=True, segment="non_compliant"
        )
    )
    liquidity = free_float_check(defaulting, LIMITS)
    gates = build_gates(defaulting, LIMITS, liquidity=liquidity)
    assert blocking_failures(gates)
    assert "defaulter" in (veto_reason(gates) or "")
    assert segment_veto(defaulting.defaulter, "REG")


def test_unknown_gate_is_not_a_veto_but_is_surfaced():
    unverified = packet(
        defaulter=DefaulterSegment(symbol="SYS", checked=False, segment="unknown")
    )
    liquidity = free_float_check(unverified, LIMITS)
    gates = build_gates(unverified, LIMITS, liquidity=liquidity)
    assert [gate.gate for gate in unknown_blocking(gates)] == ["defaulter_segment"]
    assert veto_reason(gates) is None


def test_circuit_lock_gate_is_advisory_not_blocking():
    locked = packet(circuit=circuit(locked=LockState.UPPER, close_pkr=105.0))
    liquidity = free_float_check(locked, LIMITS)
    gates = build_gates(locked, LIMITS, liquidity=liquidity)
    circuit_gate = next(gate for gate in gates if gate.gate == "circuit_lock")
    assert circuit_gate.blocking is False
    assert circuit_gate.status is GateStatus.FAIL


def test_side_boards_are_outside_the_mandate():
    odd_lot = packet(quote=PSXQuote(symbol="SYS", trade_date="2026-10-29", close_pkr=100.0, board="ODL"))
    liquidity = free_float_check(odd_lot, LIMITS)
    gates = build_gates(odd_lot, LIMITS, liquidity=liquidity)
    board_gate = next(gate for gate in gates if gate.gate == "trading_board")
    assert board_gate.status is GateStatus.FAIL
    assert segment_veto(odd_lot.defaulter, "ODL")
