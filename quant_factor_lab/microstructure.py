from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import pandas as pd

from .types import AssetClass, DataRequest, Instrument


MICROSTRUCTURE_COLUMNS = [
    "timestamp",
    "symbol",
    "exchange",
    "inst_id",
    "best_bid",
    "best_ask",
    "mid_price",
    "spread_bps",
    "bid_depth_usd",
    "ask_depth_usd",
    "depth_imbalance",
    "last_trade_price",
    "last_trade_size",
    "last_trade_side",
    "recent_trade_count",
    "buy_trade_count",
    "sell_trade_count",
    "microstructure_label",
    "interpretation",
]


class OKXMicrostructureSnapshotProvider:
    """OKX public REST order-book and recent-trade snapshot for spot instruments."""

    BASE_URL = "https://www.okx.com"
    BOOKS_PATH = "/api/v5/market/books"
    TRADES_PATH = "/api/v5/market/trades"

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        depth_size: int = 50,
        trade_limit: int = 100,
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.depth_size = max(1, min(int(depth_size), 400))
        self.trade_limit = max(1, min(int(trade_limit), 500))
        self._fetch_json = fetch_json or self._default_fetch_json

    def load(self, request: DataRequest) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for instrument in request.universe:
            if instrument.asset_class != AssetClass.CRYPTO:
                continue
            inst_id = okx_spot_inst_id(instrument)
            book_payload = self._fetch_json(self.BOOKS_PATH, {"instId": inst_id, "sz": str(self.depth_size)})
            trades_payload = self._fetch_json(self.TRADES_PATH, {"instId": inst_id, "limit": str(self.trade_limit)})
            rows.append(_snapshot_row(instrument.symbol, inst_id, book_payload, trades_payload))
        if not rows:
            return pd.DataFrame(columns=MICROSTRUCTURE_COLUMNS)
        return pd.DataFrame(rows, columns=MICROSTRUCTURE_COLUMNS)

    def _default_fetch_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = UrlRequest(url, headers={"User-Agent": "QuantFactorLab/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def load_microstructure_snapshot(config: dict[str, Any], request: DataRequest) -> pd.DataFrame:
    micro_config = config.get("microstructure", {})
    if micro_config.get("enabled") is False:
        return pd.DataFrame(columns=MICROSTRUCTURE_COLUMNS)
    data_config = config.get("data", {})
    if str(data_config.get("provider", "")).lower() != "okx":
        return pd.DataFrame(columns=MICROSTRUCTURE_COLUMNS)
    provider = OKXMicrostructureSnapshotProvider(
        timeout=float(micro_config.get("timeout", data_config.get("timeout", 15.0))),
        depth_size=int(micro_config.get("depth_size", 50)),
        trade_limit=int(micro_config.get("trade_limit", 100)),
    )
    return provider.load(request)


def okx_spot_inst_id(instrument: Instrument) -> str:
    normalized = str(instrument.exchange or instrument.symbol).strip().upper().replace("/", "-")
    if normalized.endswith("-USD"):
        return f"{normalized[:-4]}-USDT"
    if "-" in normalized:
        return normalized
    quote = instrument.currency.upper() if instrument.currency else "USDT"
    return f"{normalized}-{'USDT' if quote == 'USD' else quote}"


def _snapshot_row(symbol: str, inst_id: str, book_payload: dict[str, Any], trades_payload: dict[str, Any]) -> dict[str, Any]:
    _raise_for_okx_error(book_payload, "order book")
    _raise_for_okx_error(trades_payload, "trades")
    book = (book_payload.get("data") or [{}])[0]
    bids = _levels(book.get("bids"))
    asks = _levels(book.get("asks"))
    trades = _trade_rows(trades_payload.get("data") or [])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid_price = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread_bps = (best_ask - best_bid) / mid_price * 10_000 if best_bid is not None and best_ask is not None and mid_price else None
    bid_depth_usd = float(sum(price * size for price, size in bids))
    ask_depth_usd = float(sum(price * size for price, size in asks))
    depth_total = bid_depth_usd + ask_depth_usd
    depth_imbalance = (bid_depth_usd - ask_depth_usd) / depth_total if depth_total else None
    last_trade = trades[0] if trades else {}
    buy_count = sum(1 for trade in trades if trade.get("side") == "buy")
    sell_count = sum(1 for trade in trades if trade.get("side") == "sell")
    label = _label(spread_bps, depth_imbalance, buy_count, sell_count)
    return {
        "timestamp": _timestamp(_max_timestamp(book.get("ts"), last_trade.get("ts"))),
        "symbol": symbol,
        "exchange": "okx",
        "inst_id": inst_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": spread_bps,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "depth_imbalance": depth_imbalance,
        "last_trade_price": _safe_float(last_trade.get("px")),
        "last_trade_size": _safe_float(last_trade.get("sz")),
        "last_trade_side": last_trade.get("side"),
        "recent_trade_count": len(trades),
        "buy_trade_count": buy_count,
        "sell_trade_count": sell_count,
        "microstructure_label": label,
        "interpretation": _interpretation(label, spread_bps, bid_depth_usd, ask_depth_usd, depth_imbalance, buy_count, sell_count),
    }


def _levels(raw_levels: Any) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    for level in raw_levels or []:
        if len(level) < 2:
            continue
        price = _safe_float(level[0])
        size = _safe_float(level[1])
        if price is not None and size is not None:
            rows.append((price, size))
    return rows


def _trade_rows(raw_trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"px": item.get("px"), "sz": item.get("sz"), "side": str(item.get("side", "")).lower(), "ts": item.get("ts")}
        for item in raw_trades
    ]


def _label(spread_bps: float | None, imbalance: float | None, buy_count: int, sell_count: int) -> str:
    if spread_bps is None:
        return "盘口未知"
    if spread_bps > 8:
        return "价差偏宽"
    if imbalance is not None and imbalance > 0.2 and buy_count >= sell_count:
        return "买盘占优"
    if imbalance is not None and imbalance < -0.2 and sell_count >= buy_count:
        return "卖盘占优"
    return "盘口均衡"


def _interpretation(
    label: str,
    spread_bps: float | None,
    bid_depth_usd: float,
    ask_depth_usd: float,
    imbalance: float | None,
    buy_count: int,
    sell_count: int,
) -> str:
    spread_text = "价差未知" if spread_bps is None else f"盘口价差约{spread_bps:.2f}bps"
    imbalance_text = "深度倾斜未知" if imbalance is None else f"深度倾斜{imbalance:.1%}"
    return (
        f"{label}；{spread_text}，买侧深度约{bid_depth_usd / 1_000_000:.1f}M美元，"
        f"卖侧深度约{ask_depth_usd / 1_000_000:.1f}M美元，{imbalance_text}，"
        f"近端买单{buy_count}笔、卖单{sell_count}笔。"
    )


def _raise_for_okx_error(payload: dict[str, Any], label: str) -> None:
    if str(payload.get("code")) != "0":
        raise RuntimeError(f"OKX {label} error {payload.get('code')}: {payload.get('msg')}")


def _max_timestamp(*values: Any) -> int | None:
    timestamps = [_safe_int(value) for value in values]
    timestamps = [value for value in timestamps if value is not None]
    return max(timestamps) if timestamps else None


def _timestamp(value: int | None) -> str | None:
    return None if value is None else str(pd.to_datetime(value, unit="ms"))


def _safe_float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(float(value))
    except (TypeError, ValueError):
        return None
