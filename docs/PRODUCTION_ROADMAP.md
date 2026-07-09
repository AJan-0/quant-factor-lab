# Production Roadmap

This project is currently a local research workstation. It is useful for personal factor exploration, but it is not yet a production trading, multi-user research, or internet-facing system.

## Largest Gaps

### 1. Data Reliability

Current state:

- Demo can run on OKX public BTC/ETH spot candlesticks without API keys.
- Real adapters include CSV, yfinance, ccxt, a native OKX public REST candlestick provider, OKX public funding-rate/open-interest snapshots, and OKX historical funding, open-interest, and long-short account ratio context.
- The pipeline now writes `source_health.json`/`source_status.csv` to make provider identity, freshness, and missing contexts visible.
- OKX public spot order-book and recent-trade snapshots are captured in `microstructure_snapshot.csv` for short-term liquidity context.
- There is no durable raw-data store, vendor reconciliation, split/dividend handling, or exchange outage handling.

Production target:

- Ingest raw data into a versioned store.
- Track vendor, symbol mapping, timezone, corporate actions, and retrieval timestamp.
- Add data quality checks for missing bars, zero volume, duplicate timestamps, extreme moves, and stale feeds.

### 2. Research Validity

Current state:

- Factor evaluation supports IC, spread, and a simple rank backtest.
- Each evaluated factor now has an independent backtest summary, a current signal radar based on latest factor value, historical percentile, direction, Sharpe, win rate, and confidence, plus trader-readable decision cards that combine market returns, factor signals, derivatives crowding, data quality, risk notes, invalidation conditions, and action hints.
- ML mining uses a single train/test split.
- Backtest assumptions are intentionally simple.

Production target:

- Walk-forward training and evaluation.
- Purged/embargoed cross validation for overlapping labels.
- Realistic transaction cost, slippage, borrow, funding, and market-impact modeling.
- Out-of-sample experiment tracking and factor decay monitoring.

### 3. Runtime Architecture

Current state:

- The admin console uses Python standard library HTTP server.
- `/api/run` is synchronous and long-running.
- Artifacts are CSV/JSON files under `runs/`.

Production target:

- Background job queue with progress, cancellation, retry, and logs.
- Persistent database for configs, runs, factors, metrics, and audit events.
- Separate API, worker, and static frontend processes.
- Idempotent run IDs and reproducible run manifests.

### 4. Security

Current state:

- The admin console is designed for `127.0.0.1` local use.
- No built-in user management.
- Config writes and artifact reads are local-only but still powerful.

Production target:

- Authentication and authorization for every admin endpoint.
- CSRF strategy, security headers, rate limiting, audit logs, and least-privilege file access.
- Secrets managed outside source code.

### 5. Observability and Operations

Current state:

- Tests and health endpoint exist.
- No structured logs, metrics, error tracking, or release process.

Production target:

- Structured logs per request and per run.
- Metrics for latency, error rate, run duration, data quality failures, and factor drift.
- CI quality gates and a rollback plan.

## Optimization Route

### Phase P0: Local Hardening

Goal: make the local admin safer and less brittle without adding heavy infrastructure.

- Add optional bearer-token protection for admin endpoints.
- Add security headers to JSON and static responses.
- Add basic per-client rate limiting.
- Move long-running pipeline execution to background jobs while keeping `/api/run` backward-compatible.
- Add `/api/jobs` and `/api/jobs/{id}` for status polling.
- Document local-production limitations and startup modes.

Acceptance:
- Current partial: OKX derivatives history writes `derivatives_history.csv` with funding rate, open interest, and long-short account ratio context.
- Current partial: `decision_cards.csv` and the Chinese transaction-decision panel translate factor, derivatives history, order-book/trade, and data-quality evidence into trader-readable setups, risks, invalidation conditions, and action hints.

- Existing tests pass.
- New tests cover auth, rate limiting, security headers, and background jobs.
- Frontend can start a run, poll status, and load results when complete.

### Phase P1: Research Data Foundation

Goal: make data reproducible and auditable.

- Introduce SQLite or DuckDB for local metadata and run storage.
- Current partial: admin runs are now persisted to `runs/admin.sqlite3` and exposed through `/api/runs` plus a Chinese run-history panel.
- Current partial: native OKX public candlestick provider maps symbols such as `ETH-USD` to `ETH-USDT` and records `exchange=okx` plus `okx_inst_id` in market data.
- Current partial: OKX derivatives snapshot maps those symbols to swaps such as `ETH-USDT-SWAP` and surfaces funding rate, annualized funding, premium, OI USD, and crowding labels.
- Current partial: OKX microstructure snapshot maps those symbols to spot instruments and surfaces best bid/ask, spread bps, bid/ask depth, depth imbalance, recent buy/sell trade counts, and a plain-language order-book read.
- Current partial: source-health reporting surfaces whether data is real or simulated, latest timestamps, freshness, and missing optional contexts.
- Current partial: `data_quality.json`, `data_quality.csv`, and the Chinese data-quality panel surface missing values, duplicate timestamps, invalid prices, non-positive volume, extreme returns, and suspected missing bars.
- Current partial: `factor_signals.csv` and the Chinese signal-radar panel translate research output into current symbol-level bullish, bearish, or neutral context.
- Persist configs, runs, factors, metrics, and adjustment rules.
- Add data quality reports and blocking thresholds.
- Add raw/processed data versioning.
- Add run manifests with code version, config hash, data snapshot, and dependency versions.

Acceptance:

- Every run can be reproduced from a manifest.
- Data quality failures are visible in the admin console.
- CSV artifacts become exports, not the source of truth.

### Phase P2: Research Correctness

Goal: reduce false discoveries and backtest overstatement.

- Add walk-forward model refits.
- Add purged time-series validation.
- Add slippage, funding, borrow, and fee models by asset class.
- Add factor neutralization, correlation clustering, and decay monitoring.
