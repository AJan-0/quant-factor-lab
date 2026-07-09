from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import pandas as pd

from .types import AssetClass, DataRequest, Instrument


ONCHAIN_COLUMNS = [
    "timestamp",
    "symbol",
    "provider",
    "asset",
    "active_addresses",
    "active_address_change",
    "tx_count",
    "tx_count_change",
    "onchain_label",
    "interpretation",
]

COINMETRICS_METRIC_MAP = {
    "AdrActCnt": "active_addresses",
    "TxCnt": "tx_count",
}


@dataclass(frozen=True)
class OnchainLoadResult:
    data: pd.DataFrame
    warnings: tuple[str, ...] = ()
    provider: str = "coinmetrics_community"


class CoinMetricsCommunityOnchainProvider:
    """Coin Metrics community API provider for public BTC/ETH chain metrics."""

    BASE_URL = "https://community-api.coinmetrics.io"
    METRICS_PATH = "/v4/timeseries/asset-metrics"

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        metrics: tuple[str, ...] = ("AdrActCnt", "TxCnt"),
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.metrics = metrics
        self._fetch_json = fetch_json or self._default_fetch_json

    def load(self, request: DataRequest) -> OnchainLoadResult:
        frames: list[pd.DataFrame] = []
        warnings: list[str] = []
        for instrument in request.universe:
            if instrument.asset_class != AssetClass.CRYPTO:
                continue
            asset = coinmetrics_asset(instrument)
            if not asset:
                warnings.append(f"Unsupported on-chain asset mapping for {instrument.symbol}")
                continue
            frame, asset_warnings = self._instrument_metrics(instrument.symbol, asset, request)
            warnings.extend(asset_warnings)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return OnchainLoadResult(pd.DataFrame(columns=ONCHAIN_COLUMNS), tuple(warnings))
        data = pd.concat(frames, ignore_index=True)
        data["timestamp"] = pd.to_datetime(data["timestamp"], errors="coerce")
        data = data.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
        data = _add_context_columns(data)
        return OnchainLoadResult(data[ONCHAIN_COLUMNS], tuple(warnings))

    def _instrument_metrics(
        self,
        symbol: str,
        asset: str,
        request: DataRequest,
    ) -> tuple[pd.DataFrame, list[str]]:
        merged: pd.DataFrame | None = None
        warnings: list[str] = []
        for metric in self.metrics:
            output_column = COINMETRICS_METRIC_MAP.get(metric)
            if output_column is None:
                warnings.append(f"Unsupported Coin Metrics metric skipped: {metric}")
                continue
            try:
                payload = self._fetch_json(
                    self.METRICS_PATH,
                    {
                        "assets": asset,
                        "metrics": metric,
                        "start_time": request.start.strftime("%Y-%m-%d"),
                        "end_time": request.end.strftime("%Y-%m-%d"),
                        "page_size": "10000",
                    },
                )
            except Exception as exc:
                warnings.append(f"Coin Metrics {metric} unavailable for {symbol}: {exc}")
                continue
            frame = _metric_frame(symbol, asset, metric, output_column, payload)
            if frame.empty:
                warnings.append(f"Coin Metrics {metric} returned no rows for {symbol}")
                continue
            merged = frame if merged is None else merged.merge(frame, on=["timestamp", "symbol", "provider", "asset"], how="outer")
        if merged is None:
            return pd.DataFrame(columns=["timestamp", "symbol", "provider", "asset"]), warnings
        return merged, warnings

    def _default_fetch_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = UrlRequest(url, headers={"User-Agent": "QuantFactorLab/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def load_onchain_context(config: dict[str, Any], request: DataRequest) -> OnchainLoadResult:
    onchain_config = config.get("onchain", {})
    if not onchain_config or onchain_config.get("enabled") is False:
        return OnchainLoadResult(pd.DataFrame(columns=ONCHAIN_COLUMNS), provider="disabled")
    provider = str(onchain_config.get("provider", "coinmetrics_community")).lower()
    if provider != "coinmetrics_community":
        return OnchainLoadResult(
            pd.DataFrame(columns=ONCHAIN_COLUMNS),
            warnings=(f"Unsupported on-chain provider: {provider}",),
            provider=provider,
        )
    metrics = tuple(str(metric) for metric in onchain_config.get("metrics", ["AdrActCnt", "TxCnt"]))
    loader = CoinMetricsCommunityOnchainProvider(
        timeout=float(onchain_config.get("timeout", 20.0)),
        metrics=metrics,
    )
    return loader.load(request)


def coinmetrics_asset(instrument: Instrument) -> str | None:
    base = str(instrument.symbol).upper().replace("/", "-").split("-")[0]
    mapping = {"BTC": "btc", "ETH": "eth"}
    return mapping.get(base)


def _metric_frame(symbol: str, asset: str, metric: str, output_column: str, payload: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for item in payload.get("data") or []:
        value = _safe_float(item.get(metric))
        rows.append(
            {
                "timestamp": item.get("time"),
                "symbol": symbol,
                "provider": "coinmetrics_community",
                "asset": asset,
                output_column: value,
            }
        )
    return pd.DataFrame(rows)


def _add_context_columns(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    for column in ("active_addresses", "tx_count"):
        if column not in data.columns:
            data[column] = None
        data[column] = pd.to_numeric(data[column], errors="coerce")
    grouped = data.groupby("symbol", sort=False)
    data["active_address_change"] = grouped["active_addresses"].pct_change(7)
    data["tx_count_change"] = grouped["tx_count"].pct_change(7)
    data["onchain_label"] = data.apply(_label_row, axis=1)
    data["interpretation"] = data.apply(_interpretation_row, axis=1)
    return data


def _label_row(row: pd.Series) -> str:
    active_change = _safe_float(row.get("active_address_change"))
    tx_change = _safe_float(row.get("tx_count_change"))
    if active_change is None and tx_change is None:
        return "链上中性"
    positive = sum((value or 0) > 0.05 for value in (active_change, tx_change))
    negative = sum((value or 0) < -0.05 for value in (active_change, tx_change))
    if positive >= 2:
        return "链上扩张"
    if negative >= 2:
        return "链上收缩"
    if positive > negative:
        return "链上偏强"
    if negative > positive:
        return "链上偏弱"
    return "链上中性"


def _interpretation_row(row: pd.Series) -> str:
    label = row.get("onchain_label") or "链上中性"
    active = _safe_float(row.get("active_addresses"))
    tx_count = _safe_float(row.get("tx_count"))
    active_change = _safe_float(row.get("active_address_change"))
    tx_change = _safe_float(row.get("tx_count_change"))
    active_text = "活跃地址暂无" if active is None else f"活跃地址 {active:,.0f}"
    tx_text = "交易数暂无" if tx_count is None else f"交易数 {tx_count:,.0f}"
    active_delta = "7日变化暂无" if active_change is None else f"活跃地址7日变化 {active_change:+.1%}"
    tx_delta = "交易数7日变化暂无" if tx_change is None else f"交易数7日变化 {tx_change:+.1%}"
    return f"{label}：{active_text}，{tx_text}，{active_delta}，{tx_delta}。"


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None
