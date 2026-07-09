from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .data import normalize_market_frame


TECHNICAL_INDICATOR_COLUMNS = [
    "timestamp",
    "symbol",
    "ema_20",
    "ema_50",
    "ema_200",
    "sma_20",
    "sma_50",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "bb_mid_20",
    "bb_upper_20",
    "bb_lower_20",
    "bb_percent_b_20",
    "atr_14",
    "atr_percent_14",
    "supertrend_10_3",
    "supertrend_direction_10_3",
    "vwap_20",
    "obv",
    "obv_slope_20",
    "adx_14",
    "plus_di_14",
    "minus_di_14",
    "mfi_14",
    "stoch_rsi_k_14",
    "stoch_rsi_d_14",
    "donchian_high_20",
    "donchian_low_20",
    "donchian_position_20",
]

INDICATOR_STATE_COLUMNS = [
    "timestamp",
    "symbol",
    "last_close",
    "technical_bias",
    "technical_bias_label",
    "technical_score",
    "confidence",
    "trend_state",
    "momentum_state",
    "volatility_state",
    "volume_state",
    "ema_alignment",
    "above_vwap",
    "supertrend_direction",
    "rsi_14",
    "macd_hist",
    "bb_percent_b_20",
    "atr_percent_14",
    "vwap_20",
    "obv_slope_20",
    "adx_14",
    "mfi_14",
    "stoch_rsi_k_14",
    "donchian_position_20",
    "support_level",
    "resistance_level",
    "trigger_long",
    "trigger_short",
    "invalidation_long",
    "invalidation_short",
    "interpretation",
]


def build_technical_indicator_panel(data: pd.DataFrame) -> pd.DataFrame:
    """Compute TradingView-style technical indicators for each symbol."""
    market = normalize_market_frame(data)
    frames: list[pd.DataFrame] = []
    for _, symbol_frame in market.groupby("symbol", sort=False):
        frames.append(_build_symbol_indicators(symbol_frame))
    if not frames:
        return pd.DataFrame(columns=list(market.columns) + TECHNICAL_INDICATOR_COLUMNS[2:])
    return pd.concat(frames, ignore_index=True).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def build_indicator_state_table(technical_panel: pd.DataFrame) -> pd.DataFrame:
    if technical_panel.empty:
        return pd.DataFrame(columns=INDICATOR_STATE_COLUMNS)

    panel = technical_panel.copy()
    panel["timestamp"] = pd.to_datetime(panel["timestamp"], errors="coerce")
    rows: list[dict[str, Any]] = []
    for symbol, symbol_frame in panel.groupby("symbol", sort=True):
        symbol_frame = symbol_frame.sort_values("timestamp")
        valid = symbol_frame.dropna(subset=["close"])
        if valid.empty:
            continue
        latest = valid.iloc[-1]
        previous = valid.iloc[-2] if len(valid) > 1 else latest
        rows.append(_indicator_state(str(symbol), latest, previous))
    if not rows:
        return pd.DataFrame(columns=INDICATOR_STATE_COLUMNS)
    return pd.DataFrame(rows, columns=INDICATOR_STATE_COLUMNS)


