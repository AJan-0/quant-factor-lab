# Spec: Quant Factor Lab

## Objective

Build a personal quant factor discovery system for crypto assets such as BTC and ETH plus US equities. The system must support a complete research pipeline: factor mining, factor evaluation, and strategy backtesting. It should run locally, work without external credentials via synthetic/sample data, and expose clean interfaces for daily and intraday market data.

## Assumptions

1. The first deliverable is a Python research engine, not a web UI.
2. Local CSV and synthetic data are required for reproducible tests.
3. Real data adapters should be optional because API keys, exchange limits, and vendor terms vary. OKX public spot candles are supported without API keys.
4. Daily data is implemented first; intraday is represented in the data model and provider interfaces.
5. Backtests are research backtests, not production execution simulators.

## Tech Stack

- Python 3.11+
- pandas and numpy for time series transforms
- scikit-learn for phase-2 ML factor mining
- unittest for dependency-light tests
- JSON for runnable example configuration

## Commands

```powershell
Run demo: python -m quant_factor_lab run --config examples/demo_config.json
Run tests: python -m unittest discover -s tests
Install deps: pip install -r requirements.txt
```

## Project Structure

```text
quant_factor_lab/  Source package
docs/              Specs and architecture notes
examples/          Runnable configs
tests/             Unit and integration tests
runs/              Generated local research outputs, ignored by git
```

## Core Interfaces

- `OKXDerivativesHistoryProvider.load(request) -> DataFrame`: returns public OKX swap funding-rate, open-interest, and long-short account ratio history for crypto instruments.
- `OKXMicrostructureSnapshotProvider.load(request) -> DataFrame`: returns public OKX spot order-book depth and recent-trade context for crypto instruments.
- `build_source_health_report(config, market_data, derivatives_snapshot, derivatives_history, microstructure_snapshot) -> dict`: reports provider identity, real-data status, freshness, missing contexts, and per-scope source status.
- `MarketDataProvider.load(request) -> DataFrame`: returns normalized long OHLCV rows.
- `OKXDerivativesSnapshotProvider.load(request) -> DataFrame`: returns public OKX swap funding-rate and open-interest snapshots for crypto instruments.
- `build_market_data_quality_report(data) -> dict`: reports duplicate timestamps, missing OHLCV values, invalid price structure, non-positive volume, extreme returns, large gaps, and per-symbol quality status.
- `FactorMiner.mine(data) -> FactorMiningResult`: returns factor columns and candidate metadata.
- `evaluate_factor_panel(data, factor_panel, horizons) -> DataFrame`: ranks factors by predictive metrics.
- `run_rank_backtest(data, factor_panel, factor_name, config) -> BacktestResult`: converts factor ranks into a portfolio return stream.
- `build_decision_cards(market_data, factor_signals, derivatives_snapshot, data_quality, derivatives_history, microstructure_snapshot) -> DataFrame`: converts market, factor, derivatives, order-book/trade, and quality context into trader-readable decision cards.
- `build_factor_signal_radar(factor_panel, factor_backtests) -> DataFrame`: converts latest factor values, historical percentiles, and per-factor backtests into current trader-readable signals.
- `PipelineRunner.run() -> PipelineResult`: orchestrates the full workflow and writes artifacts.
- `AdminApp`: local HTTP API for editing pipeline config, applying data adjustments, launching runs, and reading generated artifacts.

## Data Model

Required market data columns:

```text
timestamp, symbol, open, high, low, close, volume
```

Optional normalized metadata columns:

```text
asset_class, frequency
```

Supported asset classes:

- `crypto`
- `equity`

Supported frequency values:

- `1d`
- `1h`
- `5m`
- `1m`

Supported market data providers:

- `synthetic`: deterministic local data for tests
- `csv`: local OHLCV files
- `yfinance`: Yahoo Finance adapter
- `ccxt`: optional ccxt adapter
- `okx`: native OKX public REST candlestick adapter for crypto spot markets, with optional public swap funding/OI/long-short context and spot order-book/recent-trade snapshots

## Factor Mining Scope

Phase 1 implements operator/formula mining:

- return momentum
- return reversal
- realized volatility
- moving-average distance
- volume z-score
- range position
- Amihud-style liquidity proxy

Phase 2 implements ML-assisted mining:

- trains a random forest on phase-1 operator features
- produces an out-of-sample prediction factor
- records feature importances as candidate metadata

Phase 3 is reserved for:

- walk-forward model refits
- genetic programming or symbolic regression
- intraday microstructure factors
- crypto/equity cross-market interaction factors
- portfolio-level optimizer constraints

## Admin Console Scope

The local admin console is a browser-based management surface served by Python standard library HTTP tooling. It supports:

- editable pipeline configuration
- universe add/edit/remove
- market data adjustment rules
- background pipeline jobs with status polling
- data-source health, data quality, OKX derivatives crowding and history, OKX spot order-book/recent-trade context, factor ranking, per-factor backtest ranking, current factor signal radar, trader decision cards, equity curve, K-line, market preview, run history, and artifact download views

The console is local-only by default at `127.0.0.1`; it is not an authenticated multi-user service.

## Testing Strategy

- Unit tests cover data normalization, factor generation, evaluation, and backtesting.
- Integration test runs the full pipeline with synthetic data and verifies output artifacts.
- Admin integration tests cover config editing, background jobs, run history, security headers, rate limiting, market series, and signal output.
- Network providers are not used in tests.

## Boundaries

- Always: validate provider output at the schema boundary.
- Always: align factor values at time `t` with future returns after `t`.
- Always: write reproducible outputs for each run.
- Ask first: add paid data vendors, broker APIs, or live execution.
- Ask first: introduce a database or service runtime.
- Never: commit API keys, exchange secrets, or downloaded proprietary data.

## Success Criteria

- Demo pipeline runs from a clean checkout.
- Tests pass with standard library unittest.
- The system evaluates multiple candidate factors and writes ranked results.
- The backtester produces portfolio returns, weights, and metrics.
- Daily and intraday frequencies are represented in the architecture even when the demo uses daily data.
