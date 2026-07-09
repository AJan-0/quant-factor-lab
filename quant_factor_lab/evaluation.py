from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .factors import merge_factor_target
from .types import KEY_COLUMNS


def evaluate_factor_panel(
    data: pd.DataFrame,
    factor_panel: pd.DataFrame,
    horizons: list[int] | tuple[int, ...],
    min_observations: int = 60,
    min_cross_section_assets: int = 3,
) -> pd.DataFrame:
    factor_columns = [column for column in factor_panel.columns if column not in KEY_COLUMNS]
    if not factor_columns:
        raise ValueError("factor_panel contains no factor columns")

    rows: list[dict] = []
    for horizon in horizons:
        merged = merge_factor_target(data, factor_panel, int(horizon))
        target_col = f"fwd_return_{int(horizon)}"
        for factor_name in factor_columns:
            valid = merged[list(KEY_COLUMNS) + [factor_name, target_col]].dropna()
            if len(valid) < min_observations:
                continue
            pooled_pearson = _safe_corr(valid[factor_name], valid[target_col], method="pearson")
            pooled_spearman = _safe_corr(valid[factor_name], valid[target_col], method="spearman")
            cs_ics = _cross_sectional_ics(valid, factor_name, target_col, min_cross_section_assets)
            ts_ics = _time_series_ics(valid, factor_name, target_col, min_observations=max(20, min_observations // 4))
            quantile_spread = _quantile_spread(valid, factor_name, target_col)
            mean_ic = _nanmean(cs_ics) if cs_ics else pooled_spearman
            ic_std = _nanstd(cs_ics) if len(cs_ics) > 1 else np.nan
            ic_ir = mean_ic / ic_std if ic_std and not math.isnan(ic_std) and ic_std > 0 else np.nan
            direction = 1 if (pooled_spearman or 0.0) >= 0 else -1
            score = abs(pooled_spearman or 0.0) * math.sqrt(len(valid))
            rows.append(
                {
                    "factor": factor_name,
                    "horizon": int(horizon),
                    "observations": int(len(valid)),
                    "pooled_pearson": pooled_pearson,
                    "pooled_spearman": pooled_spearman,
                    "mean_cross_section_ic": mean_ic,
                    "ic_ir": ic_ir,
                    "positive_cross_section_ic_ratio": _positive_ratio(cs_ics),
                    "mean_time_series_ic": _nanmean(ts_ics) if ts_ics else np.nan,
                    "quantile_spread": quantile_spread,
                    "direction": direction,
                    "score": score,
                }
            )
    if not rows:
        return pd.DataFrame(
            columns=[
                "factor",
                "horizon",
                "observations",
                "pooled_pearson",
                "pooled_spearman",
                "mean_cross_section_ic",
                "ic_ir",
                "positive_cross_section_ic_ratio",
                "mean_time_series_ic",
                "quantile_spread",
                "direction",
                "score",
            ]
        )
    return pd.DataFrame(rows).sort_values(["horizon", "score"], ascending=[True, False]).reset_index(drop=True)


def select_top_factor(evaluation: pd.DataFrame, horizon: int) -> dict:
    subset = evaluation[evaluation["horizon"] == int(horizon)].sort_values("score", ascending=False)
    if subset.empty:
        raise ValueError(f"No evaluated factors available for horizon={horizon}")
    return subset.iloc[0].to_dict()


def _safe_corr(left: pd.Series, right: pd.Series, method: str) -> float:
    frame = pd.DataFrame({"left": left, "right": right}).dropna()
    if len(frame) < 3 or frame["left"].nunique() < 2 or frame["right"].nunique() < 2:
        return np.nan
    value = frame["left"].corr(frame["right"], method=method)
    return float(value) if not pd.isna(value) else np.nan


def _cross_sectional_ics(valid: pd.DataFrame, factor_name: str, target_col: str, min_assets: int) -> list[float]:
    values: list[float] = []
    for _, group in valid.groupby("timestamp", sort=True):
        if len(group) < min_assets:
            continue
        corr = _safe_corr(group[factor_name], group[target_col], method="spearman")
        if not pd.isna(corr):
            values.append(float(corr))
    return values


def _time_series_ics(valid: pd.DataFrame, factor_name: str, target_col: str, min_observations: int) -> list[float]:
    values: list[float] = []
    for _, group in valid.groupby("symbol", sort=True):
        if len(group) < min_observations:
            continue
        corr = _safe_corr(group[factor_name], group[target_col], method="spearman")
        if not pd.isna(corr):
            values.append(float(corr))
    return values


def _quantile_spread(valid: pd.DataFrame, factor_name: str, target_col: str) -> float:
    unique_values = valid[factor_name].nunique()
    bucket_count = min(5, unique_values)
    if bucket_count < 2:
        return np.nan
    ranked = valid.copy()
    ranked["bucket"] = pd.qcut(
        ranked[factor_name].rank(method="first"),
        q=bucket_count,
        labels=False,
        duplicates="drop",
    )
    grouped = ranked.groupby("bucket")[target_col].mean()
    if len(grouped) < 2:
        return np.nan
    return float(grouped.iloc[-1] - grouped.iloc[0])


def _nanmean(values: list[float]) -> float:
    return float(np.nanmean(values)) if values else np.nan


def _nanstd(values: list[float]) -> float:
    return float(np.nanstd(values, ddof=1)) if len(values) > 1 else np.nan


def _positive_ratio(values: list[float]) -> float:
    cleaned = [value for value in values if not pd.isna(value)]
    if not cleaned:
        return np.nan
    return float(sum(value > 0 for value in cleaned) / len(cleaned))