def _build_symbol_indicators(symbol_frame: pd.DataFrame) -> pd.DataFrame:
    frame = symbol_frame.sort_values("timestamp").copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)

    frame["ema_20"] = _ema(close, 20)
    frame["ema_50"] = _ema(close, 50)
    frame["ema_200"] = _ema(close, 200)
    frame["sma_20"] = close.rolling(20, min_periods=10).mean()
    frame["sma_50"] = close.rolling(50, min_periods=20).mean()
    frame["rsi_14"] = _rsi(close, 14)

    macd = _ema(close, 12, min_periods=12) - _ema(close, 26, min_periods=26)
    frame["macd"] = macd
    frame["macd_signal"] = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    frame["macd_hist"] = frame["macd"] - frame["macd_signal"]

    bb_mid = close.rolling(20, min_periods=10).mean()
    bb_std = close.rolling(20, min_periods=10).std(ddof=0)
    frame["bb_mid_20"] = bb_mid
    frame["bb_upper_20"] = bb_mid + 2.0 * bb_std
    frame["bb_lower_20"] = bb_mid - 2.0 * bb_std
    frame["bb_percent_b_20"] = (close - frame["bb_lower_20"]) / (frame["bb_upper_20"] - frame["bb_lower_20"]).replace(0, np.nan)

    atr14 = _atr(high, low, close, 14)
    frame["atr_14"] = atr14
    frame["atr_percent_14"] = atr14 / close.replace(0, np.nan)
    supertrend = _supertrend(high, low, close, period=10, multiplier=3.0)
    frame["supertrend_10_3"] = supertrend["line"]
    frame["supertrend_direction_10_3"] = supertrend["direction"]

    typical_price = (high + low + close) / 3.0
    rolling_dollar_volume = (typical_price * volume).rolling(20, min_periods=5).sum()
    rolling_volume = volume.rolling(20, min_periods=5).sum().replace(0, np.nan)
    frame["vwap_20"] = rolling_dollar_volume / rolling_volume
    frame["obv"] = _obv(close, volume)
    frame["obv_slope_20"] = frame["obv"] - frame["obv"].shift(20)

    adx = _adx(high, low, close, 14)
    frame["adx_14"] = adx["adx"]
    frame["plus_di_14"] = adx["plus_di"]
    frame["minus_di_14"] = adx["minus_di"]
    frame["mfi_14"] = _mfi(high, low, close, volume, 14)
    stoch_rsi = _stoch_rsi(frame["rsi_14"], 14)
    frame["stoch_rsi_k_14"] = stoch_rsi["k"]
    frame["stoch_rsi_d_14"] = stoch_rsi["d"]

    frame["donchian_high_20"] = high.rolling(20, min_periods=10).max()
    frame["donchian_low_20"] = low.rolling(20, min_periods=10).min()
    frame["donchian_position_20"] = (close - frame["donchian_low_20"]) / (
        frame["donchian_high_20"] - frame["donchian_low_20"]
    ).replace(0, np.nan)
    return frame


def _indicator_state(symbol: str, latest: pd.Series, previous: pd.Series) -> dict[str, Any]:
    close = _safe_float(latest.get("close"))
    ema20 = _safe_float(latest.get("ema_20"))
    ema50 = _safe_float(latest.get("ema_50"))
    ema200 = _safe_float(latest.get("ema_200"))
    vwap = _safe_float(latest.get("vwap_20"))
    supertrend_direction = _safe_float(latest.get("supertrend_direction_10_3"))
    rsi = _safe_float(latest.get("rsi_14"))
    macd_hist = _safe_float(latest.get("macd_hist"))
    adx = _safe_float(latest.get("adx_14"))
    atr_percent = _safe_float(latest.get("atr_percent_14"))
    bb_percent_b = _safe_float(latest.get("bb_percent_b_20"))
    mfi = _safe_float(latest.get("mfi_14"))
    stoch_k = _safe_float(latest.get("stoch_rsi_k_14"))
    donchian_position = _safe_float(latest.get("donchian_position_20"))
    obv_slope = _safe_float(latest.get("obv_slope_20"))

    score = 0.0
    ema_alignment = _ema_alignment(close, ema20, ema50, ema200)
    if ema_alignment == "BULL_STACK":
        score += 2.0
    elif ema_alignment == "BEAR_STACK":
        score -= 2.0
    elif ema_alignment == "BULL_SETUP":
        score += 1.0
    elif ema_alignment == "BEAR_SETUP":
        score -= 1.0

    if supertrend_direction is not None:
        score += 1.0 if supertrend_direction > 0 else -1.0 if supertrend_direction < 0 else 0.0
    if vwap is not None and close is not None:
        score += 0.75 if close > vwap else -0.75 if close < vwap else 0.0
    if macd_hist is not None:
        score += 0.75 if macd_hist > 0 else -0.75 if macd_hist < 0 else 0.0
    if rsi is not None:
        if 52 <= rsi <= 68:
            score += 0.5
        elif 32 <= rsi <= 48:
            score -= 0.5
        elif rsi >= 78:
            score -= 0.35
        elif rsi <= 22:
            score += 0.35
    if donchian_position is not None:
        if donchian_position >= 0.8:
            score += 0.5
        elif donchian_position <= 0.2:
            score -= 0.5
    if obv_slope is not None:
        score += 0.35 if obv_slope > 0 else -0.35 if obv_slope < 0 else 0.0
    if adx is not None and adx >= 25:
        score *= 1.08

    bias, bias_label = _bias_from_score(score)
    confidence = _confidence_from_score(score, adx, atr_percent)
    trend_state = _trend_state(ema_alignment, supertrend_direction, adx)
    momentum_state = _momentum_state(rsi, macd_hist, stoch_k)
    volatility_state = _volatility_state(bb_percent_b, atr_percent)
    volume_state = _volume_state(obv_slope, mfi)
    support_level = _first_finite(latest.get("supertrend_10_3"), vwap, ema20, latest.get("donchian_low_20"))
    resistance_level = _first_finite(latest.get("donchian_high_20"), latest.get("bb_upper_20"), ema20)

    return {
        "timestamp": str(latest.get("timestamp")),
        "symbol": symbol,
        "last_close": close,
        "technical_bias": bias,
        "technical_bias_label": bias_label,
        "technical_score": round(score, 4),
        "confidence": confidence,
        "trend_state": trend_state,
        "momentum_state": momentum_state,
        "volatility_state": volatility_state,
        "volume_state": volume_state,
        "ema_alignment": ema_alignment,
        "above_vwap": bool(vwap is not None and close is not None and close >= vwap),
        "supertrend_direction": int(supertrend_direction) if supertrend_direction in {-1.0, 0.0, 1.0} else None,
        "rsi_14": rsi,
        "macd_hist": macd_hist,
        "bb_percent_b_20": bb_percent_b,
        "atr_percent_14": atr_percent,
        "vwap_20": vwap,
        "obv_slope_20": obv_slope,
        "adx_14": adx,
        "mfi_14": mfi,
        "stoch_rsi_k_14": stoch_k,
        "donchian_position_20": donchian_position,
        "support_level": support_level,
        "resistance_level": resistance_level,
        "trigger_long": _ZH_TRIGGER_LONG,
        "trigger_short": _ZH_TRIGGER_SHORT,
        "invalidation_long": _ZH_INVALIDATION_LONG,
        "invalidation_short": _ZH_INVALIDATION_SHORT,
        "interpretation": _interpretation(ema_alignment, supertrend_direction, macd_hist, rsi, vwap, close),
    }


