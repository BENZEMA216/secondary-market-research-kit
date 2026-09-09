"""Single implementation contract for the frozen research strategy.

The JSON file is the hand-off artifact.  These constants are the executable
counterpart.  ``load_frozen_config`` fails if either side drifts, preventing a
backtester from silently running rules different from the documented version.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .models import deep_freeze


STRATEGY_VERSION = "scenario-router-0.3.0"

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_NEGATIVE_WAVES = 3

QUAD_STOCH_LINES = ((9, 3), (14, 3), (40, 4), (60, 10))
QUAD_STOCH_OVERSOLD = 20.0
QUAD_STOCH_BOUNCE = 20.0
QUAD_STOCH_MIN_DIVERGENCE = 2.0
QUAD_STOCH_MAX_BARS_FROM_ARM = 12

MINIMUM_PRIOR_CLOSE = 5.0
MINIMUM_MEDIAN_DOLLAR_VOLUME_20 = 20_000_000.0
MAXIMUM_ENTRY_NBBO_SPREAD_BPS = 30.0

MINIMUM_OPENING_GAP = 0.10
MINIMUM_FIRST_20M_VOLUME_MULTIPLE = 1.0
OPENING_RANGE_STARTS = ("09:30", "09:35", "09:40", "09:45")
ORB_SIGNAL_START = "09:50"
ORB_SIGNAL_END = "10:55"

RISK_PER_TRADE = 0.0025
MAXIMUM_POSITIONS = 4
MAXIMUM_TOTAL_OPEN_RISK = 0.01
MAXIMUM_GROSS_EXPOSURE = 1.0
MAXIMUM_SINGLE_NAME_EXPOSURE = 0.25

REVERSAL_TARGET_R = 2.0
REVERSAL_MAX_HOLDING_SESSIONS = 30
EVENT_DAY5_REDUCTION_FRACTION = 0.5
EVENT_EMA_EXIT_START_SESSION = 5
EVENT_MAX_HOLDING_SESSIONS = 60

ARTICLE_CATEGORY_MAPPING_VERSION = "normalized-news-v1-2026-09-03"
ARTICLE_CATEGORY_MAP = {
    "news_feed": {
        "earnings": "earnings",
        "guidance": "guidance",
    },
}


def _at(config: Mapping[str, Any], *path: str) -> Any:
    value: Any = config
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            raise ValueError(f"frozen config is missing {'.'.join(path)}")
        value = value[key]
    return value


def assert_config_matches_implementation(config: Mapping[str, Any]) -> None:
    expected: dict[str, Any] = {
        "schema_version": "1.0",
        "strategy_version": STRATEGY_VERSION,
        "as_of": "2026-09-03",
        "market": "US_EQUITIES_RTH",
        "timezone": "America/New_York",
        "universe": {
            "security_type": "primary_common_stock",
            "minimum_prior_close": MINIMUM_PRIOR_CLOSE,
            "minimum_20d_median_dollar_volume": MINIMUM_MEDIAN_DOLLAR_VOLUME_20,
            "maximum_entry_nbbo_spread_bps": MAXIMUM_ENTRY_NBBO_SPREAD_BPS,
            "excluded": ["ETF", "ADR", "OTC", "preferred", "fund"],
        },
        "reversal": {
            "macd": {
                "fast": MACD_FAST,
                "slow": MACD_SLOW,
                "signal": MACD_SIGNAL,
                "negative_waves": MACD_NEGATIVE_WAVES,
            },
            "valid_sessions_after_signal": 1,
            "quad_stochastic": {
                "bar_minutes": 5,
                "lines": [list(item) for item in QUAD_STOCH_LINES],
                "oversold_strictly_below": QUAD_STOCH_OVERSOLD,
                "bounce_strictly_above": QUAD_STOCH_BOUNCE,
                "minimum_oscillator_divergence_points": QUAD_STOCH_MIN_DIVERGENCE,
                "maximum_bars_from_arm": QUAD_STOCH_MAX_BARS_FROM_ARM,
            },
            "stop": "macd_third_negative_wave_low",
            "target_r": REVERSAL_TARGET_R,
            "maximum_holding_sessions": REVERSAL_MAX_HOLDING_SESSIONS,
        },
        "event": {
            "article_category_mapping": {
                "version": ARTICLE_CATEGORY_MAPPING_VERSION,
                "providers": ARTICLE_CATEGORY_MAP,
                "unknown_or_conflicting": "unclassified",
            },
            "event_window": "previous_regular_close_exclusive_to_market_open_exclusive",
            "minimum_opening_gap": MINIMUM_OPENING_GAP,
            "minimum_first_20m_volume_multiple_of_adv20": MINIMUM_FIRST_20M_VOLUME_MULTIPLE,
            "corporate_action_basis_required": True,
            "opening_range_bar_starts": list(OPENING_RANGE_STARTS),
            "orb_signal_bar_starts": [ORB_SIGNAL_START, ORB_SIGNAL_END],
            "stop": "opening_range_low",
            "day5_close_reduction_fraction": EVENT_DAY5_REDUCTION_FRACTION,
            "remaining_stop_after_day5": "breakeven",
            "trend_exit": "daily_close_below_ema10_then_next_open",
            "maximum_holding_sessions": EVENT_MAX_HOLDING_SESSIONS,
            "primary_event_mode": "strict_primary",
            "event_variants_must_be_reported_separately": ["E2A", "E2B"],
        },
        "portfolio": {
            "initial_stop_risk_per_trade": RISK_PER_TRADE,
            "maximum_positions": MAXIMUM_POSITIONS,
            "maximum_total_initial_stop_risk": MAXIMUM_TOTAL_OPEN_RISK,
            "maximum_gross_exposure": MAXIMUM_GROSS_EXPOSURE,
            "maximum_single_name_exposure": MAXIMUM_SINGLE_NAME_EXPOSURE,
            "conflict_priority": ["event", "reversal"],
            "simultaneous_sort": [
                "branch_priority", "signal_time", "prior_day_median_dollar_volume_desc", "ticker"
            ],
        },
        "execution": {
            "signal_uses_completed_bar": True,
            "entry": "next_bar_open",
            "requires_point_in_time_nbbo": True,
            "same_bar_exit_priority": ["stop", "target", "scheduled_close_action"],
        },
    }
    def same_type_and_value(actual: Any, frozen: Any) -> bool:
        if type(actual) is not type(frozen):
            return False
        if isinstance(frozen, dict):
            return set(actual) == set(frozen) and all(
                same_type_and_value(actual[key], value) for key, value in frozen.items()
            )
        if isinstance(frozen, list):
            return len(actual) == len(frozen) and all(
                same_type_and_value(left, right) for left, right in zip(actual, frozen, strict=True)
            )
        return bool(actual == frozen)

    if not same_type_and_value(dict(config), expected):
        raise ValueError("frozen config drift: the complete JSON contract does not match this implementation")


def load_frozen_config(path: str | Path) -> tuple[Mapping[str, Any], str]:
    payload = Path(path).read_bytes()
    config = json.loads(payload)
    if not isinstance(config, dict):
        raise ValueError("frozen config must be a JSON object")
    assert_config_matches_implementation(config)
    return deep_freeze(config), hashlib.sha256(payload).hexdigest()
