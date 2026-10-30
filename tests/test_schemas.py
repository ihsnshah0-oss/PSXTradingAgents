"""Schema invariants: the desk's JSON contract must hold even if the model misbehaves."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from psx_desk.schemas import (
    Action,
    AllocationStatement,
    CircuitState,
    Conviction,
    DataQuality,
    Decision,
    DefaulterSegment,
    FreeFloatCheck,
    LockState,
    MacroBackdrop,
    coerce_pct,
    coerce_price,
)

DAY = date(2026, 10, 29)


def circuit(**kwargs) -> CircuitState:
    base = {
        "band_pct": 5.0,
        "upper_cap_pkr": 105.0,
        "lower_cap_pkr": 95.0,
        "close_pkr": 100.0,
        "locked": LockState.NONE,
        "distance_to_upper_pct": 5.0,
        "distance_to_lower_pct": 5.0,
    }
    base.update(kwargs)
    return CircuitState(**base)


def allocation(**kwargs) -> AllocationStatement:
    base = {
        "symbol": "SYS",
        "as_of": DAY,
        "decision": Decision.APPROVE,
        "action": Action.BUY,
        "conviction": Conviction.MEDIUM,
        "stronger_case": "bull",
        "nav_pkr": 50_000_000.0,
        "allocation_pkr": 5_000_000.0,
        "weight_of_nav_pct": 10.0,
        "entry_limit_pkr": 400.0,
        "stop_pkr": 360.0,
        "target_pkr": 480.0,
        "gates": [],
        "free_float": FreeFloatCheck(passes=True),
        "defaulter": DefaulterSegment(symbol="SYS", checked=True),
        "circuit": circuit(),
        "rationale": "test",
    }
    base.update(kwargs)
    return AllocationStatement(**base)


def test_reject_cannot_carry_size():
    statement = allocation(decision=Decision.REJECT, action=Action.BUY, allocation_pkr=4_000_000)
    assert statement.allocation_pkr == 0
    assert statement.shares == 0
    assert statement.weight_of_nav_pct == 0
    assert statement.action is Action.NO_TRADE
    assert "REJECT forced to zero size" in statement.size_cap_reason


def test_defaulter_segment_is_an_absolute_veto():
    statement = allocation(
        defaulter=DefaulterSegment(
            symbol="SYS", checked=True, is_defaulting=True, segment="non_compliant"
        )
    )
    assert statement.decision is Decision.REJECT
    assert statement.action is Action.NO_TRADE
    assert statement.allocation_pkr == 0


def test_weight_and_shares_are_derived_not_trusted():
    statement = allocation(allocation_pkr=2_000_000.0, entry_limit_pkr=400.0, weight_of_nav_pct=0.1)
    assert statement.weight_of_nav_pct == pytest.approx(4.0)
    assert statement.shares == 5_000
    assert statement.risk_per_share_pkr == pytest.approx(40.0)
    assert statement.reward_risk == pytest.approx(2.0)


def test_stop_above_entry_is_dropped_for_a_long():
    statement = allocation(entry_limit_pkr=400.0, stop_pkr=420.0)
    assert statement.stop_pkr is None


def test_circuit_lock_adds_an_execution_condition():
    locked = circuit(locked=LockState.UPPER, close_pkr=105.0)
    statement = allocation(circuit=locked)
    assert any("opening auction" in condition for condition in statement.conditions)


def test_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        allocation(unexpected="field")


def test_allocation_json_is_strict_and_sorted():
    payload = allocation().to_json()
    assert payload.strip().startswith("{")
    assert '"schema_version": "psx-alloc/1.0"' in payload
    assert "NaN" not in payload


def test_forward_referenced_packet_schema_builds():
    schema = AllocationStatement.model_json_schema()
    assert schema["properties"]["decision"]["$ref"].endswith("Decision")
    assert "risk_budget_pkr" in schema["properties"]


def test_macro_derives_the_real_policy_rate():
    macro = MacroBackdrop(sbp_policy_rate_pct=12.0, cpi_yoy_pct=10.3)
    assert macro.real_policy_rate_pct == pytest.approx(1.7)


def test_price_and_percent_coercion():
    assert coerce_price("Rs. 1,234.50") == pytest.approx(1234.5)
    assert coerce_price("n/a") is None
    assert coerce_pct("(3.42)") == pytest.approx(-3.42)
    assert coerce_pct("3.42%") == pytest.approx(3.42)


def test_data_quality_enum_values_are_stable():
    assert {q.value for q in DataQuality} == {"verified", "stale", "partial", "missing"}
