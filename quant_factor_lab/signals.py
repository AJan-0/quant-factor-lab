from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .types import KEY_COLUMNS


SIGNAL_COLUMNS = [
    "timestamp",
    "symbol",
    "factor",
    "theme",
    "value",
    "percentile",
    "signal",
    "signal_label",
    "setup",
    "direction",
    "direction_label",
    "confidence",
    "confidence_label",
    "signal_score",
    "sharpe",
    "score",
    "total_return",
    "max_drawdown",
    "win_rate",
    "horizon",
    "interpretation",
]


def build_factor_signal_radar(
    factor_panel: pd.DataFrame,
    factor_backtests: pd.DataFrame,
    *,
    min_history: int = 20,
    limit_per_symbol: int = 8,
) -> pd.DataFrame:
    """Translate factor research outputs into current, trader-readable signals."""
    factor_columns = [column for column in factor_panel.columns if column not in KEY_COLUMNS]
    if factor_panel.empty or not factor_columns:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)

    latest_metrics = _latest_factor_metrics(factor_backtests)
    available_factors = [factor for factor in latest_metrics if factor in factor_columns]
    if not available_factors:
        available_factors = factor_columns

    rows: list[dict[str, Any]] = []
    normalized_panel = factor_panel.sort_values(list(KEY_COLUMNS)).copy()
    normalized_panel["timestamp"] = pd.to_datetime(normalized_panel["timestamp"])

    for symbol, symbol_frame in normalized_panel.groupby("symbol", sort=True):
        symbol_frame = symbol_frame.sort_values("timestamp")
        for factor_name in available_factors:
            if factor_name not in symbol_frame:
                continue
            series = pd.to_numeric(symbol_frame[factor_name], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if len(series) < min_history:
                continue
            latest_value = float(series.iloc[-1])
            percentile = float(series.rank(pct=True).iloc[-1])
            metric = latest_metrics.get(factor_name, {})
            direction = _safe_int(metric.get("direction"), default=1)
            signal, signal_label, setup = _classify_signal(percentile, direction)
            confidence = _confidence(metric)
            signal_score = _signal_score(percentile, confidence, signal)
            rows.append(
                {
                    "timestamp": symbol_frame.loc[series.index[-1], "timestamp"],
                    "symbol": str(symbol),
                    "factor": factor_name,
                    "theme": _factor_theme(factor_name),
                    "value": latest_value,
                    "percentile": percentile,
                    "signal": signal,
                    "signal_label": signal_label,
                    "setup": setup,
                    "direction": direction,
                    "direction_label": "high_value_bullish" if direction >= 0 else "low_value_bullish",
                    "confidence": confidence,
                    "confidence_label": _confidence_label(confidence),
                    "signal_score": signal_score,
                    "sharpe": _safe_float(metric.get("sharpe")),
                    "score": _safe_float(metric.get("score")),
                    "total_return": _safe_float(metric.get("total_return")),
                    "max_drawdown": _safe_float(metric.get("max_drawdown")),
                    "win_rate": _safe_float(metric.get("win_rate")),
                    "horizon": _safe_int(metric.get("horizon"), default=None),
                    "interpretation": _interpretation(signal, percentile, direction, confidence),
                }
            )

    if not rows:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)

    result = pd.DataFrame(rows)
    result["timestamp"] = result["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    result = result.sort_values(
        ["symbol", "signal_score", "confidence", "sharpe"],
        ascending=[True, False, False, False],
        na_position="last",
    )
    if limit_per_symbol > 0:
        result = result.groupby("symbol", sort=False).head(int(limit_per_symbol))
    return result[SIGNAL_COLUMNS].reset_index(drop=True)


def _latest_factor_metrics(factor_backtests: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if factor_backtests.empty or "factor" not in factor_backtests.columns:
        return {}
    frame = factor_backtests.copy()
    if "error" in frame.columns:
        frame = frame[frame["error"].isna() | (frame["error"].astype(str) == "")]
    if frame.empty:
        return {}
    sort_columns = [column for column in ["sharpe", "score"] if column in frame.columns]
    if sort_columns:
        frame = frame.sort_values(sort_columns, ascending=False, na_position="last")
    return {str(row["factor"]): row.to_dict() for _, row in frame.groupby("factor", sort=False).head(1).iterrows()}


def _classify_signal(percentile: float, direction: int) -> tuple[str, str, str]:
    if direction >= 0:
        if percentile >= 0.8:
            return "BULLISH", "偏多", "高分位顺势"
        if percentile <= 0.2:
            return "BEARISH", "偏空", "低分位转弱"
    else:
        if percentile <= 0.2:
            return "BULLISH", "偏多", "低分位反转"
        if percentile >= 0.8:
            return "BEARISH", "偏空", "高分位过热"
    return "NEUTRAL", "中性", "分位中枢"


def _confidence(metric: dict[str, Any]) -> float:
    sharpe = max(_safe_float(metric.get("sharpe")) or 0.0, 0.0)
    score = abs(_safe_float(metric.get("score")) or 0.0)
    win_rate = _safe_float(metric.get("win_rate"))
    drawdown = abs(_safe_float(metric.get("max_drawdown")) or 0.0)

    sharpe_component = min(sharpe / 3.0, 1.0)
    score_component = min(score / 5.0, 1.0)
    win_component = 0.5 if win_rate is None else min(max((win_rate - 0.45) / 0.25, 0.0), 1.0)
    drawdown_component = max(0.0, 1.0 - min(drawdown / 0.35, 1.0))
    confidence = 0.45 * sharpe_component + 0.25 * score_component + 0.2 * win_component + 0.1 * drawdown_component
    return float(min(max(confidence, 0.0), 1.0))


def _signal_score(percentile: float, confidence: float, signal: str) -> float:
    extremity = abs(percentile - 0.5) * 2.0
    neutral_penalty = 0.45 if signal == "NEUTRAL" else 1.0
    return float(extremity * confidence * neutral_penalty)


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.7:
        return "高"
    if confidence >= 0.4:
        return "中"
    return "低"


def _factor_theme(factor_name: str) -> str:
    if factor_name.startswith(("mom_", "ma_gap_", "range_position_")):
        return "趋势"
    if factor_name.startswith("reversal_"):
        return "反转"
    if factor_name.startswith("volume_"):
        return "量能"
    if factor_name.startswith("amihud_"):
        return "流动性"
    if factor_name.startswith("volatility_"):
        return "波动"
    if factor_name.startswith("ml_"):
        return "机器学习"
    return "综合"


def _interpretation(signal: str, percentile: float, direction: int, confidence: float) -> str:
    side = {"BULLISH": "偏多", "BEARISH": "偏空", "NEUTRAL": "中性"}[signal]
    direction_text = "高值对未来收益更友好" if direction >= 0 else "低值对未来收益更友好"
    confidence_text = _confidence_label(confidence)
    return f"{side}；当前位于历史{percentile:.0%}分位，{direction_text}，回测可信度{confidence_text}。"


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _safe_int(value: Any, default: int | None) -> int | None:
    try:
        if value is None or pd.isna(value):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default
