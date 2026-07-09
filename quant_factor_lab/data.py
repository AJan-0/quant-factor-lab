from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import numpy as np
import pandas as pd

from .types import AssetClass, DataFrequency, DataRequest, REQUIRED_MARKET_COLUMNS


class MarketDataProvider(Protocol):
    def load(self, request: DataRequest) -> pd.DataFrame:
        """Return normalized long OHLCV market data."""


def normalize_market_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [col for col in REQUIRED_MARKET_COLUMNS if col not in frame.columns]
    if missing:
        raise ValueError(f"Market data missing required columns: {missing}")

    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=False)
    data["symbol"] = data["symbol"].astype(str)
    for column in ("open", "high", "low", "close", "volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")

    if "asset_class" not in data.columns:
        data["asset_class"] = None
    if "frequency" not in data.columns:
        data["frequency"] = None

    data = data.dropna(subset=["timestamp", "symbol", "open", "high", "low", "close", "volume"])
    data = data.drop_duplicates(subset=["timestamp", "symbol"], keep="last")
    data = data.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    return data


class SyntheticMarketDataProvider:
    """Deterministic OHLCV generator for local research and tests."""

    def __init__(self, seed: int = 42) -> None:
        self.seed = seed

    def load(self, request: DataRequest) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        index = pd.date_range(
            start=request.start,
            end=request.end,
            freq=request.frequency.pandas_freq,
            inclusive="left",
        )
        if len(index) < 90:
            index = pd.date_range(end=request.end, periods=252, freq=request.frequency.pandas_freq)

        latent_market = rng.normal(0.0, self._volatility(request.frequency), size=len(index))
        lagged_market = np.roll(latent_market, 1)
        lagged_market[0] = 0.0
        rows: list[pd.DataFrame] = []

        for position, instrument in enumerate(request.universe):
            symbol_rng = np.random.default_rng(self.seed + position * 101)
            beta = 0.45 + 0.18 * position
            drift = 0.00025 if instrument.asset_class == AssetClass.EQUITY else 0.00035
            idiosyncratic = symbol_rng.normal(0.0, self._volatility(request.frequency) * 0.9, size=len(index))
            autocorr = 0.10 * lagged_market
            returns = drift + beta * latent_market + autocorr + idiosyncratic
            base_price = 100.0 * (1.8 + position) if instrument.asset_class == AssetClass.CRYPTO else 80.0 + 35.0 * position
            close = base_price * np.exp(np.cumsum(returns))
            open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + symbol_rng.normal(0, 0.0015, len(index)))
            spread = np.abs(symbol_rng.normal(0.003, 0.0015, len(index))) * close
            high = np.maximum(open_, close) + spread
            low = np.maximum(0.01, np.minimum(open_, close) - spread)
            volume_base = 1_000_000 if instrument.asset_class == AssetClass.CRYPTO else 5_000_000
            volume = volume_base * (1 + position * 0.25) * symbol_rng.lognormal(mean=0.0, sigma=0.35, size=len(index))

            rows.append(
                pd.DataFrame(
                    {
                        "timestamp": index,
                        "symbol": instrument.symbol,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "asset_class": instrument.asset_class.value,
                        "frequency": request.frequency.value,
                    }
                )
            )

        return normalize_market_frame(pd.concat(rows, ignore_index=True))

    @staticmethod
    def _volatility(frequency: DataFrequency) -> float:
        return {
            DataFrequency.DAILY: 0.012,
            DataFrequency.HOUR_1: 0.004,
            DataFrequency.MINUTE_5: 0.0015,
            DataFrequency.MINUTE_1: 0.0007,
        }[frequency]


