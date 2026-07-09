from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import pandas as pd

from .types import AssetClass, DataRequest, Instrument


DERIVATIVES_COLUMNS = [
    "timestamp",
    "symbol",
    "exchange",
    "inst_id",
    "inst_type",
    "funding_rate",
    "annualized_funding_rate",
    "premium",
    "interest_rate",
    "next_funding_time",
    "oi_contracts",
    "oi_ccy",
    "oi_usd",
    "crowding",
    "crowding_label",
    "interpretation",
]

DERIVATIVES_HISTORY_COLUMNS = [
    "timestamp",
    "symbol",
    "exchange",
    "inst_id",
    "inst_type",
    "funding_rate",
    "realized_rate",
    "annualized_funding_rate",
    "oi_contracts",
    "oi_ccy",
    "oi_usd",
    "long_short_ratio",
]


class OKXDerivativesSnapshotProvider:
    """OKX public derivatives snapshot for swap funding and open interest."""

    BASE_URL = "https://www.okx.com"
    FUNDING_RATE_PATH = "/api/v5/public/funding-rate"
    OPEN_INTEREST_PATH = "/api/v5/public/open-interest"

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._fetch_json = fetch_json or self._default_fetch_json

    def load(self, request: DataRequest) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for instrument in request.universe:
            if instrument.asset_class != AssetClass.CRYPTO:
                continue
            inst_id = self.okx_swap_inst_id(instrument)
            funding = self._first_okx_row(self.FUNDING_RATE_PATH, {"instId": inst_id})
            open_interest = self._first_okx_row(self.OPEN_INTEREST_PATH, {"instType": "SWAP", "instId": inst_id})
            rows.append(_snapshot_row(instrument.symbol, inst_id, funding, open_interest))
        if not rows:
            return pd.DataFrame(columns=DERIVATIVES_COLUMNS)
        return pd.DataFrame(rows, columns=DERIVATIVES_COLUMNS)

    def _first_okx_row(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        payload = self._fetch_json(path, params)
        if str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX derivatives error {payload.get('code')}: {payload.get('msg')}")
        data = payload.get("data") or []
        return data[0] if data else {}

    def _default_fetch_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = UrlRequest(url, headers={"User-Agent": "QuantFactorLab/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def okx_swap_inst_id(instrument: Instrument) -> str:
        raw_symbol = instrument.exchange or instrument.symbol
        normalized = str(raw_symbol).strip().upper().replace("/", "-")
        if normalized.endswith("-SWAP"):
            return normalized
        if normalized.endswith("-USD"):
            return f"{normalized[:-4]}-USDT-SWAP"
        if normalized.endswith("-USDT"):
            return f"{normalized}-SWAP"
        return f"{normalized}-USDT-SWAP"


class OKXDerivativesHistoryProvider:
    """OKX public historical derivatives context for swaps."""

    BASE_URL = "https://www.okx.com"
    FUNDING_RATE_HISTORY_PATH = "/api/v5/public/funding-rate-history"
    OPEN_INTEREST_HISTORY_PATH = "/api/v5/rubik/stat/contracts/open-interest-history"
    LONG_SHORT_RATIO_PATH = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        limit: int = 100,
        period: str = "1D",
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.limit = max(1, min(int(limit), 100))
        self.period = str(period)
        self._fetch_json = fetch_json or self._default_fetch_json

    def load(self, request: DataRequest) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for instrument in request.universe:
            if instrument.asset_class != AssetClass.CRYPTO:
                continue
            inst_id = OKXDerivativesSnapshotProvider.okx_swap_inst_id(instrument)
            history = self._instrument_history(instrument.symbol, inst_id)
            if not history.empty:
                frames.append(history)
        if not frames:
            return pd.DataFrame(columns=DERIVATIVES_HISTORY_COLUMNS)
        frame = pd.concat(frames, ignore_index=True).sort_values(["symbol", "timestamp"]).reset_index(drop=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        return frame[(frame["timestamp"] >= request.start) & (frame["timestamp"] < request.end)][DERIVATIVES_HISTORY_COLUMNS]

    def _instrument_history(self, symbol: str, inst_id: str) -> pd.DataFrame:
        funding_payload = self._fetch_json(
            self.FUNDING_RATE_HISTORY_PATH,
            {"instId": inst_id, "limit": str(self.limit)},
        )
        oi_payload = self._fetch_json(
            self.OPEN_INTEREST_HISTORY_PATH,
            {"instId": inst_id, "period": self.period, "limit": str(self.limit)},
        )
        ratio_payload = self._fetch_json(
            self.LONG_SHORT_RATIO_PATH,
            {"instId": inst_id, "period": self.period, "limit": str(self.limit)},
        )
        funding = _funding_history_frame(symbol, inst_id, funding_payload)
        open_interest = _open_interest_history_frame(symbol, inst_id, oi_payload)
        ratio = _long_short_ratio_frame(symbol, inst_id, ratio_payload)
        merged = funding.merge(open_interest, on=["timestamp", "symbol", "exchange", "inst_id", "inst_type"], how="outer")
        merged = merged.merge(ratio, on=["timestamp", "symbol", "exchange", "inst_id", "inst_type"], how="outer")
        return merged.sort_values("timestamp").reset_index(drop=True)

    def _default_fetch_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = UrlRequest(url, headers={"User-Agent": "QuantFactorLab/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def load_derivatives_snapshot(config: dict[str, Any], request: DataRequest) -> pd.DataFrame:
    derivatives_config = config.get("derivatives", {})
    if derivatives_config.get("enabled") is False:
        return pd.DataFrame(columns=DERIVATIVES_COLUMNS)
    data_config = config.get("data", {})
    if str(data_config.get("provider", "")).lower() != "okx":
        return pd.DataFrame(columns=DERIVATIVES_COLUMNS)
    provider = OKXDerivativesSnapshotProvider(timeout=float(derivatives_config.get("timeout", data_config.get("timeout", 15.0))))
    return provider.load(request)


def load_derivatives_history(config: dict[str, Any], request: DataRequest) -> pd.DataFrame:
    derivatives_config = config.get("derivatives", {})
    if derivatives_config.get("enabled") is False or derivatives_config.get("history_enabled") is False:
        return pd.DataFrame(columns=DERIVATIVES_HISTORY_COLUMNS)
    data_config = config.get("data", {})
    if str(data_config.get("provider", "")).lower() != "okx":
        return pd.DataFrame(columns=DERIVATIVES_HISTORY_COLUMNS)
    provider = OKXDerivativesHistoryProvider(
        timeout=float(derivatives_config.get("timeout", data_config.get("timeout", 15.0))),
        limit=int(derivatives_config.get("history_limit", 100)),
        period=str(derivatives_config.get("history_period", "1D")),
    )
    return provider.load(request)


def _snapshot_row(
    symbol: str,
    inst_id: str,
    funding: dict[str, Any],
    open_interest: dict[str, Any],
) -> dict[str, Any]:
    funding_rate = _safe_float(funding.get("fundingRate"))
    annualized = funding_rate * 3 * 365 if funding_rate is not None else None
    premium = _safe_float(funding.get("premium"))
    interest_rate = _safe_float(funding.get("interestRate"))
    oi_usd = _safe_float(open_interest.get("oiUsd"))
    timestamp_ms = _max_timestamp(funding.get("ts"), open_interest.get("ts"))
    crowding, label = _crowding(annualized)
    return {
        "timestamp": _timestamp(timestamp_ms),
        "symbol": symbol,
        "exchange": "okx",
        "inst_id": inst_id,
        "inst_type": funding.get("instType") or open_interest.get("instType") or "SWAP",
        "funding_rate": funding_rate,
        "annualized_funding_rate": annualized,
        "premium": premium,
        "interest_rate": interest_rate,
        "next_funding_time": _timestamp(_safe_int(funding.get("nextFundingTime"))),
        "oi_contracts": _safe_float(open_interest.get("oi")),
        "oi_ccy": _safe_float(open_interest.get("oiCcy")),
        "oi_usd": oi_usd,
        "crowding": crowding,
        "crowding_label": label,
        "interpretation": _interpretation(label, annualized, oi_usd),
    }


def _funding_history_frame(symbol: str, inst_id: str, payload: dict[str, Any]) -> pd.DataFrame:
    _raise_for_okx_error(payload, "funding history")
    rows = []
    for item in payload.get("data") or []:
        funding_rate = _safe_float(item.get("fundingRate"))
        rows.append(
            {
                "timestamp": _timestamp(_safe_int(item.get("fundingTime"))),
                "symbol": symbol,
                "exchange": "okx",
                "inst_id": inst_id,
                "inst_type": item.get("instType") or "SWAP",
                "funding_rate": funding_rate,
                "realized_rate": _safe_float(item.get("realizedRate")),
                "annualized_funding_rate": funding_rate * 3 * 365 if funding_rate is not None else None,
            }
        )
    return pd.DataFrame(rows)


def _open_interest_history_frame(symbol: str, inst_id: str, payload: dict[str, Any]) -> pd.DataFrame:
    _raise_for_okx_error(payload, "open interest history")
    rows = []
    for item in payload.get("data") or []:
        if len(item) < 4:
            continue
        rows.append(
            {
                "timestamp": _timestamp(_safe_int(item[0])),
                "symbol": symbol,
                "exchange": "okx",
                "inst_id": inst_id,
                "inst_type": "SWAP",
                "oi_contracts": _safe_float(item[1]),
                "oi_ccy": _safe_float(item[2]),
                "oi_usd": _safe_float(item[3]),
            }
        )
    return pd.DataFrame(rows)


def _long_short_ratio_frame(symbol: str, inst_id: str, payload: dict[str, Any]) -> pd.DataFrame:
    _raise_for_okx_error(payload, "long short ratio")
    rows = []
    for item in payload.get("data") or []:
        if len(item) < 2:
            continue
        rows.append(
            {
                "timestamp": _timestamp(_safe_int(item[0])),
                "symbol": symbol,
                "exchange": "okx",
                "inst_id": inst_id,
                "inst_type": "SWAP",
                "long_short_ratio": _safe_float(item[1]),
            }
        )
    return pd.DataFrame(rows)


def _raise_for_okx_error(payload: dict[str, Any], label: str) -> None:
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX {label} error {payload.get('code')}: {payload.get('msg')}")


def _crowding(annualized_funding_rate: float | None) -> tuple[str, str]:
    if annualized_funding_rate is None:
        return "UNKNOWN", "未知"
    if annualized_funding_rate >= 0.25:
        return "LONG_CROWDED", "多头拥挤"
    if annualized_funding_rate >= 0.1:
        return "LONG_WARM", "多头偏热"
    if annualized_funding_rate <= -0.25:
        return "SHORT_CROWDED", "空头拥挤"
    if annualized_funding_rate <= -0.1:
        return "SHORT_WARM", "空头偏热"
    return "NEUTRAL", "中性"


def _interpretation(label: str, annualized_funding_rate: float | None, oi_usd: float | None) -> str:
    funding_text = "暂无资金费率" if annualized_funding_rate is None else f"资金费率年化约{annualized_funding_rate:.1%}"
    oi_text = "OI暂无" if oi_usd is None else f"OI约{oi_usd / 1_000_000_000:.2f}B美元"
    if label in {"多头拥挤", "多头偏热"}:
        return f"{label}；{funding_text}，{oi_text}，上涨趋势中需警惕多头过度拥挤。"
    if label in {"空头拥挤", "空头偏热"}:
        return f"{label}；{funding_text}，{oi_text}，下跌趋势中需警惕空头回补。"
    return f"{label}；{funding_text}，{oi_text}，杠杆拥挤度暂不极端。"


def _max_timestamp(*values: Any) -> int | None:
    timestamps = [_safe_int(value) for value in values]
    timestamps = [value for value in timestamps if value is not None]
    return max(timestamps) if timestamps else None


def _timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    return str(pd.to_datetime(value, unit="ms"))


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None
