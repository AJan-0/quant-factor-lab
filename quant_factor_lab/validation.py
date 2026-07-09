from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from .backtest import BacktestConfig, run_rank_backtest
from .evaluation import evaluate_factor_panel, select_top_factor
from .types import KEY_COLUMNS


WALK_FORWARD_COLUMNS = [
    "fold",
    "train_start",
    "train_end",
    "test_start",
    "test_end",
    "embargo_bars",
    "selected_factor",
    "direction",
    "train_score",
    "train_observations",
    "test_periods",
    "test_total_return",
    "test_sharpe",
    "test_max_drawdown",
    "test_win_rate",
    "error",
]


@dataclass(frozen=True)
class WalkForwardConfig:
    enabled: bool = False
    n_splits: int = 3
    train_fraction: float = 0.55
    embargo_bars: int = 1
    min_train_observations: int = 60
    min_test_bars: int = 10

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "WalkForwardConfig":
        validation = raw.get("validation", {})
        return cls(
            enabled=bool(validation.get("enabled", False)),
            n_splits=max(1, int(validation.get("n_splits", 3))),
            train_fraction=min(max(float(validation.get("train_fraction", 0.55)), 0.2), 0.85),
            embargo_bars=max(0, int(validation.get("embargo_bars", 1))),
            min_train_observations=max(5, int(validation.get("min_train_observations", 60))),
            min_test_bars=max(2, int(validation.get("min_test_bars", 10))),
        )


def run_walk_forward_validation(
    market_data: pd.DataFrame,
    factor_panel: pd.DataFrame,
    raw_config: dict[str, Any],
    microstructure_snapshot: pd.DataFrame | None = None,
    derivatives_history: pd.DataFrame | None = None,
) -> pd.DataFrame:
    config = WalkForwardConfig.from_config(raw_config)
    if not config.enabled:
        return pd.DataFrame(columns=WALK_FORWARD_COLUMNS)

    timestamps = pd.Index(pd.to_datetime(market_data["timestamp"], errors="coerce").dropna().sort_values().unique())
    if len(timestamps) < config.min_test_bars * 2:
        return _error_frame("not enough timestamps for walk-forward validation")

    folds = _fold_boundaries(timestamps, config)
    rows: list[dict[str, Any]] = []
    mining_config = raw_config.get("mining", {})
    evaluation_config = raw_config.get("evaluation", {})
    backtest_config_raw = raw_config.get("backtest", {})
    horizon = int(backtest_config_raw.get("horizon", mining_config.get("target_horizon", 1)))
    horizons = evaluation_config.get("forward_horizons", [horizon])
    if horizon not in [int(value) for value in horizons]:
        horizons = list(horizons) + [horizon]

    market = market_data.copy()
    market["timestamp"] = pd.to_datetime(market["timestamp"], errors="coerce")
    factors = factor_panel.copy()
    factors["timestamp"] = pd.to_datetime(factors["timestamp"], errors="coerce")

    for fold_index, boundary in enumerate(folds, start=1):
        train_mask = (market["timestamp"] >= boundary["train_start"]) & (market["timestamp"] <= boundary["train_end"])
        test_mask = (market["timestamp"] >= boundary["test_start"]) & (market["timestamp"] <= boundary["test_end"])
        train_market = market[train_mask]
        test_market = market[test_mask]
        train_factors = _filter_factor_panel(factors, train_market)
        test_factors = _filter_factor_panel(factors, test_market)
        base = {
            "fold": fold_index,
            "train_start": str(boundary["train_start"]),
            "train_end": str(boundary["train_end"]),
            "test_start": str(boundary["test_start"]),
            "test_end": str(boundary["test_end"]),
            "embargo_bars": config.embargo_bars,
        }
        try:
            evaluation = evaluate_factor_panel(
                train_market,
                train_factors,
                horizons=horizons,
                min_observations=config.min_train_observations,
                min_cross_section_assets=int(evaluation_config.get("min_cross_section_assets", 3)),
            )
            selected = select_top_factor(evaluation, horizon=horizon)
            backtest_config = BacktestConfig.from_config(backtest_config_raw, direction=int(selected["direction"]))
            result = run_rank_backtest(
                test_market,
                test_factors,
                str(selected["factor"]),
                backtest_config,
                microstructure_snapshot=microstructure_snapshot,
                derivatives_history=derivatives_history,
            )
            rows.append(
                {
                    **base,
                    "selected_factor": selected["factor"],
                    "direction": int(selected["direction"]),
                    "train_score": selected.get("score"),
                    "train_observations": selected.get("observations"),
                    "test_periods": result.metrics.get("periods"),
                    "test_total_return": result.metrics.get("total_return"),
                    "test_sharpe": result.metrics.get("sharpe"),
                    "test_max_drawdown": result.metrics.get("max_drawdown"),
                    "test_win_rate": result.metrics.get("win_rate"),
                    "error": None,
                }
            )
        except Exception as exc:
            rows.append(
                {
                    **base,
                    "selected_factor": None,
                    "direction": None,
                    "train_score": None,
                    "train_observations": None,
                    "test_periods": None,
                    "test_total_return": None,
                    "test_sharpe": None,
                    "test_max_drawdown": None,
                    "test_win_rate": None,
                    "error": str(exc),
                }
            )
    return pd.DataFrame(rows, columns=WALK_FORWARD_COLUMNS)


def _fold_boundaries(timestamps: pd.Index, config: WalkForwardConfig) -> list[dict[str, pd.Timestamp]]:
    count = len(timestamps)
    train_size = max(config.min_test_bars, int(count * config.train_fraction))
    remaining = count - train_size - config.embargo_bars
    if remaining < config.min_test_bars:
        train_size = max(config.min_test_bars, count - config.min_test_bars - config.embargo_bars)
        remaining = count - train_size - config.embargo_bars
    test_size = max(config.min_test_bars, remaining // config.n_splits)
    folds = []
    for fold in range(config.n_splits):
        train_end_index = train_size + fold * test_size - 1
        test_start_index = train_end_index + config.embargo_bars + 1
        test_end_index = min(test_start_index + test_size - 1, count - 1)
        if test_start_index >= count or test_end_index - test_start_index + 1 < config.min_test_bars:
            break
        folds.append(
            {
                "train_start": pd.Timestamp(timestamps[0]),
                "train_end": pd.Timestamp(timestamps[train_end_index]),
                "test_start": pd.Timestamp(timestamps[test_start_index]),
                "test_end": pd.Timestamp(timestamps[test_end_index]),
            }
        )
    return folds


def _filter_factor_panel(factors: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    keys = market[list(KEY_COLUMNS)].drop_duplicates()
    return factors.merge(keys, on=list(KEY_COLUMNS), how="inner")


def _error_frame(message: str) -> pd.DataFrame:
    return pd.DataFrame([{**{column: None for column in WALK_FORWARD_COLUMNS}, "error": message}], columns=WALK_FORWARD_COLUMNS)
