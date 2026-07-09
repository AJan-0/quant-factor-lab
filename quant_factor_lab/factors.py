from __future__ import annotations

import numpy as np
import pandas as pd

from .data import normalize_market_frame
from .types import KEY_COLUMNS


def compute_forward_returns(data: pd.DataFrame, horizon: int) -> pd.DataFrame:
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    market = normalize_market_frame(data)
    column = f"fwd_return_{horizon}"
    result = market[list(KEY_COLUMNS)].copy()
    result[column] = market.groupby("symbol")["close"].transform(
        lambda close: close.pct_change(horizon).shift(-horizon)
    )
    return result


def build_operator_factor_panel(data: pd.DataFrame, windows: list[int] | tuple[int, ...]) -> pd.DataFrame:
    market = normalize_market_frame(data)
    if not windows:
        raise ValueError("windows must contain at least one lookback")

    panel = market[list(KEY_COLUMNS)].copy()
    grouped = market.groupby("symbol", sort=False)
    returns_1 = grouped["close"].pct_change()
    panel["return_1"] = returns_1

    for raw_window in windows:
        window = int(raw_window)
        if window <= 1:
            continue
        min_periods = max(2, min(window, max(3, window // 2)))
        rolling_close_mean = grouped["close"].transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).mean()
        )
        rolling_volume_mean = grouped["volume"].transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).mean()
        )
        rolling_volume_std = grouped["volume"].transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).std(ddof=0)
        )
        rolling_high = grouped["high"].transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).max()
        )
        rolling_low = grouped["low"].transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).min()
        )
        rolling_return_std = returns_1.groupby(market["symbol"], sort=False).transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).std(ddof=0)
        )
        amihud = (returns_1.abs() / market["volume"].replace(0, np.nan)).groupby(market["symbol"], sort=False).transform(
            lambda values, w=window, mp=min_periods: values.rolling(w, min_periods=mp).mean()
        )

        panel[f"mom_{window}"] = grouped["close"].pct_change(window)
        panel[f"reversal_{window}"] = -panel[f"mom_{window}"]
        panel[f"volatility_{window}"] = rolling_return_std
        panel[f"ma_gap_{window}"] = market["close"] / rolling_close_mean - 1.0
        panel[f"volume_zscore_{window}"] = (market["volume"] - rolling_volume_mean) / rolling_volume_std.replace(0, np.nan)
        panel[f"range_position_{window}"] = (market["close"] - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan)
        panel[f"amihud_{window}"] = amihud

    factor_columns = [column for column in panel.columns if column not in KEY_COLUMNS]
    panel[factor_columns] = panel[factor_columns].replace([np.inf, -np.inf], np.nan)
    return panel.sort_values(list(KEY_COLUMNS)).reset_index(drop=True)


def merge_factor_target(data: pd.DataFrame, factor_panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    target = compute_forward_returns(data, horizon)
    return factor_panel.merge(target, on=list(KEY_COLUMNS), how="inner")
