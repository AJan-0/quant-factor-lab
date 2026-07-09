from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import pandas as pd


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    EQUITY = "equity"

    @classmethod
    def parse(cls, value: str | "AssetClass") -> "AssetClass":
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        return cls(normalized)


class DataFrequency(str, Enum):
    DAILY = "1d"
    HOUR_1 = "1h"
    MINUTE_5 = "5m"
    MINUTE_1 = "1m"

    @classmethod
    def parse(cls, value: str | "DataFrequency") -> "DataFrequency":
        if isinstance(value, cls):
            return value
        aliases = {
            "daily": cls.DAILY,
            "day": cls.DAILY,
            "d": cls.DAILY,
            "hourly": cls.HOUR_1,
            "hour": cls.HOUR_1,
            "h": cls.HOUR_1,
            "5min": cls.MINUTE_5,
            "5minute": cls.MINUTE_5,
            "1min": cls.MINUTE_1,
            "minute": cls.MINUTE_1,
        }
        normalized = str(value).strip().lower()
        if normalized in aliases:
            return aliases[normalized]
        return cls(normalized)

    @property
    def pandas_freq(self) -> str:
        return {
            self.DAILY: "D",
            self.HOUR_1: "h",
            self.MINUTE_5: "5min",
            self.MINUTE_1: "min",
        }[self]

    @property
    def yfinance_interval(self) -> str:
        return {
            self.DAILY: "1d",
            self.HOUR_1: "1h",
            self.MINUTE_5: "5m",
            self.MINUTE_1: "1m",
        }[self]

    @property
    def ccxt_timeframe(self) -> str:
        return {
            self.DAILY: "1d",
            self.HOUR_1: "1h",
            self.MINUTE_5: "5m",
            self.MINUTE_1: "1m",
        }[self]


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: AssetClass
    exchange: str | None = None
    currency: str = "USD"

    @classmethod
    def from_config(cls, raw: dict[str, Any] | str) -> "Instrument":
        if isinstance(raw, str):
            return cls(symbol=raw, asset_class=AssetClass.EQUITY)
        return cls(
            symbol=str(raw["symbol"]),
            asset_class=AssetClass.parse(raw.get("asset_class", AssetClass.EQUITY)),
            exchange=raw.get("exchange"),
            currency=str(raw.get("currency", "USD")),
        )


@dataclass(frozen=True)
class DataRequest:
    universe: tuple[Instrument, ...]
    start: pd.Timestamp
    end: pd.Timestamp
    frequency: DataFrequency

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "DataRequest":
        universe = tuple(Instrument.from_config(item) for item in raw.get("universe", ()))
        if not universe:
            raise ValueError("data.universe must contain at least one instrument")
        return cls(
            universe=universe,
            start=pd.Timestamp(raw["start"]),
            end=pd.Timestamp(raw["end"]),
            frequency=DataFrequency.parse(raw.get("frequency", "1d")),
        )


REQUIRED_MARKET_COLUMNS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")
KEY_COLUMNS = ("timestamp", "symbol")
