from __future__ import annotations

from typing import Any

import pandas as pd

from .data import normalize_market_frame


DECISION_CARD_COLUMNS = [
    "symbol",
    "timestamp",
    "last_close",
    "return_1d",
    "return_5d",
    "stance",
    "stance_label",
    "confidence",
    "primary_factor",
    "primary_signal",
    "funding_annualized",
    "funding_change",
    "oi_usd",
    "oi_change",
    "long_short_ratio",
    "crowding",
    "crowding_label",
    "microstructure_label",
    "spread_bps",
    "depth_imbalance",
    "active_addresses",
    "active_address_change",
    "tx_count",
    "tx_count_change",
    "onchain_label",
    "data_quality_status",
    "evidence",
    "risk_note",
    "invalidation_note",
    "action_hint",
]


def build_decision_cards(
    market_data: pd.DataFrame,
    factor_signals: pd.DataFrame,
    derivatives_snapshot: pd.DataFrame,
    data_quality: dict[str, Any] | None,
    derivatives_history: pd.DataFrame | None = None,
    microstructure_snapshot: pd.DataFrame | None = None,
    onchain_metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    market = normalize_market_frame(market_data)
    cards: list[dict[str, Any]] = []
    quality_status = (data_quality or {}).get("summary", {}).get("status", "UNKNOWN")
    for symbol, symbol_market in market.groupby("symbol", sort=True):
        symbol_market = symbol_market.sort_values("timestamp")
        latest = symbol_market.iloc[-1]
        close = float(latest["close"])
        return_1d = _lookback_return(symbol_market, 1)
        return_5d = _lookback_return(symbol_market, 5)
        symbol_signals = factor_signals[factor_signals["symbol"].astype(str) == str(symbol)] if not factor_signals.empty else pd.DataFrame()
        primary_signal = _primary_signal(symbol_signals)
        derivative = _derivative_row(derivatives_snapshot, str(symbol))
        derivative_history = _derivative_history_context(derivatives_history, str(symbol))
        microstructure = _microstructure_row(microstructure_snapshot, str(symbol))
        onchain = _onchain_row(onchain_metrics, str(symbol))
        stance, label, confidence = _stance(symbol_signals, primary_signal, derivative, microstructure, onchain)
        evidence = _evidence(primary_signal, derivative, derivative_history, microstructure, onchain, return_1d, return_5d)
        risk_note = _risk_note(stance, derivative, microstructure, quality_status)
        invalidation_note = _invalidation_note(stance, primary_signal, derivative)
        action_hint = _action_hint(stance, derivative, microstructure)
        cards.append(
            {
                "symbol": str(symbol),
                "timestamp": str(latest["timestamp"]),
                "last_close": close,
                "return_1d": return_1d,
                "return_5d": return_5d,
                "stance": stance,
                "stance_label": label,
                "confidence": confidence,
                "primary_factor": primary_signal.get("factor"),
                "primary_signal": primary_signal.get("signal_label") or primary_signal.get("signal"),
                "funding_annualized": _safe_float(derivative.get("annualized_funding_rate")),
                "funding_change": derivative_history.get("funding_change"),
                "oi_usd": _safe_float(derivative.get("oi_usd")),
                "oi_change": derivative_history.get("oi_change"),
                "long_short_ratio": derivative_history.get("long_short_ratio"),
                "crowding": derivative.get("crowding"),
                "crowding_label": derivative.get("crowding_label"),
                "microstructure_label": microstructure.get("microstructure_label"),
                "spread_bps": _safe_float(microstructure.get("spread_bps")),
                "depth_imbalance": _safe_float(microstructure.get("depth_imbalance")),
                "active_addresses": _safe_float(onchain.get("active_addresses")),
                "active_address_change": _safe_float(onchain.get("active_address_change")),
                "tx_count": _safe_float(onchain.get("tx_count")),
                "tx_count_change": _safe_float(onchain.get("tx_count_change")),
                "onchain_label": onchain.get("onchain_label"),
                "data_quality_status": quality_status,
                "evidence": evidence,
                "risk_note": risk_note,
                "invalidation_note": invalidation_note,
                "action_hint": action_hint,
            }
        )
    if not cards:
        return pd.DataFrame(columns=DECISION_CARD_COLUMNS)
    return pd.DataFrame(cards, columns=DECISION_CARD_COLUMNS)


def _primary_signal(signals: pd.DataFrame) -> dict[str, Any]:
    if signals.empty:
        return {}
    frame = signals.copy()
    frame["signal_score"] = pd.to_numeric(frame.get("signal_score", 0), errors="coerce").fillna(0)
    frame["confidence"] = pd.to_numeric(frame.get("confidence", 0), errors="coerce").fillna(0)
    row = frame.sort_values(["signal_score", "confidence"], ascending=False).iloc[0]
    return row.to_dict()


def _derivative_row(derivatives_snapshot: pd.DataFrame, symbol: str) -> dict[str, Any]:
    if derivatives_snapshot.empty or "symbol" not in derivatives_snapshot:
        return {}
    rows = derivatives_snapshot[derivatives_snapshot["symbol"].astype(str) == symbol]
    return rows.iloc[0].to_dict() if not rows.empty else {}


def _derivative_history_context(derivatives_history: pd.DataFrame | None, symbol: str) -> dict[str, Any]:
    if derivatives_history is None or derivatives_history.empty or "symbol" not in derivatives_history:
        return {}
    rows = derivatives_history[derivatives_history["symbol"].astype(str) == symbol].copy()
    if rows.empty:
        return {}
    rows["timestamp"] = pd.to_datetime(rows["timestamp"], errors="coerce")
    rows = rows.sort_values("timestamp")
    latest_funding, previous_funding = _latest_previous_non_null(rows, "annualized_funding_rate")
    latest_oi, previous_oi = _latest_previous_non_null(rows, "oi_usd")
    latest_ratio, _ = _latest_previous_non_null(rows, "long_short_ratio")
    return {
        "funding_change": _diff(latest_funding, previous_funding),
        "oi_change": _pct_change(latest_oi, previous_oi),
        "long_short_ratio": _safe_float(latest_ratio),
    }


def _microstructure_row(microstructure_snapshot: pd.DataFrame | None, symbol: str) -> dict[str, Any]:
    if microstructure_snapshot is None or microstructure_snapshot.empty or "symbol" not in microstructure_snapshot:
        return {}
    rows = microstructure_snapshot[microstructure_snapshot["symbol"].astype(str) == symbol]
    return rows.iloc[0].to_dict() if not rows.empty else {}


def _onchain_row(onchain_metrics: pd.DataFrame | None, symbol: str) -> dict[str, Any]:
    if onchain_metrics is None or onchain_metrics.empty or "symbol" not in onchain_metrics:
        return {}
    rows = onchain_metrics[onchain_metrics["symbol"].astype(str) == symbol].copy()
    if rows.empty:
        return {}
    if "timestamp" in rows.columns:
        rows["timestamp"] = pd.to_datetime(rows["timestamp"], errors="coerce")
        rows = rows.sort_values("timestamp")
    return rows.iloc[-1].to_dict()


def _stance(
    signals: pd.DataFrame,
    primary_signal: dict[str, Any],
    derivative: dict[str, Any],
    microstructure: dict[str, Any],
    onchain: dict[str, Any],
) -> tuple[str, str, float]:
    bullish = int((signals.get("signal") == "BULLISH").sum()) if not signals.empty else 0
    bearish = int((signals.get("signal") == "BEARISH").sum()) if not signals.empty else 0
    confidence = _safe_float(primary_signal.get("confidence")) or 0.0
    confidence = _adjust_confidence_for_context(confidence, derivative, microstructure, onchain)
    crowding = derivative.get("crowding")
    if bullish > bearish and crowding in {"LONG_CROWDED", "LONG_WARM"}:
        return "WATCH_LONG", "偏多但等待", confidence
    if bearish > bullish and crowding in {"SHORT_CROWDED", "SHORT_WARM"}:
        return "WATCH_SHORT", "偏空但等待", confidence
    if bullish > bearish:
        return "LEAN_LONG", "偏多", confidence
    if bearish > bullish:
        return "LEAN_SHORT", "偏空", confidence
    return "NEUTRAL", "观望", confidence


def _evidence(
    primary_signal: dict[str, Any],
    derivative: dict[str, Any],
    derivative_history: dict[str, Any],
    microstructure: dict[str, Any],
    onchain: dict[str, Any],
    return_1d: float | None,
    return_5d: float | None,
) -> str:
    parts = []
    if primary_signal:
        parts.append(str(primary_signal.get("interpretation") or primary_signal.get("setup") or "因子信号存在"))
    if derivative:
        parts.append(str(derivative.get("interpretation") or derivative.get("crowding_label") or "衍生品快照存在"))
    history_text = _derivative_history_text(derivative_history)
    if history_text:
        parts.append(history_text)
    if microstructure:
        parts.append(str(microstructure.get("interpretation") or microstructure.get("microstructure_label") or "盘口逐笔快照存在"))
    if onchain:
        parts.append(str(onchain.get("interpretation") or onchain.get("onchain_label") or "链上指标存在"))
    if return_1d is not None and return_5d is not None:
        parts.append(f"1日收益{return_1d:.2%}，5日收益{return_5d:.2%}")
    return "；".join(parts) if parts else "暂无足够证据。"


def _risk_note(stance: str, derivative: dict[str, Any], microstructure: dict[str, Any], quality_status: str) -> str:
    notes = []
    if quality_status != "PASS":
        notes.append("数据质量未完全通过，降低信号权重")
    crowding = derivative.get("crowding")
    if stance in {"WATCH_LONG", "LEAN_LONG"} and crowding in {"LONG_CROWDED", "LONG_WARM"}:
        notes.append("多头资金费率偏热，追多需等待回撤或拥挤缓和")
    if stance in {"WATCH_SHORT", "LEAN_SHORT"} and crowding in {"SHORT_CROWDED", "SHORT_WARM"}:
        notes.append("空头拥挤，追空需警惕回补反抽")
    spread_bps = _safe_float(microstructure.get("spread_bps"))
    if spread_bps is not None and spread_bps > 8:
        notes.append("盘口价差偏宽，主观交易应降低追价冲动")
    return "；".join(notes) if notes else "未见极端拥挤，按信号失效条件管理风险。"


def _action_hint(stance: str, derivative: dict[str, Any], microstructure: dict[str, Any]) -> str:
    micro_label = str(microstructure.get("microstructure_label") or "")
    if stance == "WATCH_LONG":
        return "只做观察或等待低风险回调，不建议在拥挤高位直接追多。"
    if stance == "WATCH_SHORT":
        return "只做观察或等待反抽失败，不建议在空头拥挤时直接追空。"
    if stance == "LEAN_LONG":
        if micro_label == "买盘占优":
            return "偏多信号与盘口买盘共振，可优先等待回踩确认后寻找顺势多头结构。"
        return "可优先寻找顺势多头结构，同时设置因子转弱或资金费率过热为失效条件。"
    if stance == "LEAN_SHORT":
        if micro_label == "卖盘占优":
            return "偏空信号与盘口卖盘共振，可优先等待反抽失败后寻找空头结构。"
        return "可优先寻找反弹做空结构，同时设置因子转强或空头回补为失效条件。"
    return "保持观望，等待量价、因子和衍生品信号形成共振。"


def _invalidation_note(stance: str, primary_signal: dict[str, Any], derivative: dict[str, Any]) -> str:
    factor = primary_signal.get("factor") or "\u4e3b\u56e0\u5b50"
    crowding = derivative.get("crowding")
    if stance in {"LEAN_LONG", "WATCH_LONG"}:
        if crowding in {"LONG_CROWDED", "LONG_WARM"}:
            return f"{factor}\u56de\u5230\u4e2d\u6027\u6216\u8d44\u91d1\u8d39\u7387\u7ee7\u7eed\u5347\u6e29\u65f6\uff0c\u591a\u5934\u89c2\u70b9\u5931\u6548\u3002"
        return f"{factor}\u8dcc\u56de\u5386\u53f2\u4e2d\u67a2\u4ee5\u4e0b\uff0c\u6216\u4ef7\u683c\u8dcc\u7834\u8fd1\u671f\u7ed3\u6784\u4f4e\u70b9\u65f6\uff0c\u591a\u5934\u89c2\u70b9\u5931\u6548\u3002"
    if stance in {"LEAN_SHORT", "WATCH_SHORT"}:
        if crowding in {"SHORT_CROWDED", "SHORT_WARM"}:
            return f"{factor}\u56de\u5230\u4e2d\u6027\u6216\u7a7a\u5934\u56de\u8865\u5bfc\u81f4\u4ef7\u683c\u5f3a\u53cd\u5f39\u65f6\uff0c\u7a7a\u5934\u89c2\u70b9\u5931\u6548\u3002"
        return f"{factor}\u5347\u56de\u5386\u53f2\u4e2d\u67a2\u4ee5\u4e0a\uff0c\u6216\u4ef7\u683c\u7a81\u7834\u8fd1\u671f\u7ed3\u6784\u9ad8\u70b9\u65f6\uff0c\u7a7a\u5934\u89c2\u70b9\u5931\u6548\u3002"
    if primary_signal:
        return f"{factor}\u8131\u79bb\u5f53\u524d\u5206\u4f4d\u72b6\u6001\u540e\u91cd\u65b0\u8bc4\u4f30\uff0c\u4e0d\u5728\u4e2d\u6027\u533a\u95f4\u4e3b\u52a8\u62bc\u6ce8\u3002"
    return "\u7f3a\u5c11\u53ef\u9a8c\u8bc1\u4e3b\u56e0\u5b50\u65f6\u4e0d\u5efa\u7acb\u65b9\u5411\u6027\u5047\u8bbe\u3002"


def _adjust_confidence_for_context(
    confidence: float,
    derivative: dict[str, Any],
    microstructure: dict[str, Any],
    onchain: dict[str, Any],
) -> float:
    adjusted = confidence
    crowding = derivative.get("crowding")
    if crowding in {"LONG_CROWDED", "SHORT_CROWDED"}:
        adjusted *= 0.85
    spread_bps = _safe_float(microstructure.get("spread_bps"))
    if spread_bps is not None and spread_bps > 8:
        adjusted *= 0.85
    if microstructure.get("microstructure_label") in {"买盘占优", "卖盘占优"}:
        adjusted = min(adjusted * 1.05, 1.0)
    if onchain.get("onchain_label") in {"链上扩张", "链上偏强"}:
        adjusted = min(adjusted * 1.05, 1.0)
    if onchain.get("onchain_label") in {"链上收缩", "链上偏弱"}:
        adjusted *= 0.92
    return float(min(max(adjusted, 0.0), 1.0))


def _derivative_history_text(context: dict[str, Any]) -> str | None:
    if not context:
        return None
    parts = []
    funding_change = _safe_float(context.get("funding_change"))
    oi_change = _safe_float(context.get("oi_change"))
    long_short_ratio = _safe_float(context.get("long_short_ratio"))
    if funding_change is not None:
        parts.append(f"资金费率变化{funding_change:+.2%}")
    if oi_change is not None:
        parts.append(f"OI变化{oi_change:+.2%}")
    if long_short_ratio is not None:
        parts.append(f"多空账户比{long_short_ratio:.2f}")
    return "，".join(parts) if parts else None


def _diff(latest: Any, previous: Any) -> float | None:
    latest_float = _safe_float(latest)
    previous_float = _safe_float(previous)
    if latest_float is None or previous_float is None:
        return None
    return latest_float - previous_float


def _pct_change(latest: Any, previous: Any) -> float | None:
    latest_float = _safe_float(latest)
    previous_float = _safe_float(previous)
    if latest_float is None or previous_float in (None, 0):
        return None
    return latest_float / previous_float - 1.0


def _latest_previous_non_null(frame: pd.DataFrame, column: str) -> tuple[Any, Any]:
    if column not in frame.columns:
        return None, None
    series = frame[column].dropna()
    if series.empty:
        return None, None
    latest = series.iloc[-1]
    previous = series.iloc[-2] if len(series) > 1 else None
    return latest, previous


def _lookback_return(frame: pd.DataFrame, periods: int) -> float | None:
    if len(frame) <= periods:
        return None
    close = pd.to_numeric(frame["close"], errors="coerce")
    latest = close.iloc[-1]
    previous = close.iloc[-1 - periods]
    if not pd.notna(latest) or not pd.notna(previous) or previous == 0:
        return None
    return float(latest / previous - 1.0)


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None
