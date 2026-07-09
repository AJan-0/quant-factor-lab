from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


SOURCE_STATUS_COLUMNS = [
    "scope",
    "symbol",
    "provider",
    "status",
    "rows",
    "latest_timestamp",
    "freshness_minutes",
    "message",
]

PROVIDER_LABELS = {
    "synthetic": "合成演示数据",
    "csv": "本地CSV数据",
    "yfinance": "Yahoo Finance数据",
    "ccxt": "CCXT交易所数据",
    "okx": "OKX公开REST真实数据",
}
REAL_DATA_PROVIDERS = {"csv", "yfinance", "ccxt", "okx"}


def build_source_health_report(
    config: dict[str, Any],
    market_data: pd.DataFrame,
    derivatives_snapshot: pd.DataFrame,
    derivatives_history: pd.DataFrame,
    microstructure_snapshot: pd.DataFrame,
    onchain_metrics: pd.DataFrame | None = None,
    onchain_warnings: tuple[str, ...] | list[str] = (),
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    data_config = config.get("data", {})
    provider = str(data_config.get("provider", "synthetic")).lower()
    frequency = str(data_config.get("frequency", "1d")).lower()
    generated_at = generated_at or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    messages: list[str] = []

    rows.extend(_market_rows(provider, frequency, market_data, generated_at))
    rows.extend(_context_rows("衍生品快照", provider, derivatives_snapshot, generated_at, stale_after_minutes=60 * 12))
    rows.extend(_context_rows("衍生品历史", provider, derivatives_history, generated_at, stale_after_minutes=60 * 24 * 4))
    rows.extend(_context_rows("盘口逐笔", provider, microstructure_snapshot, generated_at, stale_after_minutes=30))
    rows.extend(_onchain_rows(config, onchain_metrics, generated_at))

    if provider == "synthetic":
        messages.append("当前使用合成数据，只适合验证流程，不应用于真实交易判断。")
    elif provider == "okx":
        messages.append("当前使用OKX公开REST行情。无需API密钥，但仍需关注网络延迟、限频和交易所维护窗口。")
    elif provider == "csv":
        messages.append(f"当前使用本地CSV数据：{data_config.get('path') or '未配置路径'}。请确认文件来源和更新时间。")
    else:
        messages.append(f"当前使用{PROVIDER_LABELS.get(provider, provider)}，请确认供应商授权和延迟属性。")
    if any(row["status"] == "STALE" for row in rows):
        messages.append("存在数据新鲜度警告，建议先刷新流水线再做盘中判断。")
    if provider == "okx" and any(row["status"] == "MISSING" for row in rows if row["scope"] != "衍生品历史"):
        messages.append("部分OKX上下文缺失，决策卡会降低可解释性。")
    for warning in onchain_warnings:
        messages.append(f"链上数据提示：{warning}")

    return {
        "summary": {
            "provider": provider,
            "provider_label": PROVIDER_LABELS.get(provider, provider),
            "is_real_data": provider in REAL_DATA_PROVIDERS,
            "frequency": frequency,
            "generated_at": generated_at.isoformat(),
            "status": _overall_status([row["status"] for row in rows], provider),
            "latest_market_timestamp": _latest_timestamp(market_data),
            "message_count": len(messages),
        },
        "rows": rows,
        "messages": messages,
    }


def source_status_frame(report: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(report.get("rows", []), columns=SOURCE_STATUS_COLUMNS)


def _market_rows(provider: str, frequency: str, market_data: pd.DataFrame, generated_at: datetime) -> list[dict[str, Any]]:
    if market_data.empty or "symbol" not in market_data.columns:
        return [_row("市场K线", "ALL", provider, "MISSING", 0, None, None, "未生成市场K线数据。")]
    rows = []
    stale_after = _stale_threshold_minutes(frequency)
    for symbol, group in market_data.groupby("symbol", sort=True):
        latest = _latest_timestamp(group)
        freshness = _freshness_minutes(latest, generated_at)
        status = _freshness_status(provider, freshness, stale_after)
        rows.append(_row("市场K线", str(symbol), provider, status, len(group), latest, freshness, _market_message(provider, freshness, stale_after)))
    return rows


def _context_rows(scope: str, provider: str, frame: pd.DataFrame, generated_at: datetime, stale_after_minutes: int) -> list[dict[str, Any]]:
    if provider != "okx":
        return [_row(scope, "ALL", provider, "SKIPPED", 0, None, None, "非OKX数据源未启用该上下文。")]
    if frame.empty or "symbol" not in frame.columns:
        return [_row(scope, "ALL", provider, "MISSING", 0, None, None, f"未获取{scope}数据。")]
    rows = []
    for symbol, group in frame.groupby("symbol", sort=True):
        latest = _latest_timestamp(group)
        freshness = _freshness_minutes(latest, generated_at)
        status = "OK" if freshness is not None and freshness <= stale_after_minutes else "STALE"
        rows.append(_row(scope, str(symbol), provider, status, len(group), latest, freshness, f"{scope}最近更新时间{_freshness_text(freshness)}。"))
    return rows


def _onchain_rows(config: dict[str, Any], frame: pd.DataFrame | None, generated_at: datetime) -> list[dict[str, Any]]:
    onchain_config = config.get("onchain", {})
    if not onchain_config or onchain_config.get("enabled") is False:
        return [_row("链上指标", "ALL", "coinmetrics_community", "SKIPPED", 0, None, None, "链上上下文未启用。")]
    provider = str(onchain_config.get("provider", "coinmetrics_community")).lower()
    if frame is None or frame.empty or "symbol" not in frame.columns:
        return [_row("链上指标", "ALL", provider, "MISSING", 0, None, None, "未获取到链上指标。")]
    rows = []
    for symbol, group in frame.groupby("symbol", sort=True):
        latest = _latest_timestamp(group)
        freshness = _freshness_minutes(latest, generated_at)
        status = "OK" if freshness is not None and freshness <= 60 * 24 * 4 else "STALE"
        rows.append(_row("链上指标", str(symbol), provider, status, len(group), latest, freshness, f"链上指标最近更新时间{_freshness_text(freshness)}。"))
    return rows


def _row(scope: str, symbol: str, provider: str, status: str, rows: int, latest: str | None, freshness: float | None, message: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "symbol": symbol,
        "provider": provider,
        "status": status,
        "rows": int(rows),
        "latest_timestamp": latest,
        "freshness_minutes": freshness,
        "message": message,
    }


def _freshness_status(provider: str, freshness: float | None, stale_after_minutes: int) -> str:
    if freshness is None:
        return "MISSING"
    if provider == "synthetic":
        return "SIMULATED"
    return "OK" if freshness <= stale_after_minutes else "STALE"


def _market_message(provider: str, freshness: float | None, stale_after_minutes: int) -> str:
    if provider == "synthetic":
        return "合成K线仅用于流程验证。"
    if freshness is None:
        return "无法识别最新K线时间。"
    if freshness > stale_after_minutes:
        return f"最新K线距本次生成约{_freshness_text(freshness)}，超过预期阈值。"
    return f"最新K线距本次生成约{_freshness_text(freshness)}。"


def _stale_threshold_minutes(frequency: str) -> int:
    return {"1m": 10, "5m": 30, "1h": 180, "1d": 60 * 24 * 3}.get(frequency, 60 * 24)


def _freshness_text(minutes: float | None) -> str:
    if minutes is None:
        return "未知"
    if minutes < 90:
        return f"{minutes:.0f}分钟"
    hours = minutes / 60
    if hours < 72:
        return f"{hours:.1f}小时"
    return f"{hours / 24:.1f}天"


def _overall_status(statuses: list[str], provider: str) -> str:
    if not statuses:
        return "MISSING"
    if provider == "synthetic":
        return "SIMULATED"
    if "MISSING" in statuses:
        return "WARN"
    if "STALE" in statuses:
        return "STALE"
    return "OK"


def _latest_timestamp(frame: pd.DataFrame) -> str | None:
    if frame.empty or "timestamp" not in frame.columns:
        return None
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    if timestamps.dropna().empty:
        return None
    return str(timestamps.max())


def _freshness_minutes(latest_timestamp: str | None, generated_at: datetime) -> float | None:
    if latest_timestamp is None:
        return None
    latest = pd.Timestamp(latest_timestamp)
    latest = latest.tz_localize(timezone.utc) if latest.tzinfo is None else latest.tz_convert(timezone.utc)
    generated = pd.Timestamp(generated_at)
    generated = generated.tz_localize(timezone.utc) if generated.tzinfo is None else generated.tz_convert(timezone.utc)
    return float(max((generated - latest).total_seconds() / 60, 0.0))
