from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from .data import normalize_market_frame


ADJUSTABLE_FIELDS = ("open", "high", "low", "close", "volume")
ADJUSTMENT_OPERATIONS = ("multiply", "add", "set")


def normalize_adjustments(raw_adjustments: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if not raw_adjustments:
        return []
    if isinstance(raw_adjustments, Mapping):
        raise ValueError("data.adjustments 必须是列表")

    normalized: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_adjustments):
        symbol = str(raw.get("symbol", "")).strip()
        field = str(raw.get("field", "")).strip().lower()
        operation = str(raw.get("operation", "")).strip().lower()
        if not symbol:
            raise ValueError(f"第 {position + 1} 条数据调整缺少代码")
        if field not in ADJUSTABLE_FIELDS:
            raise ValueError(f"第 {position + 1} 条数据调整字段不支持：{field}")
        if operation not in ADJUSTMENT_OPERATIONS:
            raise ValueError(f"第 {position + 1} 条数据调整操作不支持：{operation}")
        try:
            value = float(raw["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {position + 1} 条数据调整必须包含数值") from exc

        start = pd.Timestamp(raw["start"]) if raw.get("start") else None
        end = pd.Timestamp(raw["end"]) if raw.get("end") else None
        if start is not None and end is not None and start > end:
            raise ValueError(f"第 {position + 1} 条数据调整开始日期不能晚于结束日期")

        normalized.append(
            {
                "symbol": symbol,
                "field": field,
                "operation": operation,
                "value": value,
                "start": start,
                "end": end,
                "note": str(raw.get("note", "")).strip(),
            }
        )
    return normalized


def apply_market_data_adjustments(
    data: pd.DataFrame, raw_adjustments: Iterable[Mapping[str, Any]] | None
) -> pd.DataFrame:
    market = normalize_market_frame(data)
    adjustments = normalize_adjustments(raw_adjustments)
    if not adjustments:
        return market

    adjusted = market.copy()
    for adjustment in adjustments:
        mask = adjusted["symbol"].eq(adjustment["symbol"])
        if adjustment["start"] is not None:
            mask &= adjusted["timestamp"].ge(adjustment["start"])
        if adjustment["end"] is not None:
            mask &= adjusted["timestamp"].le(adjustment["end"])
        if not mask.any():
            continue

        field = adjustment["field"]
        value = adjustment["value"]
        if adjustment["operation"] == "multiply":
            adjusted.loc[mask, field] = adjusted.loc[mask, field] * value
        elif adjustment["operation"] == "add":
            adjusted.loc[mask, field] = adjusted.loc[mask, field] + value
        else:
            adjusted.loc[mask, field] = value

    adjusted["volume"] = adjusted["volume"].clip(lower=0.0)
    adjusted[["open", "high", "low", "close"]] = adjusted[["open", "high", "low", "close"]].clip(lower=0.01)
    adjusted["high"] = adjusted[["high", "open", "close"]].max(axis=1)
    adjusted["low"] = adjusted[["low", "open", "close"]].min(axis=1)
    adjustable_columns = list(ADJUSTABLE_FIELDS)
    adjusted[adjustable_columns] = adjusted[adjustable_columns].replace([np.inf, -np.inf], np.nan)
    return normalize_market_frame(adjusted)