def _ema(series: pd.Series, span: int, min_periods: int | None = None) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=min_periods or max(2, span // 2)).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous_close = close.shift(1)
    ranges = pd.concat([(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1)
    return ranges.max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return _true_range(high, low, close).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int, multiplier: float) -> pd.DataFrame:
    atr = _atr(high, low, close, period)
    hl2 = (high + low) / 2.0
    upper_basic = hl2 + multiplier * atr
    lower_basic = hl2 - multiplier * atr
    final_upper = np.full(len(close), np.nan)
    final_lower = np.full(len(close), np.nan)
    direction = np.zeros(len(close), dtype=float)
    line = np.full(len(close), np.nan)

    for index in range(len(close)):
        if not np.isfinite(upper_basic.iloc[index]) or not np.isfinite(lower_basic.iloc[index]):
            continue
        if index == 0 or not np.isfinite(final_upper[index - 1]) or not np.isfinite(final_lower[index - 1]):
            final_upper[index] = upper_basic.iloc[index]
            final_lower[index] = lower_basic.iloc[index]
            direction[index] = 1.0 if close.iloc[index] >= lower_basic.iloc[index] else -1.0
        else:
            previous_close = close.iloc[index - 1]
            final_upper[index] = upper_basic.iloc[index] if upper_basic.iloc[index] < final_upper[index - 1] or previous_close > final_upper[index - 1] else final_upper[index - 1]
            final_lower[index] = lower_basic.iloc[index] if lower_basic.iloc[index] > final_lower[index - 1] or previous_close < final_lower[index - 1] else final_lower[index - 1]
            previous_direction = direction[index - 1] if direction[index - 1] != 0 else 1.0
            if previous_direction < 0 and close.iloc[index] > final_upper[index]:
                direction[index] = 1.0
            elif previous_direction > 0 and close.iloc[index] < final_lower[index]:
                direction[index] = -1.0
            else:
                direction[index] = previous_direction
        line[index] = final_lower[index] if direction[index] > 0 else final_upper[index]

    return pd.DataFrame({"line": line, "direction": direction}, index=close.index)


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff()).fillna(0.0)
    return (sign * volume).cumsum()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> dict[str, pd.Series]:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index)
    atr = _atr(high, low, close, period).replace(0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def _mfi(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int) -> pd.Series:
    typical_price = (high + low + close) / 3.0
    raw_money_flow = typical_price * volume
    positive_flow = pd.Series(np.where(typical_price.diff() > 0, raw_money_flow, 0.0), index=high.index)
    negative_flow = pd.Series(np.where(typical_price.diff() < 0, raw_money_flow, 0.0), index=high.index)
    ratio = positive_flow.rolling(period, min_periods=period).sum() / negative_flow.rolling(period, min_periods=period).sum().replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + ratio)


def _stoch_rsi(rsi: pd.Series, period: int) -> dict[str, pd.Series]:
    lowest = rsi.rolling(period, min_periods=period).min()
    highest = rsi.rolling(period, min_periods=period).max()
    raw = (rsi - lowest) / (highest - lowest).replace(0, np.nan)
    k = raw.rolling(3, min_periods=1).mean()
    d = k.rolling(3, min_periods=1).mean()
    return {"k": k, "d": d}


