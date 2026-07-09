from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from .factors import build_operator_factor_panel, merge_factor_target
from .types import KEY_COLUMNS


@dataclass(frozen=True)
class FactorCandidate:
    name: str
    source: str
    description: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactorMiningResult:
    factor_panel: pd.DataFrame
    candidates: tuple[FactorCandidate, ...]


class FactorMiner(Protocol):
    def mine(self, data: pd.DataFrame) -> FactorMiningResult:
        """Discover candidate factors from normalized market data."""


class OperatorFactorMiner:
    def __init__(self, windows: list[int] | tuple[int, ...]) -> None:
        self.windows = tuple(int(window) for window in windows)

    def mine(self, data: pd.DataFrame) -> FactorMiningResult:
        panel = build_operator_factor_panel(data, self.windows)
        candidates = tuple(
            FactorCandidate(
                name=column,
                source="operator",
                description=_operator_description(column),
                metadata={"windows": self.windows},
            )
            for column in panel.columns
            if column not in KEY_COLUMNS
        )
        return FactorMiningResult(factor_panel=panel, candidates=candidates)


class MLFeatureMiner:
    """ML-assisted discovery that emits an out-of-sample prediction factor."""

    def __init__(
        self,
        windows: list[int] | tuple[int, ...],
        target_horizon: int = 1,
        max_features: int = 10,
        train_fraction: float = 0.7,
        random_state: int = 7,
        n_estimators: int = 80,
    ) -> None:
        self.windows = tuple(int(window) for window in windows)
        self.target_horizon = int(target_horizon)
        self.max_features = int(max_features)
        self.train_fraction = float(train_fraction)
        self.random_state = int(random_state)
        self.n_estimators = int(n_estimators)

    def mine(self, data: pd.DataFrame) -> FactorMiningResult:
        try:
            from sklearn.ensemble import RandomForestRegressor
        except ImportError as exc:
            raise RuntimeError("Install scikit-learn to use MLFeatureMiner") from exc

        base_panel = build_operator_factor_panel(data, self.windows)
        merged = merge_factor_target(data, base_panel, self.target_horizon)
        target_col = f"fwd_return_{self.target_horizon}"
        feature_cols = [column for column in base_panel.columns if column not in KEY_COLUMNS]
        valid_mask = merged[feature_cols + [target_col]].notna().all(axis=1)
        valid = merged.loc[valid_mask].copy()
        if valid.empty or valid["timestamp"].nunique() < 5:
            return FactorMiningResult(factor_panel=base_panel[list(KEY_COLUMNS)].copy(), candidates=())

        timestamps = np.array(sorted(valid["timestamp"].unique()))
        split_at = int(len(timestamps) * self.train_fraction)
        split_at = min(max(split_at, 1), len(timestamps) - 1)
        cutoff = timestamps[split_at]
        train = valid[valid["timestamp"] < cutoff]
        test = valid[valid["timestamp"] >= cutoff]
        if len(train) < 50 or len(test) < 20:
            return FactorMiningResult(factor_panel=base_panel[list(KEY_COLUMNS)].copy(), candidates=())

        model = RandomForestRegressor(
            n_estimators=self.n_estimators,
            max_depth=5,
            min_samples_leaf=8,
            random_state=self.random_state,
            n_jobs=1,
        )
        model.fit(train[feature_cols], train[target_col])
        prediction_col = f"ml_rf_prediction_h{self.target_horizon}"
        valid[prediction_col] = np.nan
        valid.loc[test.index, prediction_col] = model.predict(test[feature_cols])

        prediction_panel = base_panel[list(KEY_COLUMNS)].merge(
            valid[list(KEY_COLUMNS) + [prediction_col]], on=list(KEY_COLUMNS), how="left"
        )
        importances = sorted(
            zip(feature_cols, model.feature_importances_, strict=True),
            key=lambda item: item[1],
            reverse=True,
        )
        top_features = [
            {"name": name, "importance": float(importance)}
            for name, importance in importances[: self.max_features]
        ]
        validation_ic = _safe_rank_corr(test[target_col], valid.loc[test.index, prediction_col])
        candidates = (
            FactorCandidate(
                name=prediction_col,
                source="ml_random_forest",
                description="Out-of-sample random forest prediction of future returns.",
                score=validation_ic,
                metadata={
                    "target_horizon": self.target_horizon,
                    "train_fraction": self.train_fraction,
                    "top_features": top_features,
                },
            ),
        )
        return FactorMiningResult(factor_panel=prediction_panel, candidates=candidates)


def combine_factor_panels(panels: list[pd.DataFrame]) -> pd.DataFrame:
    if not panels:
        raise ValueError("At least one factor panel is required")
    combined = panels[0][list(KEY_COLUMNS)].drop_duplicates().copy()
    for panel in panels:
        columns = [column for column in panel.columns if column not in KEY_COLUMNS]
        if not columns:
            continue
        combined = combined.merge(panel[list(KEY_COLUMNS) + columns], on=list(KEY_COLUMNS), how="outer")
    return combined.sort_values(list(KEY_COLUMNS)).reset_index(drop=True)


def _operator_description(name: str) -> str:
    if name.startswith("mom_"):
        return "Close-to-close return momentum over the lookback window."
    if name.startswith("reversal_"):
        return "Negative momentum used as a mean-reversion candidate."
    if name.startswith("volatility_"):
        return "Rolling realized volatility of one-period returns."
    if name.startswith("ma_gap_"):
        return "Distance between close and rolling moving average."
    if name.startswith("volume_zscore_"):
        return "Volume surprise relative to its rolling mean and standard deviation."
    if name.startswith("range_position_"):
        return "Close location inside the rolling high-low range."
    if name.startswith("amihud_"):
        return "Rolling absolute return divided by traded volume."
    return "Operator-generated factor candidate."


def _safe_rank_corr(left: pd.Series, right: pd.Series) -> float | None:
    frame = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return None
    value = frame["left"].rank().corr(frame["right"].rank())
    if pd.isna(value):
        return None
    return float(value)
