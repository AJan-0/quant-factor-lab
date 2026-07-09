from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .derivatives import OKXDerivativesSnapshotProvider
from .microstructure import okx_spot_inst_id
from .types import AssetClass, DataRequest


OKX_PUBLIC_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


@dataclass
class RealtimeEventStore:
    max_events: int = 500
    status: str = "stopped"
    error: str | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    subscribed_args: list[dict[str, str]] = field(default_factory=list)
    message_count: int = 0
    order_books: dict[str, dict[str, Any]] = field(default_factory=dict)
    trades: deque[dict[str, Any]] = field(default_factory=deque)
    liquidations: deque[dict[str, Any]] = field(default_factory=deque)
    events: deque[dict[str, Any]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.trades = deque(maxlen=self.max_events)
        self.liquidations = deque(maxlen=self.max_events)
        self.events = deque(maxlen=self.max_events)
        self._lock = threading.Lock()

    def mark_starting(self, args: list[dict[str, str]]) -> None:
        with self._lock:
            self.status = "starting"
            self.error = None
            self.started_at = time.time()
            self.stopped_at = None
            self.subscribed_args = args
            self.events.append(_event("INFO", "Realtime service starting"))

    def mark_running(self) -> None:
        with self._lock:
            self.status = "running"
            self.events.append(_event("INFO", "Realtime service connected"))

    def mark_stopped(self, message: str = "Realtime service stopped") -> None:
        with self._lock:
            self.status = "stopped"
            self.stopped_at = time.time()
            self.events.append(_event("INFO", message))

    def mark_error(self, message: str) -> None:
        with self._lock:
            self.status = "error"
            self.error = message
            self.stopped_at = time.time()
            self.events.append(_event("ERROR", message))

    def add_order_book(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.message_count += 1
            self.order_books[str(row.get("inst_id") or row.get("symbol"))] = row

    def add_trade(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.message_count += 1
            self.trades.appendleft(row)

    def add_liquidation(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.message_count += 1
            self.liquidations.appendleft(row)

    def add_event(self, level: str, message: str) -> None:
        with self._lock:
            self.events.append(_event(level, message))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "error": self.error,
                "startedAt": _format_time(self.started_at),
                "stoppedAt": _format_time(self.stopped_at),
                "messageCount": self.message_count,
                "subscribedArgs": list(self.subscribed_args),
                "orderBooks": list(self.order_books.values()),
                "trades": list(self.trades)[:120],
                "liquidations": list(self.liquidations)[:120],
                "events": list(self.events)[-120:],
            }


class OKXRealtimeService:
    def __init__(self, url: str = OKX_PUBLIC_WS_URL, store: RealtimeEventStore | None = None) -> None:
        self.url = url
        self.store = store or RealtimeEventStore()
        self._app: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, request: DataRequest, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config or {}
        args = build_okx_public_subscriptions(request, config)
        if not args:
            self.store.mark_error("No OKX realtime subscriptions were built")
            return self.store.snapshot()
        try:
            import websocket
        except ImportError:
            self.store.mark_error("Missing optional dependency websocket-client; install requirements.txt to enable realtime")
            return self.store.snapshot()

        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.store.snapshot()
            self.store.mark_starting(args)

            def on_open(ws: Any) -> None:
                self.store.mark_running()
                ws.send(json.dumps({"op": "subscribe", "args": args}))

            def on_message(ws: Any, message: str) -> None:
                if message == "pong":
                    self.store.add_event("DEBUG", "pong")
                    return
                self.handle_message(message)

            def on_error(ws: Any, error: Exception) -> None:
                self.store.mark_error(str(error))

            def on_close(ws: Any, status_code: int, message: str) -> None:
                self.store.mark_stopped(f"Realtime service closed: {status_code or ''} {message or ''}".strip())

            self._app = websocket.WebSocketApp(
                self.url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            self._thread = threading.Thread(target=self._run_forever, daemon=True)
            self._thread.start()
        return self.store.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._app is not None:
                self._app.close()
            self.store.mark_stopped()
        return self.store.snapshot()

    def handle_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            self.store.add_event("WARN", f"Unparseable realtime message: {message[:120]}")
            return
        if payload.get("event"):
            level = "ERROR" if payload.get("event") == "error" else "INFO"
            self.store.add_event(level, payload.get("msg") or payload.get("event"))
            return
        arg = payload.get("arg") or {}
        channel = str(arg.get("channel") or "")
        inst_id = str(arg.get("instId") or "")
        for item in payload.get("data") or []:
            if channel in {"books", "books5", "bbo-tbt"}:
                self.store.add_order_book(_book_row(inst_id, item))
            elif channel == "trades":
                self.store.add_trade(_trade_row(inst_id, item))
            elif channel == "liquidation-orders":
                for row in _liquidation_rows(arg, item):
                    self.store.add_liquidation(row)

    def _run_forever(self) -> None:
        assert self._app is not None
        self._app.run_forever(ping_interval=20, ping_timeout=10)


def build_okx_public_subscriptions(request: DataRequest, config: dict[str, Any] | None = None) -> list[dict[str, str]]:
    config = config or {}
    channels = tuple(config.get("channels", ["books5", "trades"]))
    include_liquidations = bool(config.get("liquidations_enabled", True))
    args: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for instrument in request.universe:
        if instrument.asset_class != AssetClass.CRYPTO:
            continue
        spot_inst_id = okx_spot_inst_id(instrument)
        for channel in channels:
            channel_name = str(channel)
            key = (channel_name, spot_inst_id)
            if key not in seen:
                args.append({"channel": channel_name, "instId": spot_inst_id})
                seen.add(key)
        if include_liquidations:
            swap_inst_id = OKXDerivativesSnapshotProvider.okx_swap_inst_id(instrument)
            key = ("liquidation-orders", swap_inst_id)
            if key not in seen:
                args.append({"channel": "liquidation-orders", "instType": "SWAP", "instId": swap_inst_id})
                seen.add(key)
    return args


def _book_row(inst_id: str, item: dict[str, Any]) -> dict[str, Any]:
    bids = _levels(item.get("bids"))
    asks = _levels(item.get("asks"))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread_bps = (best_ask - best_bid) / mid * 10_000 if mid else None
    bid_depth = sum(price * size for price, size in bids)
    ask_depth = sum(price * size for price, size in asks)
    return {
        "timestamp": _timestamp(item.get("ts")),
        "inst_id": inst_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid,
        "spread_bps": spread_bps,
        "bid_depth_usd": bid_depth,
        "ask_depth_usd": ask_depth,
        "seq_id": item.get("seqId"),
    }


def _trade_row(inst_id: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": _timestamp(item.get("ts")),
        "inst_id": inst_id,
        "price": _safe_float(item.get("px")),
        "size": _safe_float(item.get("sz")),
        "side": item.get("side"),
        "trade_id": item.get("tradeId"),
    }


def _liquidation_rows(arg: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    details = item.get("details") if isinstance(item.get("details"), list) else [item]
    rows = []
    for detail in details:
        inst_id = detail.get("instId") or item.get("instId") or arg.get("instId")
        rows.append(
            {
                "timestamp": _timestamp(detail.get("ts") or item.get("ts")),
                "inst_id": inst_id,
                "inst_type": detail.get("instType") or item.get("instType") or arg.get("instType"),
                "side": detail.get("side") or item.get("side"),
                "size": _safe_float(detail.get("sz") or item.get("sz")),
                "bankruptcy_price": _safe_float(detail.get("bkPx") or item.get("bkPx")),
            }
        )
    return rows


def _levels(raw_levels: Any) -> list[tuple[float, float]]:
    rows = []
    for level in raw_levels or []:
        if len(level) < 2:
            continue
        price = _safe_float(level[0])
        size = _safe_float(level[1])
        if price is not None and size is not None:
            rows.append((price, size))
    return rows


def _event(level: str, message: str) -> dict[str, Any]:
    return {"timestamp": _format_time(time.time()), "level": level.upper(), "message": message}


def _timestamp(value: Any) -> str | None:
    number = _safe_float(value)
    return None if number is None else str(pd.to_datetime(int(number), unit="ms"))


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_time(value: float | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
