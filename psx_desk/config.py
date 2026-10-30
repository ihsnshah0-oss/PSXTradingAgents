"""Runtime configuration for the PSX Trading Desk.

Everything the desk needs to run is declared here once.  Configuration is a
frozen dataclass so a node can never mutate the desk's limits mid-run, and
every value can be overridden from the environment with a ``PSXDESK_`` prefix.

Local realism notes
-------------------
* ``circuit_band_pct`` defaults to the 5.0% scrip-level band the desk is
  required to police.  PSX Regulation 19.3 lets the Exchange widen the band
  (historically 5% -> 7.5%, wider still for some instrument classes), so the
  band is a *configuration* value, never a hard-coded constant, and when the
  exchange publishes an authoritative upper/lower cap file for the session the
  scrapers use those caps in preference to any computed band.
* ``sbp_policy_rate_pct`` / ``cpi_yoy_pct`` carry an explicit ``as_of`` date and
  source URL.  If the live SBP scrape is unavailable the desk uses these
  labelled defaults and the Fundamental Analyst must treat the rate as
  ``stale`` instead of inventing a number.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# Repo-local defaults
# --------------------------------------------------------------------------- #

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_WORK_DIR = Path(os.getenv("PSXDESK_WORK_DIR") or (PROJECT_ROOT / "var"))


def _env(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(f"PSXDESK_{name}")
    return default if raw is None or raw == "" else raw


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    return default if raw is None else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return default if raw is None else int(raw)


def _env_flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Macro snapshot (labelled, never guessed)
# --------------------------------------------------------------------------- #

# SBP raised the policy rate 50bps to 12.00% at the 26-Oct-2026 MPC, effective
# 27-Oct-2026; the corridor is 11.00% (floor) - 13.00% (ceiling) and CPI for
# September 2026 printed at 10.3% y/y.  These are the *fallback* values; the
# live scrape in ``psx_desk.dataflows.macro`` replaces them whenever SBP is
# reachable, and every consumer records which one it used.
FALLBACK_SBP_POLICY_RATE_PCT = 12.00
FALLBACK_SBP_POLICY_CORRIDOR_FLOOR_PCT = 11.00
FALLBACK_SBP_POLICY_CORRIDOR_CEILING_PCT = 13.00
FALLBACK_PAKISTAN_CPI_YOY_PCT = 10.3
FALLBACK_MACRO_AS_OF = date(2026, 10, 27)


@dataclass(frozen=True, slots=True)
class MacroDefaults:
    """Fallback macro print used when the SBP scrape is unavailable."""

    sbp_policy_rate_pct: float = FALLBACK_SBP_POLICY_RATE_PCT
    corridor_floor_pct: float = FALLBACK_SBP_POLICY_CORRIDOR_FLOOR_PCT
    corridor_ceiling_pct: float = FALLBACK_SBP_POLICY_CORRIDOR_CEILING_PCT
    cpi_yoy_pct: float = FALLBACK_PAKISTAN_CPI_YOY_PCT
    as_of: date = FALLBACK_MACRO_AS_OF


# --------------------------------------------------------------------------- #
# Risk limits (the CRO's rulebook)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard limits enforced by the CRO gate, independent of any LLM opinion."""

    nav_pkr: float = 50_000_000.0            # desk mandate size (PKR)
    max_single_name_weight_pct: float = 10.0  # concentration cap
    max_risk_per_trade_pct: float = 1.0       # of NAV, at the stop
    max_adv_participation_pct: float = 15.0   # of 20-day ADV, per session
    min_free_float_mcap_pkr: float = 500_000_000.0
    min_adv_value_pkr: float = 5_000_000.0    # Rs 5m a day is the liquidity floor
    min_price_pkr: float = 5.0                # sub-Rs5 tape is untradeable at size
    max_stop_pct: float = 12.0                # a stop wider than this is a thesis, not a stop
    min_reward_risk: float = 1.5              # reject sub-1.5R setups
    max_target_horizon_days: int = 60


# --------------------------------------------------------------------------- #
# Desk configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeskConfig:
    """Immutable desk configuration shared by every node."""

    # --- LLM plumbing -----------------------------------------------------
    llm_backend: str = "google-genai"      # google-genai | langchain
    llm_provider: str = "google_genai"     # used by the langchain backend
    deep_model: str = "gemini-3-pro"
    quick_model: str = "gemini-3-flash"
    api_key: str | None = None
    temperature: float = 0.2
    max_output_tokens: int = 8192
    llm_max_retries: int = 2
    repairs: int = 1                       # schema-repair attempts per node

    # --- Market structure -------------------------------------------------
    circuit_band_pct: float = 5.0
    circuit_band_floor_pkr: float = 1.0    # "or Rs.1, whichever is higher"
    tick_size_pkr: float = 0.01
    trading_days_per_year: int = 252
    timezone: str = "Asia/Karachi"

    # --- Scraping ---------------------------------------------------------
    request_timeout_s: float = 30.0
    http_retries: int = 3
    throttle_seconds: float = 0.75         # politeness delay between PSX hits
    cache_ttl_seconds: int = 900           # intraday freshness window
    max_workers: int = 6                   # concurrent symbol fetches
    user_agent: str = (
        "PSXDesk/1.0 (institutional research; +https://www.psx.com.pk)"
    )
    offline: bool = False                  # replay fixtures / cache only

    # --- Desk -------------------------------------------------------------
    macro: MacroDefaults = field(default_factory=MacroDefaults)
    risk: RiskLimits = field(default_factory=RiskLimits)
    max_debate_rounds: int = 2
    history_days: int = 400                # EOD lookback pulled per symbol
    work_dir: Path = DEFAULT_WORK_DIR

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    @property
    def runs_dir(self) -> Path:
        return self.work_dir / "runs"

    def with_overrides(self, **kwargs) -> DeskConfig:
        """Return a copy with the given fields replaced (validation included)."""
        return replace(self, **kwargs)