class CSVMarketDataProvider:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self, request: DataRequest) -> pd.DataFrame:
        if self.path.is_dir():
            frames = []
            for csv_path in sorted(self.path.glob("*.csv")):
                frame = pd.read_csv(csv_path)
                if "symbol" not in frame.columns:
                    frame["symbol"] = csv_path.stem.upper()
                frames.append(frame)
            if not frames:
                raise FileNotFoundError(f"No CSV files found under {self.path}")
            raw = pd.concat(frames, ignore_index=True)
        else:
            raw = pd.read_csv(self.path)

        data = normalize_market_frame(raw)
        symbols = {instrument.symbol for instrument in request.universe}
        data = data[data["symbol"].isin(symbols)]
        data = data[(data["timestamp"] >= request.start) & (data["timestamp"] < request.end)]
        if "frequency" in data.columns:
            data.loc[data["frequency"].isna(), "frequency"] = request.frequency.value
        return normalize_market_frame(data)


class YFinanceMarketDataProvider:
    def load(self, request: DataRequest) -> pd.DataFrame:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError("Install yfinance to use provider='yfinance'") from exc

        frames: list[pd.DataFrame] = []
        for instrument in request.universe:
            raw = yf.download(
                instrument.symbol,
                start=request.start.strftime("%Y-%m-%d"),
                end=request.end.strftime("%Y-%m-%d"),
                interval=request.frequency.yfinance_interval,
                progress=False,
                auto_adjust=False,
            )
            if raw.empty:
                continue
            raw = raw.reset_index()
            timestamp_col = "Datetime" if "Datetime" in raw.columns else "Date"
            frames.append(
                pd.DataFrame(
                    {
                        "timestamp": raw[timestamp_col],
                        "symbol": instrument.symbol,
                        "open": raw["Open"],
                        "high": raw["High"],
                        "low": raw["Low"],
                        "close": raw["Close"],
                        "volume": raw["Volume"],
                        "asset_class": instrument.asset_class.value,
                        "frequency": request.frequency.value,
                    }
                )
            )
        if not frames:
            raise RuntimeError("yfinance returned no data for the requested universe")
        return normalize_market_frame(pd.concat(frames, ignore_index=True))


class CCXTMarketDataProvider:
    def __init__(self, exchange_id: str = "binance") -> None:
        self.exchange_id = exchange_id

    def load(self, request: DataRequest) -> pd.DataFrame:
        try:
            import ccxt
        except ImportError as exc:
            raise RuntimeError("Install ccxt to use provider='ccxt'") from exc

        exchange_class = getattr(ccxt, self.exchange_id)
        exchange = exchange_class({"enableRateLimit": True})
        since = int(request.start.timestamp() * 1000)
        frames: list[pd.DataFrame] = []
        for instrument in request.universe:
            rows = exchange.fetch_ohlcv(instrument.symbol, timeframe=request.frequency.ccxt_timeframe, since=since)
            frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms")
            frame["symbol"] = instrument.symbol
            frame["asset_class"] = instrument.asset_class.value
            frame["frequency"] = request.frequency.value
            frames.append(frame)
        if not frames:
            raise RuntimeError("ccxt returned no data for the requested universe")
        data = normalize_market_frame(pd.concat(frames, ignore_index=True))
        return data[(data["timestamp"] >= request.start) & (data["timestamp"] < request.end)]


