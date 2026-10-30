"""PSX market microstructure, in code rather than in a prompt.

Two rules shape every decision on this exchange, and both are deterministic:

**Scrip-level circuit breakers.**  A scrip may not move more than the band in
force from its previous close — "5% or Rs.1, whichever is higher" under the
desk's rulebook, widened by the Exchange under Regulation 19.3 (historically to
7.5%).  The calculated price is rounded to the nearest tick.  A scrip that
closes *on* its cap is locked: the close is real, the execution is not.

**Trading board.**  Anything outside the Ready (``REG``) board — odd lots,
futures, CSF, the Defaulters / Non-Compliant segment — is not the instrument the
desk is mandated to trade.

Everything here is a pure function of numbers that came from PSX, so the
technical analyst's "circuit" block and the CRO's execution veto cannot drift
apart.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from psx_desk.schemas import CircuitState, LockState

DEFAULT_TICK_PKR = 0.01
DEFAULT_BAND_PCT = 5.0
DEFAULT_BAND_FLOOR_PKR = 1.0


def round_to_tick(value: float, tick: float = DEFAULT_TICK_PKR) -> float:
    """Round a price to the exchange tick (PSX rounds caps, not truncates)."""

    if tick <= 0:
        return float(value)
    ticks = Decimal(str(value)) / Decimal(str(tick))
    return float(ticks.quantize(Decimal("1"), rounding=ROUND_HALF_UP) * Decimal(str(tick)))


def circuit_caps(
    prev_close: float,
    *,
    band_pct: float = DEFAULT_BAND_PCT,
    band_floor_pkr: float = DEFAULT_BAND_FLOOR_PKR,
    tick: float = DEFAULT_TICK_PKR,
) -> tuple[float, float]:
    """Upper and lower circuit prices for a session.

    The band is the greater of ``band_pct`` of the previous close and
    ``band_floor_pkr`` ("or Rs.1, whichever is higher"), applied to the previous
    close and rounded to the tick.  The lower cap never goes below one tick.
    """

    if prev_close <= 0:
        raise ValueError(f"prev_close must be positive, got {prev_close}")
    band = max(prev_close * band_pct / 100.0, band_floor_pkr)
    upper = round_to_tick(prev_close + band, tick)
    lower = round_to_tick(max(tick, prev_close - band), tick)
    return upper, lower


def detect_lock(
    close: float,
    upper_cap: float,
    lower_cap: float,
    *,
    tick: float = DEFAULT_TICK_PKR,
) -> LockState:
    """Is the close sitting on (or through) a cap?

    A tolerance of half a tick absorbs the exchange's own rounding, so a scrip
    printed at its cap is never reported as "0.004% away from the cap".
    """

    tolerance = tick / 2.0
    if close >= upper_cap - tolerance:
        return LockState.UPPER
    if close <= lower_cap + tolerance:
        return LockState.LOWER
    return LockState.NONE


def _is_locked(
    bars: list[dict], index: int, *, band_pct: float, band_floor_pkr: float, tick: float
) -> LockState:
    """Lock test for ``bars[index]`` against its own previous close."""

    if index < 1 or index >= len(bars):
        return LockState.NONE
    close = bars[index].get("close")
    prev = bars[index - 1].get("close")
    if not close or not prev:
        return LockState.NONE
    upper, lower = circuit_caps(prev, band_pct=band_pct, band_floor_pkr=band_floor_pkr, tick=tick)
    upper_file = bars[index].get("upper_cap")
    lower_file = bars[index].get("lower_cap")
    if upper_file and lower_file:
        upper, lower = float(upper_file), float(lower_file)
    return detect_lock(float(close), upper, lower, tick=tick)


def consecutive_locks(
    bars: list[dict],
    *,
    band_pct: float = DEFAULT_BAND_PCT,
    band_floor_pkr: float = DEFAULT_BAND_FLOOR_PKR,
    tick: float = DEFAULT_TICK_PKR,
) -> tuple[LockState, int]:
    """Trailing run of sessions locked in the same direction.

    PSX tightens scrutiny and withdraws short-sale exemptions after repeated
    lower locks, so the desk tracks the streak, not just the last session.
    """

    streak = 0
    direction = LockState.NONE
    for index in range(len(bars) - 1, 0, -1):
        state = _is_locked(bars, index, band_pct=band_pct, band_floor_pkr=band_floor_pkr, tick=tick)
        if state is LockState.NONE:
            break
        if direction is LockState.NONE:
            direction = state
        elif state is not direction:
            break
        streak += 1
    return (direction if streak else LockState.NONE), streak


def compute_circuit_state(
    *,
    close: float,
    prev_close: float | None,
    history: list[dict] | None = None,
    exchange_upper: float | None = None,
    exchange_lower: float | None = None,
    band_pct: float = DEFAULT_BAND_PCT,
    band_floor_pkr: float = DEFAULT_BAND_FLOOR_PKR,
    tick: float = DEFAULT_TICK_PKR,
) -> CircuitState:
    """Build the full :class:`CircuitState` for a session.

    ``exchange_upper`` / ``exchange_lower`` — when the PSX ``symbol_price`` file
    for the date was available — always win over a computed band, because the
    Exchange publishes the authoritative caps.
    """

    bars = history or []
    if prev_close is None and len(bars) >= 2:
        prev_close = bars[-2].get("close")
    if prev_close is None and bars:
        # Last resort: the bar before the one we are evaluating.
        closes = [b.get("close") for b in bars if b.get("close")]
        prev_close = closes[-2] if len(closes) >= 2 else closes[-1]
    if prev_close is None or prev_close <= 0:
        # Cannot establish a band without a previous close: report the
        # constraint as unknown instead of inventing one.
        upper = exchange_upper or round_to_tick(close, tick)
        lower = exchange_lower or round_to_tick(close, tick)
        return CircuitState(
            band_pct=band_pct,
            band_floor_pkr=band_floor_pkr,
            upper_cap_pkr=upper,
            lower_cap_pkr=lower,
            close_pkr=close,
            locked=LockState.NONE,
            cap_source="exchange_file" if exchange_upper else "computed",
            distance_to_upper_pct=0.0,
            distance_to_lower_pct=0.0,
            tradable_at_close=True,
            execution_note="Previous close unavailable from PSX; circuit band not established.",
        )

    computed_upper, computed_lower = circuit_caps(
        prev_close, band_pct=band_pct, band_floor_pkr=band_floor_pkr, tick=tick
    )
    cap_source = "computed"
    upper, lower = computed_upper, computed_lower
    if exchange_upper and exchange_lower:
        upper, lower = float(exchange_upper), float(exchange_lower)
        cap_source = "exchange_file"

    locked = detect_lock(close, upper, lower, tick=tick)

    streak_state, streak = consecutive_locks(
        [*bars, {"close": close}], band_pct=band_pct, band_floor_pkr=band_floor_pkr, tick=tick
    )
    if not bars:
        streak = 1 if locked is not LockState.NONE else 0

    distance_upper = max(0.0, round(100.0 * (upper - close) / close, 3))
    distance_lower = max(0.0, round(100.0 * (close - lower) / close, 3))

    note = _execution_note(locked, streak, band_pct)

    return CircuitState(
        band_pct=band_pct,
        band_floor_pkr=band_floor_pkr,
        upper_cap_pkr=upper,
        lower_cap_pkr=lower,
        close_pkr=close,
        locked=locked,
        cap_source=cap_source,
        distance_to_upper_pct=distance_upper,
        distance_to_lower_pct=distance_lower,
        consecutive_locked_sessions=streak,
        tradable_at_close=locked is LockState.NONE,
        execution_note=note,
    )


def _execution_note(locked: LockState, streak: int, band_pct: float) -> str:
    if locked is LockState.UPPER:
        base = (
            f"Locked at the upper {band_pct:g}% circuit — buyers are queued with no sellers. "
            "Nothing can be bought at the close; work the next opening auction."
        )
        if streak > 1:
            base += f" {streak} consecutive upper locks: expect an opening gap, size accordingly."
        return base
    if locked is LockState.LOWER:
        base = (
            f"Locked at the lower {band_pct:g}% circuit — sellers are queued with no bids. "
            "An exit is not available today; pre-open the next session."
        )
        if streak > 1:
            base += (
                f" {streak} consecutive lower locks: PSX tightens scrutiny and withdraws "
                "short-sale exemptions on this pattern."
            )
        return base
    return "Tape open; the closing print was executable."


def board_is_tradeable(board: str | None) -> bool:
    """Only the Ready board is in the desk's mandate."""

    return (board or "REG").strip().upper() == "REG"


def is_untradeable_segment(segment: str | None) -> bool:
    """Defaulters / non-compliant / suspended / delisted are all vetoes."""

    return (segment or "regular").strip().lower() in {
        "defaulters",
        "non_compliant",
        "non-compliant",
        "suspended",
        "delisted",
    }


def sessions_between(start: date, end: date) -> int:
    """Rough weekday count between two dates (exchange holidays not modelled)."""

    if end <= start:
        return 0
    return sum(1 for i in range((end - start).days + 1) if (start.toordinal() + i) % 7 not in {5, 6})
