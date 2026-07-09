from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .factors import merge_factor_target
from .types import KEY_COLUMNS


@dataclass(frozen=True)
class BacktestConfig:
    horizon: int = 1
    mode: str = "long_short"
    top_n: int | None = None
    bottom_n: int | None = None
    quantile: float = 0.2
    transaction_cost_bps: float = 0.0
    slippage_bps: float = 0.0
    spread_cost_multiplier: float = 0.0
    funding_enabled: bool = False
    funding_rate_column: str = "annualized_funding_rate"
    funding_cost_multiplier: float = 1.0
    annualization: int = 252
    direction: int = 1

    @classmethod
    def from_config(cls, raw: dict, direction: int = 1) -> "BacktestConfig":
        return cls(
            horizon=int(raw.get("horizon", 1)),
            mode=str(raw.get("mode", "long_short")),
            top_n=raw.get("top_n"),
            bottom_n=raw.get("bottom_n"),
            quantile=float(raw.get("quantile", 0.2)),
            transaction_cost_bps=float(raw.get("transaction_cost_bps", 0.0)),
            slippage_bps=float(raw.get("slippage_bps", 0.0)),
            spread_cost_multiplier=float(raw.get("spread_cost_multiplier", 0.0)),
            funding_enabled=bool(raw.get("funding_enabled", False)),
            funding_rate_column=str(raw.get("funding_rate_column", "annualized_funding_rate")),
            funding_cost_multiplier=float(raw.get("funding_cost_multiplier", 1.0)),
            annualization=int(raw.get("annualization", 252)),
            direction=int(direction),
        )


@dataclass(frozen=True)
class BacktestResult:
    returns: pd.DataFrame
    weights: pd.DataFrame
    metrics: dict[str, float]


def run_rank_backtest(
    data: pd.DataFrame,
    factor_panel: pd.DataFrame,
    factor_name: str,
    config: BacktestConfig,
    microstructure_snapshot: pd.DataFrame | None = None,
    derivatives_history: pd.DataFrame | None = None,
) -> BacktestResult:
    if factor_name not in factor_panel.columns:
        raise ValueError(f"Unknown factor: {factor_name}")
    merged = merge_factor_target(data, factor_panel[list(KEY_COLUMNS) + [factor_name]], config.horizon)
    target_col = f"fwd_return_{config.horizon}"
    merged = merged.dropna(subset=[factor_name, target_col])
    if merged.empty:
        raise ValueError("No valid factor and target rows available for backtest")

    weights = _build_rank_weights(merged, factor_name, config)
    if weights.empty:
        raise ValueError("Rank strategy produced no weights")
    realized = weights.merge(merged[list(KEY_COLUMNS) + [target_col]], on=list(KEY_COLUMNS), how="inner")
    gross_returns = realized.assign(weighted_return=realized["weight"] * realized[target_col]).groupby("timestamp")[
        "weighted_return"
    ].sum()
    execution_costs = _execution_costs(weights, config, microstructure_snapshot)
    funding_costs = _funding_costs(weights, config, derivatives_history)
    net_index = execution_costs.index.union(funding_costs.index).union(gross_returns.index)
    execution_costs = execution_costs.reindex(net_index).fillna(0.0)
    funding_costs = funding_costs.reindex(net_index).fillna(0.0)
    gross_returns = gross_returns.reindex(net_index).fillna(0.0)
    total_costs = execution_costs + funding_costs
    net_returns = gross_returns - total_costs
    returns = pd.DataFrame(
        {
            "timestamp": net_returns.index,
            "gross_return": gross_returns.to_numpy(),
            "transaction_cost": execution_costs.to_numpy(),
            "execution_cost": execution_costs.to_numpy(),
            "funding_cost": funding_costs.to_numpy(),
            "total_cost": total_costs.to_numpy(),
            "net_return": net_returns.to_numpy(),
        }
    )
    returns["equity_curve"] = (1.0 + returns["net_return"]).cumprod()
    metrics = compute_backtest_metrics(returns["net_return"], annualization=config.annualization)
    metrics["average_turnover"] = float(_turnover(weights).mean())
    metrics["average_execution_cost"] = float(execution_costs.mean())
    metrics["average_funding_cost"] = float(funding_costs.mean())
    metrics["periods"] = float(len(returns))
    return BacktestResult(returns=returns, weights=weights, metrics=metrics)


