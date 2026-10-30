"""PSX circuit-breaker arithmetic (Regulation 19.3) and lock detection."""

from __future__ import annotations

import pytest

from psx_desk.market_rules import (
    board_is_tradeable,
    circuit_caps,
    compute_circuit_state,
    consecutive_locks,
    detect_lock,
    is_untradeable_segment,
    round_to_tick,
)
from psx_desk.schemas import LockState


def test_round_to_tick_rounds_half_up():
    assert round_to_tick(412.554) == 412.55
    assert round_to_tick(412.556) == 412.56


def test_seven_and_a_half_percent_band():
    upper, lower = circuit_caps(100.0, band_pct=7.5)
    assert (upper, lower) == (107.5, 92.5)


def test_psx_tick_rounding_example_from_the_exchange_notice():
    # PSX's notice: a +7.5% cap of 11.0187 prints as 11.02, and 9.4235 as 9.42.
    assert round_to_tick(11.0187) == 11.02
    assert round_to_tick(9.4235) == 9.42


def test_five_percent_band_is_the_desk_default():
    upper, lower = circuit_caps(100.0, band_pct=5.0)
    assert (upper, lower) == (105.0, 95.0)


def test_rupee_floor_overrides_a_tiny_percentage_band():
    # On a Rs.10 scrip, 5% is Rs.0.50, so the "or Rs.1, whichever is higher" floor binds.
    upper, lower = circuit_caps(10.0, band_pct=5.0, band_floor_pkr=1.0)
    assert (upper, lower) == (11.0, 9.0)


def test_lock_detection_tolerates_half_a_tick():
    assert detect_lock(105.0, 105.0, 95.0) is LockState.UPPER
    assert detect_lock(104.997, 105.0, 95.0) is LockState.UPPER
    assert detect_lock(95.0, 105.0, 95.0) is LockState.LOWER
    assert detect_lock(100.0, 105.0, 95.0) is LockState.NONE


def test_exchange_caps_win_over_a_computed_band():
    state = compute_circuit_state(
        close=105.0,
        prev_close=100.0,
        exchange_upper=105.0,
        exchange_lower=95.0,
        band_pct=5.0,
    )
    assert state.cap_source == "exchange_file"
    assert state.locked is LockState.UPPER
    assert state.tradable_at_close is False
    assert "opening auction" in state.execution_note


def test_computed_band_is_used_when_psx_publishes_no_caps():
    state = compute_circuit_state(close=104.5, prev_close=100.0, band_pct=5.0)
    assert state.cap_source == "computed"
    assert state.locked is LockState.NONE
    assert state.distance_to_upper_pct == pytest.approx(0.48, abs=0.01)


def test_consecutive_lower_locks_are_counted():
    bars = [
        {"close": 100.0},
        {"close": 95.0},
        {"close": 90.25},
        {"close": 85.74},
    ]
    direction, streak = consecutive_locks(bars, band_pct=5.0)
    assert direction is LockState.LOWER
    assert streak == 3


def test_unknown_previous_close_does_not_invent_a_band():
    state = compute_circuit_state(close=412.55, prev_close=None, history=[])
    assert state.locked is LockState.NONE
    assert "not established" in state.execution_note


def test_only_the_ready_board_is_tradeable():
    assert board_is_tradeable("REG")
    assert not board_is_tradeable("ODL")
    assert not board_is_tradeable("FUT")


def test_untradeable_segments():
    assert is_untradeable_segment("defaulters")
    assert is_untradeable_segment("non_compliant")
    assert not is_untradeable_segment("regular")
