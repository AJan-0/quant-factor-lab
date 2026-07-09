from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .types import REQUIRED_MARKET_COLUMNS


QUALITY_COLUMNS = [
    "symbol",
    "rows",
    "start",
    "end",
    "duplicate_timestamps",
    "missing_values",
    "invalid_prices",
    "zero_or_negative_volume",
    "extreme_returns",
    "large_time_gaps",
    "estimated_missing_bars",
    "quality_score",
    "status",
]


def build_market_data_quality_report(data: pd.DataFrame) -> dict[str, Any]:
    market = _coerce_quality_frame(data)
    symbol_rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for symbol, group in market.groupby("symbol", sort=True):
        group = group.sort_values("timestamp").copy()
        row = _symbol_quality_row(str(symbol), group)
        symbol_rows.append(row)
        issues.extend(_issue_rows(row))

    total_rows = int(sum(row["rows"] for row in symbol_rows))
    total_issues = int(
        sum(
            row["duplicate_timestamps"]
            + row["missing_values"]
            + row["invalid_prices"]
            + row["zero_or_negative_volume"]
            + row["extreme_returns"]
            + row["estimated_missing_bars"]
            for row in symbol_rows
        )
    )
    worst = min(symbol_rows, key=lambda row: row["quality_score"], default=None)
    summary = {
        "symbol_count": int(len(symbol_rows)),
        "row_count": total_rows,
        "issue_count": total_issues,
        "worst_symbol": worst["symbol"] if worst else None,
        "worst_score": worst["quality_score"] if worst else None,
        "status": _overall_status(symbol_rows),
    }
    return {"summary": summary, "symbols": symbol_rows, "issues": issues}


def quality_symbols_frame(report: dict[str, Any]) -> pd.DataFrame:
    rows = report.get("symbols", [])
    return pd.DataFrame(rows, columns=QUALITY_COLUMNS)


def _coerce_quality_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_MARKET_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Market data missing required columns: {missing}")
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=False, errors="coerce")
    data["symbol"] = data["symbol"].astype(str)
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if "asset_class" not in data.columns:
        data["asset_class"] = None
    if "frequency" not in data.columns:
        data["frequency"] = None
    return data.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def _symbol_quality_row(symbol: str, group: pd.DataFrame) -> dict[str, Any]:
    timestamps = pd.to_datetime(group["timestamp"])
    duplicate_timestamps = int(timestamps.duplicated().sum())
    price_columns = ["open", "high", "low", "close"]
    value_columns = price_columns + ["volume"]
    numeric = group[value_columns].apply(pd.to_numeric, errors="coerce")
    missing_values = int(numeric.isna().any(axis=1).sum())
    invalid_prices = int(
        (
            (numeric[price_columns] <= 0).any(axis=1)
            | (numeric["high"] < numeric[["open", "close"]].max(axis=1))
            | (numeric["low"] > numeric[["open", "close"]].min(axis=1))
            | (numeric["low"] > numeric["high"])
        ).sum()
    )
    zero_or_negative_volume = int((numeric["volume"] <= 0).sum())
    returns = numeric["close"].pct_change().replace([np.inf, -np.inf], np.nan)
    threshold = _extreme_return_threshold(group)
    extreme_returns = int((returns.abs() > threshold).sum())
    large_time_gaps, estimated_missing_bars = _time_gap_stats(group)
    issue_weight = (
        duplicate_timestamps * 1.0
        + missing_values * 1.0
        + invalid_prices * 1.5
        + zero_or_negative_volume * 0.6
        + extreme_returns * 0.8
        + estimated_missing_bars * 0.5
    )
    denominator = max(float(len(group)), 1.0)
    quality_score = float(max(0.0, 1.0 - issue_weight / denominator))
    return {
        "symbol": symbol,
        "rows": int(len(group)),
        "start": str(timestamps.min()),
        "end": str(timestamps.max()),
        "duplicate_timestamps": duplicate_timestamps,
        "missing_values": missing_values,
        "invalid_prices": invalid_prices,
        "zero_or_negative_volume": zero_or_negative_volume,
        "extreme_returns": extreme_returns,
        "large_time_gaps": large_time_gaps,
        "estimated_missing_bars": estimated_missing_bars,
        "quality_score": quality_score,
        "status": _status_for_score(quality_score),
    }


def _issue_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "duplicate_timestamps": ("重复时间戳", "high"),
        "missing_values": ("缺失OHLCV", "high"),
        "invalid_prices": ("价格结构异常", "high"),
        "zero_or_negative_volume": ("成交量非正", "medium"),
        "extreme_returns": ("极端收益", "medium"),
        "estimated_missing_bars": ("疑似缺失K线", "medium"),
    }
    issues: list[dict[str, Any]] = []
    for key, (label, severity) in labels.items():
        count = int(row.get(key, 0) or 0)
        if count <= 0:
            continue
        issues.append({"symbol": row["symbol"], "type": key, "label": label, "severity": severity, "count": count})
    return issues


def _time_gap_stats(group: pd.DataFrame) -> tuple[int, int]:
    unique_times = pd.Series(pd.to_datetime(group["timestamp"]).drop_duplicates().sort_values().to_list())
    if len(unique_times) < 2:
        return 0, 0
    diffs = unique_times.diff().dropna()
    if diffs.empty:
        return 0, 0
    frequency = _first_value(group, "frequency").lower()
    expected_gap = {
        "1d": pd.Timedelta(days=1),
        "1h": pd.Timedelta(hours=1),
        "5m": pd.Timedelta(minutes=5),
        "1m": pd.Timedelta(minutes=1),
    }.get(frequency, diffs.median())
    if expected_gap <= pd.Timedelta(0):
        return 0, 0
    asset_class = _first_value(group, "asset_class").lower()
    allowed_multiplier = 3.2 if asset_class == "equity" and frequency == "1d" else 1.8
    large_gaps = diffs[diffs > expected_gap * allowed_multiplier]
    estimated_missing = 0
    for gap in large_gaps:
        estimated_missing += max(0, int(round(gap / expected_gap)) - 1)
    return int(len(large_gaps)), int(estimated_missing)


def _extreme_return_threshold(group: pd.DataFrame) -> float:
    asset_class = _first_value(group, "asset_class").lower()
    if asset_class == "crypto":
        return 0.6
    if asset_class == "equity":
        return 0.35
    return 0.5


def _first_value(group: pd.DataFrame, column: str) -> str:
    if column not in group:
        return ""
    values = group[column].dropna()
    if values.empty:
        return ""
    return str(values.iloc[0])


def _status_for_score(score: float) -> str:
    if score >= 0.98:
        return "PASS"
    if score >= 0.9:
        return "WARN"
    return "FAIL"


def _overall_status(rows: list[dict[str, Any]]) -> str:
    statuses = {row["status"] for row in rows}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"