def _ema_alignment(close: float | None, ema20: float | None, ema50: float | None, ema200: float | None) -> str:
    if close is None or ema20 is None or ema50 is None:
        return "UNKNOWN"
    if ema200 is not None and close > ema20 > ema50 > ema200:
        return "BULL_STACK"
    if ema200 is not None and close < ema20 < ema50 < ema200:
        return "BEAR_STACK"
    if close > ema20 > ema50:
        return "BULL_SETUP"
    if close < ema20 < ema50:
        return "BEAR_SETUP"
    return "MIXED"


def _bias_from_score(score: float) -> tuple[str, str]:
    if score >= 3.0:
        return "LEAN_LONG", _ZH_LEAN_LONG
    if score >= 1.5:
        return "WATCH_LONG", _ZH_WATCH_LONG
    if score <= -3.0:
        return "LEAN_SHORT", _ZH_LEAN_SHORT
    if score <= -1.5:
        return "WATCH_SHORT", _ZH_WATCH_SHORT
    return "NEUTRAL", _ZH_NEUTRAL


def _confidence_from_score(score: float, adx: float | None, atr_percent: float | None) -> float:
    base = min(abs(score) / 5.25, 1.0)
    if adx is not None:
        base += min(max((adx - 15.0) / 40.0, 0.0), 0.2)
    if atr_percent is not None and atr_percent > 0.12:
        base *= 0.85
    return float(min(max(base, 0.0), 1.0))


def _trend_state(ema_alignment: str, supertrend_direction: float | None, adx: float | None) -> str:
    strength = _ZH_TREND_STRONG if adx is not None and adx >= 25 else _ZH_TREND_WEAK
    if ema_alignment in {"BULL_STACK", "BULL_SETUP"} and (supertrend_direction is None or supertrend_direction >= 0):
        return _ZH_TREND_BULL + strength
    if ema_alignment in {"BEAR_STACK", "BEAR_SETUP"} and (supertrend_direction is None or supertrend_direction <= 0):
        return _ZH_TREND_BEAR + strength
    return _ZH_TREND_MIXED


def _momentum_state(rsi: float | None, macd_hist: float | None, stoch_k: float | None) -> str:
    if rsi is not None and rsi >= 72:
        return _ZH_OVERBOUGHT
    if rsi is not None and rsi <= 28:
        return _ZH_OVERSOLD
    if macd_hist is not None and macd_hist > 0 and (stoch_k is None or stoch_k >= 0.5):
        return _ZH_MOMENTUM_UP
    if macd_hist is not None and macd_hist < 0 and (stoch_k is None or stoch_k <= 0.5):
        return _ZH_MOMENTUM_DOWN
    return _ZH_MOMENTUM_NEUTRAL


def _volatility_state(bb_percent_b: float | None, atr_percent: float | None) -> str:
    if atr_percent is not None and atr_percent >= 0.08:
        return _ZH_VOL_HIGH
    if bb_percent_b is not None and (bb_percent_b >= 1.0 or bb_percent_b <= 0.0):
        return _ZH_VOL_BREAKOUT
    return _ZH_VOL_NORMAL


def _volume_state(obv_slope: float | None, mfi: float | None) -> str:
    if obv_slope is not None and obv_slope > 0 and (mfi is None or mfi >= 50):
        return _ZH_VOLUME_CONFIRM
    if obv_slope is not None and obv_slope < 0 and (mfi is None or mfi <= 50):
        return _ZH_VOLUME_DIVERGE
    return _ZH_VOLUME_NEUTRAL


def _interpretation(
    ema_alignment: str,
    supertrend_direction: float | None,
    macd_hist: float | None,
    rsi: float | None,
    vwap: float | None,
    close: float | None,
) -> str:
    ema_label = {
        "BULL_STACK": _ZH_EMA_BULL,
        "BEAR_STACK": _ZH_EMA_BEAR,
        "BULL_SETUP": _ZH_EMA_BULL_SETUP,
        "BEAR_SETUP": _ZH_EMA_BEAR_SETUP,
    }.get(ema_alignment, _ZH_EMA_MIXED)
    st_label = _ZH_BULL if supertrend_direction is not None and supertrend_direction > 0 else _ZH_BEAR if supertrend_direction is not None and supertrend_direction < 0 else _ZH_UNKNOWN
    macd_label = _ZH_MOMENTUM_UP if macd_hist is not None and macd_hist > 0 else _ZH_MOMENTUM_DOWN if macd_hist is not None and macd_hist < 0 else _ZH_UNKNOWN
    rsi_text = _ZH_UNKNOWN if rsi is None else f"{rsi:.1f}"
    vwap_label = _ZH_ABOVE if close is not None and vwap is not None and close >= vwap else _ZH_BELOW if close is not None and vwap is not None else _ZH_UNKNOWN
    return f"EMA{ema_label}\uff0cSuperTrend{st_label}\uff0cMACD{macd_label}\uff0cRSI {rsi_text}\uff0c\u4ef7\u683c{vwap_label}VWAP\u3002"


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _first_finite(*values: Any) -> float | None:
    for value in values:
        result = _safe_float(value)
        if result is not None:
            return result
    return None