class OKXMarketDataProvider:
    """OKX public REST OHLCV provider for spot crypto candles."""

    BASE_URL = "https://www.okx.com"
    HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        page_limit: int = 300,
        max_pages: int = 30,
        include_unconfirmed: bool = False,
        fetch_json: Callable[[str, dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.page_limit = max(1, min(int(page_limit), 300))
        self.max_pages = max(1, int(max_pages))
        self.include_unconfirmed = include_unconfirmed
        self._fetch_json = fetch_json or self._default_fetch_json

    def load(self, request: DataRequest) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for instrument in request.universe:
            inst_id = self._okx_inst_id(instrument)
            candles = self._fetch_candles(inst_id, request)
            if not candles:
                continue
            frame = self._candles_to_frame(candles, instrument.symbol, inst_id, request.frequency.value)
            frames.append(frame)
            time.sleep(0.05)
        if not frames:
            raise RuntimeError("OKX returned no candles for the requested universe")
        data = normalize_market_frame(pd.concat(frames, ignore_index=True))
        return data[(data["timestamp"] >= request.start) & (data["timestamp"] < request.end)]

    def _fetch_candles(self, inst_id: str, request: DataRequest) -> list[list[str]]:
        end_ms = int(request.end.timestamp() * 1000)
        start_ms = int(request.start.timestamp() * 1000)
        cursor = str(end_ms)
        rows: list[list[str]] = []
        seen: set[str] = set()
        for _ in range(self.max_pages):
            payload = self._fetch_json(
                self.HISTORY_CANDLES_PATH,
                {
                    "instId": inst_id,
                    "bar": self._okx_bar(request.frequency),
                    "after": cursor,
                    "limit": str(self.page_limit),
                },
            )
            if str(payload.get("code")) != "0":
                raise RuntimeError(f"OKX market data error {payload.get('code')}: {payload.get('msg')}")
            batch = payload.get("data") or []
            if not batch:
                break
            oldest_ms = None
            for candle in batch:
                if len(candle) < 9:
                    continue
                timestamp = str(candle[0])
                if timestamp in seen:
                    continue
                seen.add(timestamp)
                candle_ms = int(float(timestamp))
                oldest_ms = candle_ms if oldest_ms is None else min(oldest_ms, candle_ms)
                if start_ms <= candle_ms < end_ms:
                    rows.append(candle)
            if oldest_ms is None or oldest_ms <= start_ms:
                break
            cursor = str(oldest_ms)
            time.sleep(0.05)
        return rows

    def _candles_to_frame(
        self,
        candles: list[list[str]],
        symbol: str,
        inst_id: str,
        frequency: str,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for candle in candles:
            if len(candle) < 9:
                continue
            if not self.include_unconfirmed and str(candle[8]) != "1":
                continue
            rows.append(
                {
                    "timestamp": pd.to_datetime(int(float(candle[0])), unit="ms"),
                    "symbol": symbol,
                    "open": candle[1],
                    "high": candle[2],
                    "low": candle[3],
                    "close": candle[4],
                    "volume": candle[5],
                    "asset_class": "crypto",
                    "frequency": frequency,
                    "exchange": "okx",
                    "okx_inst_id": inst_id,
                    "okx_confirmed": str(candle[8]) == "1",
                }
            )
        return pd.DataFrame(rows)

    def _default_fetch_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = UrlRequest(url, headers={"User-Agent": "QuantFactorLab/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _okx_bar(frequency: DataFrequency) -> str:
        return {
            DataFrequency.DAILY: "1Dutc",
            DataFrequency.HOUR_1: "1H",
            DataFrequency.MINUTE_5: "5m",
            DataFrequency.MINUTE_1: "1m",
        }[frequency]

    @staticmethod
    def _okx_inst_id(instrument: Instrument) -> str:
        raw_symbol = instrument.exchange or instrument.symbol
        normalized = str(raw_symbol).strip().upper().replace("/", "-")
        if normalized.endswith("-USD"):
            return f"{normalized[:-4]}-USDT"
        if "-" in normalized:
            return normalized
        quote = instrument.currency.upper() if instrument.currency else "USDT"
        if quote == "USD":
            quote = "USDT"
        return f"{normalized}-{quote}"


def build_provider(raw_config: dict) -> MarketDataProvider:
    provider = str(raw_config.get("provider", "synthetic")).lower()
    if provider == "synthetic":
        return SyntheticMarketDataProvider(seed=int(raw_config.get("seed", 42)))
    if provider == "csv":
        if "path" not in raw_config:
            raise ValueError("data.path is required when provider='csv'")
        return CSVMarketDataProvider(raw_config["path"])
    if provider == "yfinance":
        return YFinanceMarketDataProvider()
    if provider == "ccxt":
        return CCXTMarketDataProvider(exchange_id=str(raw_config.get("exchange", "binance")))
    if provider == "okx":
        return OKXMarketDataProvider(
            timeout=float(raw_config.get("timeout", 15.0)),
            page_limit=int(raw_config.get("page_limit", 300)),
            max_pages=int(raw_config.get("max_pages", 30)),
            include_unconfirmed=bool(raw_config.get("include_unconfirmed", False)),
        )
    raise ValueError(f"Unknown data provider: {provider}")