def compute_backtest_metrics(returns: pd.Series, annualization: int = 252) -> dict[str, float]:
    clean = pd.Series(returns).dropna()
    if clean.empty:
        return {
            "total_return": np.nan,
            "annualized_return": np.nan,
            "annualized_volatility": np.nan,
            "sharpe": np.nan,
            "max_drawdown": np.nan,
            "win_rate": np.nan,
        }
    equity = (1.0 + clean).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    years = len(clean) / annualization if annualization > 0 else np.nan
    if years and years > 0 and total_return > -1:
        annualized_return = float((1.0 + total_return) ** (1.0 / years) - 1.0)
    else:
        annualized_return = np.nan
    volatility = float(clean.std(ddof=0) * np.sqrt(annualization))
    sharpe = float(clean.mean() / clean.std(ddof=0) * np.sqrt(annualization)) if clean.std(ddof=0) > 0 else np.nan
    drawdown = equity / equity.cummax() - 1.0
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": float(drawdown.min()),
        "win_rate": float((clean > 0).mean()),
    }


def _build_rank_weights(data: pd.DataFrame, factor_name: str, config: BacktestConfig) -> pd.DataFrame:
    rows: list[dict] = []
    for timestamp, group in data.groupby("timestamp", sort=True):
        ranked = group.copy()
        ranked["_signal"] = ranked[factor_name] * config.direction
        ranked = ranked.sort_values("_signal")
        count = len(ranked)
        if count == 0:
            continue
        if config.mode == "long_only" or count == 1:
            top_n = _resolve_count(config.top_n, config.quantile, count)
            longs = ranked.tail(top_n)
            for _, row in longs.iterrows():
                rows.append({"timestamp": timestamp, "symbol": row["symbol"], "weight": 1.0 / len(longs)})
            continue

        top_n = _resolve_count(config.top_n, config.quantile, count)
        bottom_n = _resolve_count(config.bottom_n, config.quantile, count)
        top_n = min(top_n, count - 1)
        bottom_n = min(bottom_n, count - top_n)
        longs = ranked.tail(top_n)
        shorts = ranked.head(bottom_n)
        if set(longs["symbol"]).intersection(set(shorts["symbol"])):
            continue
        for _, row in longs.iterrows():
            rows.append({"timestamp": timestamp, "symbol": row["symbol"], "weight": 1.0 / len(longs)})
        for _, row in shorts.iterrows():
            rows.append({"timestamp": timestamp, "symbol": row["symbol"], "weight": -1.0 / len(shorts)})
    return pd.DataFrame(rows, columns=["timestamp", "symbol", "weight"])


def _resolve_count(explicit: int | None, quantile: float, available: int) -> int:
    if explicit is not None:
        return max(1, min(int(explicit), available))
    return max(1, min(int(np.ceil(available * quantile)), available))


def _turnover(weights: pd.DataFrame) -> pd.Series:
    wide = weights.pivot_table(index="timestamp", columns="symbol", values="weight", aggfunc="sum").fillna(0.0)
    turnover = wide.diff().abs().sum(axis=1)
    if not turnover.empty:
        turnover.iloc[0] = wide.iloc[0].abs().sum()
    return turnover


def _execution_costs(
    weights: pd.DataFrame,
    config: BacktestConfig,
    microstructure_snapshot: pd.DataFrame | None = None,
) -> pd.Series:
    turnover_by_symbol = _turnover_by_symbol(weights)
    cost_bps = _symbol_execution_cost_bps(weights, config, microstructure_snapshot)
    costs = turnover_by_symbol.copy()
    for symbol in costs.columns:
        costs[symbol] = costs[symbol] * (cost_bps.get(str(symbol), _base_execution_cost_bps(config)) / 10_000.0)
    return costs.sum(axis=1)