_ZH_LEAN_LONG = "\u504f\u591a"
_ZH_WATCH_LONG = "\u504f\u591a\u7b49\u5f85"
_ZH_LEAN_SHORT = "\u504f\u7a7a"
_ZH_WATCH_SHORT = "\u504f\u7a7a\u7b49\u5f85"
_ZH_NEUTRAL = "\u89c2\u671b"
_ZH_BULL = "\u591a\u5934"
_ZH_BEAR = "\u7a7a\u5934"
_ZH_UNKNOWN = "\u672a\u786e\u8ba4"
_ZH_ABOVE = "\u5728\u4e0a\u65b9"
_ZH_BELOW = "\u5728\u4e0b\u65b9"
_ZH_EMA_BULL = "\u591a\u5934\u6392\u5217"
_ZH_EMA_BEAR = "\u7a7a\u5934\u6392\u5217"
_ZH_EMA_BULL_SETUP = "\u591a\u5934\u5efa\u8bbe\u4e2d"
_ZH_EMA_BEAR_SETUP = "\u7a7a\u5934\u5efa\u8bbe\u4e2d"
_ZH_EMA_MIXED = "\u7ed3\u6784\u6df7\u5408"
_ZH_TREND_BULL = "\u8d8b\u52bf\u504f\u591a / "
_ZH_TREND_BEAR = "\u8d8b\u52bf\u504f\u7a7a / "
_ZH_TREND_STRONG = "ADX\u8f83\u5f3a"
_ZH_TREND_WEAK = "ADX\u4e00\u822c"
_ZH_TREND_MIXED = "\u8d8b\u52bf\u4e0d\u4e00\u81f4"
_ZH_OVERBOUGHT = "RSI\u8fc7\u70ed"
_ZH_OVERSOLD = "RSI\u8d85\u5356"
_ZH_MOMENTUM_UP = "\u52a8\u80fd\u5411\u4e0a"
_ZH_MOMENTUM_DOWN = "\u52a8\u80fd\u5411\u4e0b"
_ZH_MOMENTUM_NEUTRAL = "\u52a8\u80fd\u4e2d\u6027"
_ZH_VOL_HIGH = "ATR\u504f\u9ad8"
_ZH_VOL_BREAKOUT = "\u5e03\u6797\u5e26\u5916\u6cbf"
_ZH_VOL_NORMAL = "\u6ce2\u52a8\u5e38\u6001"
_ZH_VOLUME_CONFIRM = "\u91cf\u80fd\u786e\u8ba4"
_ZH_VOLUME_DIVERGE = "\u91cf\u80fd\u80cc\u79bb"
_ZH_VOLUME_NEUTRAL = "\u91cf\u80fd\u4e2d\u6027"
_ZH_TRIGGER_LONG = "\u6536\u76d8\u7ad9\u7a33 EMA20/VWAP\uff0cMACD\u67f1\u7ef4\u6301\u4e3a\u6b63\uff0cSuperTrend\u672a\u7ffb\u7a7a\u3002"
_ZH_TRIGGER_SHORT = "\u6536\u76d8\u8dcc\u7834 EMA20/VWAP\uff0cMACD\u67f1\u7ef4\u6301\u4e3a\u8d1f\uff0cSuperTrend\u672a\u7ffb\u591a\u3002"
_ZH_INVALIDATION_LONG = "\u8dcc\u56de EMA50 \u4e0b\u65b9\u6216 SuperTrend\u7ffb\u7a7a\u65f6\uff0c\u591a\u5934\u5267\u672c\u5931\u6548\u3002"
_ZH_INVALIDATION_SHORT = "\u7ad9\u56de EMA50 \u4e0a\u65b9\u6216 SuperTrend\u7ffb\u591a\u65f6\uff0c\u7a7a\u5934\u5267\u672c\u5931\u6548\u3002"