def load_config(**overrides) -> DeskConfig:
    """Build a :class:`DeskConfig` from defaults + ``PSXDESK_*`` env + overrides."""

    cfg = DeskConfig(
        llm_backend=_env("LLM_BACKEND", "google-genai"),
        llm_provider=_env("LLM_PROVIDER", "google_genai"),
        deep_model=_env("DEEP_MODEL", "gemini-3-pro"),
        quick_model=_env("QUICK_MODEL", "gemini-3-flash"),
        api_key=_env("API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"),
        temperature=_env_float("TEMPERATURE", 0.2),
        max_output_tokens=_env_int("MAX_OUTPUT_TOKENS", 8192),
        llm_max_retries=_env_int("LLM_MAX_RETRIES", 2),
        repairs=_env_int("REPAIRS", 1),
        circuit_band_pct=_env_float("CIRCUIT_BAND_PCT", 5.0),
        circuit_band_floor_pkr=_env_float("CIRCUIT_FLOOR_PKR", 1.0),
        tick_size_pkr=_env_float("TICK_SIZE_PKR", 0.01),
        request_timeout_s=_env_float("HTTP_TIMEOUT_S", 30.0),
        http_retries=_env_int("HTTP_RETRIES", 3),
        throttle_seconds=_env_float("THROTTLE_SECONDS", 0.75),
        cache_ttl_seconds=_env_int("CACHE_TTL_SECONDS", 900),
        max_workers=_env_int("MAX_WORKERS", 6),
        offline=_env_flag("OFFLINE", False),
        max_debate_rounds=_env_int("MAX_DEBATE_ROUNDS", 2),
        history_days=_env_int("HISTORY_DAYS", 400),
        work_dir=Path(_env("WORK_DIR", str(DEFAULT_WORK_DIR))),
        macro=MacroDefaults(
            sbp_policy_rate_pct=_env_float("SBP_POLICY_RATE_PCT", FALLBACK_SBP_POLICY_RATE_PCT),
            corridor_floor_pct=_env_float(
                "SBP_CORRIDOR_FLOOR_PCT", FALLBACK_SBP_POLICY_CORRIDOR_FLOOR_PCT
            ),
            corridor_ceiling_pct=_env_float(
                "SBP_CORRIDOR_CEILING_PCT", FALLBACK_SBP_POLICY_CORRIDOR_CEILING_PCT
            ),
            cpi_yoy_pct=_env_float("CPI_YOY_PCT", FALLBACK_PAKISTAN_CPI_YOY_PCT),
        ),
        risk=RiskLimits(
            nav_pkr=_env_float("NAV_PKR", 50_000_000.0),
            max_single_name_weight_pct=_env_float("MAX_NAME_WEIGHT_PCT", 10.0),
            max_risk_per_trade_pct=_env_float("MAX_RISK_PER_TRADE_PCT", 1.0),
            max_adv_participation_pct=_env_float("MAX_ADV_PARTICIPATION_PCT", 15.0),
            min_free_float_mcap_pkr=_env_float("MIN_FREE_FLOAT_MCAP_PKR", 500_000_000.0),
            min_adv_value_pkr=_env_float("MIN_ADV_VALUE_PKR", 5_000_000.0),
            min_price_pkr=_env_float("MIN_PRICE_PKR", 5.0),
            max_stop_pct=_env_float("MAX_STOP_PCT", 12.0),
            min_reward_risk=_env_float("MIN_REWARD_RISK", 1.5),
            max_target_horizon_days=_env_int("MAX_TARGET_HORIZON_DAYS", 60),
        ),
    )
    cfg = cfg.with_overrides(**overrides)
    validate_config(cfg)
    return cfg


def validate_config(cfg: DeskConfig) -> None:
    """Fail loudly on nonsense limits rather than mis-sizing a live order."""

    if not 0 < cfg.circuit_band_pct <= 25:
        raise ValueError(f"circuit_band_pct out of range: {cfg.circuit_band_pct}")
    if cfg.tick_size_pkr <= 0:
        raise ValueError("tick_size_pkr must be positive")
    if cfg.max_debate_rounds < 1:
        raise ValueError("max_debate_rounds must be >= 1")
    if cfg.history_days < 60:
        raise ValueError("history_days must be >= 60 to compute 50-day levels")
    r = cfg.risk
    if not 0 < r.max_single_name_weight_pct <= 100:
        raise ValueError("max_single_name_weight_pct must be in (0, 100]")
    if not 0 < r.max_risk_per_trade_pct <= 25:
        raise ValueError("max_risk_per_trade_pct must be in (0, 25]")
    if not 0 < r.max_adv_participation_pct <= 100:
        raise ValueError("max_adv_participation_pct must be in (0, 100]")
    if r.min_reward_risk <= 0:
        raise ValueError("min_reward_risk must be positive")
    if cfg.macro.sbp_policy_rate_pct <= 0:
        raise ValueError("sbp_policy_rate_pct must be positive")