def _turnover_by_symbol(weights: pd.DataFrame) -> pd.DataFrame:
    wide = weights.pivot_table(index="timestamp", columns="symbol", values="weight", aggfunc="sum").fillna(0.0)
    turnover = wide.diff().abs()
    if not turnover.empty:
        turnover.iloc[0] = wide.iloc[0].abs()
    return turnover


def _symbol_execution_cost_bps(
    weights: pd.DataFrame,
    config: BacktestConfig,
    microstructure_snapshot: pd.DataFrame | None,
) -> dict[str, float]:
    base = _base_execution_cost_bps(config)
    costs = {str(symbol): base for symbol in weights["symbol"].dropna().astype(str).unique()}
    if microstructure_snapshot is None or microstructure_snapshot.empty or "symbol" not in microstructure_snapshot:
        return costs
    if "spread_bps" not in microstructure_snapshot:
        return costs
    for _, row in microstructure_snapshot.iterrows():
        symbol = str(row.get("symbol"))
        spread = _safe_float(row.get("spread_bps"))
        if symbol and spread is not None:
            costs[symbol] = base + max(spread, 0.0) * config.spread_cost_multiplier
    return costs


def _base_execution_cost_bps(config: BacktestConfig) -> float:
    return max(config.transaction_cost_bps, 0.0) + max(config.slippage_bps, 0.0)


def _funding_costs(
    weights: pd.DataFrame,
    config: BacktestConfig,
    derivatives_history: pd.DataFrame | None,
) -> pd.Series:
    timestamps = pd.Index(sorted(weights["timestamp"].unique()))
    zero_costs = pd.Series(0.0, index=timestamps)
    if not config.funding_enabled or derivatives_history is None or derivatives_history.empty:
        return zero_costs
    required = {"timestamp", "symbol", config.funding_rate_column}
    if not required.issubset(set(derivatives_history.columns)):
        return zero_costs

    rows: list[pd.DataFrame] = []
    weights_frame = weights[["timestamp", "symbol", "weight"]].copy()
    weights_frame["timestamp"] = pd.to_datetime(weights_frame["timestamp"], errors="coerce").astype("datetime64[ns]")
    rates = derivatives_history[["timestamp", "symbol", config.funding_rate_column]].copy()
    rates["timestamp"] = pd.to_datetime(rates["timestamp"], errors="coerce").astype("datetime64[ns]")
    rates[config.funding_rate_column] = pd.to_numeric(rates[config.funding_rate_column], errors="coerce")
    weights_frame = weights_frame.dropna(subset=["timestamp"])
    rates = rates.dropna(subset=["timestamp", config.funding_rate_column])

    for symbol, symbol_weights in weights_frame.groupby("symbol", sort=False):
        symbol_rates = rates[rates["symbol"].astype(str) == str(symbol)].sort_values("timestamp")
        if symbol_rates.empty:
            continue
        merged = pd.merge_asof(
            symbol_weights.sort_values("timestamp"),
            symbol_rates[["timestamp", config.funding_rate_column]],
            on="timestamp",
            direction="backward",
        )
        rows.append(merged)
    if not rows:
        return zero_costs
    priced = pd.concat(rows, ignore_index=True)
    period_rate = priced[config.funding_rate_column] / max(config.annualization, 1)
    priced["funding_cost"] = priced["weight"] * period_rate * config.funding_cost_multiplier
    costs = priced.groupby("timestamp")["funding_cost"].sum()
    costs.index = pd.Index(costs.index)
    zero_costs.index = pd.to_datetime(zero_costs.index, errors="coerce").astype("datetime64[ns]")
    return costs.reindex(zero_costs.index).fillna(0.0)


def _safe_float(value: object) -> float | None:
    try:
        if value in (None, ""):
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) else None
