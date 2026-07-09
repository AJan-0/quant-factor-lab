# Quant Factor Lab

个人量化因子研究与交易决策工作台，面向 Crypto 的 BTC/ETH 和美股市场。当前版本优先服务个人研究与交易复盘，已经具备从真实行情数据、因子挖掘、评估、回测到中文可视化决策面板的完整本地闭环。

The first version runs a complete local pipeline:

1. Load market data for BTC, ETH, and equities.
2. Mine factors with operator/formula candidates and an ML prediction factor.
3. Evaluate factors with forward returns, IC, quantile spread, and stability metrics.
4. Backtest the selected factor with a rank-based long/short portfolio.

The architecture keeps daily and intraday data as first-class frequencies. The included demo now uses OKX public market data for BTC/ETH without API keys; synthetic, CSV, yfinance, and ccxt adapters remain available for local tests and extensions.

## Quick Start / 本地启动

```powershell
python -m quant_factor_lab run --config examples/demo_config.json
python -m quant_factor_lab admin --config examples/demo_config.json --port 8765
python -m unittest discover -s tests
```

The demo writes outputs under `runs/okx-smoke`:

- `market_data.csv`
- `data_quality.json`
- `data_quality.csv`
- `source_health.json`
- `source_status.csv`
- `derivatives_snapshot.csv`
- `derivatives_history.csv`
- `microstructure_snapshot.csv`
- `decision_cards.csv`
- `factor_panel.csv`
- `factor_candidates.csv`
- `factor_evaluation.csv`
- `factor_backtests.csv`
- `factor_signals.csv`
- `backtest_returns.csv`
- `backtest_weights.csv`
- `backtest_metrics.json`
- `summary.json`

Open the admin console at `http://127.0.0.1:8765` after starting the `admin` command. The console lets you edit universe members, data frequency, mining windows, ML settings, evaluation horizons, backtest parameters, and data adjustment rules before running the pipeline. It also includes one-click research presets, a Chinese data-source health panel, data-quality panel, OKX derivatives crowding snapshot and history table, OKX spot order-book/recent-trade snapshot, K-line panel with candlesticks, volume bars, quote summary, an OHLCV table, per-factor backtest rankings, current factor signal radar, and trader-readable decision cards with evidence, risk notes, invalidation conditions, and action hints.

For OKX spot crypto data, set `data.provider` to `okx`. Symbols such as `ETH-USD` and `BTC-USD` are displayed in the system and automatically mapped to OKX spot instruments such as `ETH-USDT` and `BTC-USDT`. When `derivatives.enabled` is true, the same symbols are mapped to OKX swaps such as `ETH-USDT-SWAP` for public funding-rate and open-interest snapshots. When `derivatives.history_enabled` is true, the pipeline also fetches historical funding rate, open interest, and long-short account ratio context. When `microstructure.enabled` is true, the pipeline fetches OKX public spot order-book depth and recent trades. Set `data.include_unconfirmed` to `true` only when you want the latest unfinished candle for monitoring; research backtests should keep it `false`.

For a hardened local session, protect admin API endpoints with a bearer token and keep the server bound to localhost:

```powershell
$env:QUANT_FACTOR_ADMIN_TOKEN="change-me"
python -m quant_factor_lab admin --config examples/demo_config.json --host 127.0.0.1 --port 8765
```

The frontend prompts for the token on the first protected API request. Pipeline runs started from the admin console use background jobs so the page can poll status while the research run is executing.

Admin job metadata is persisted in `runs/admin.sqlite3`. The console exposes a run-history view backed by `/api/runs`, including status, config hash, output directory, summary metrics, and failure errors.

## Web 部署：最快方案

最快可以上线的是 **GitHub Pages 静态快照版**。它会把最新一次研究结果、K线、因子评分、回测排行、决策卡和运行产物导出成纯静态网页，适合手机、平板和桌面浏览，也适合发给自己或团队做复盘。

```powershell
python -m quant_factor_lab run --config examples/demo_config.json
python -m quant_factor_lab export-site --config examples/demo_config.json --site-dir site
python -m http.server 9001 -d site
```

打开 `http://127.0.0.1:9001/` 可以预览即将发布到网页上的版本。静态快照版只负责展示数据，不能在网页上保存配置、启动回测、取消任务或连接 OKX WebSocket 实时流；这些能力需要 Python 后端在线运行。

### 部署到 GitHub Pages

仓库已经包含 `.github/workflows/pages.yml`，推荐使用 GitHub Actions 自动生成并发布静态快照：

1. 在 GitHub 创建一个仓库，并把本项目推送上去。
2. 进入仓库 `Settings -> Pages`。
3. 将 `Build and deployment -> Source` 设置为 `GitHub Actions`。
4. 进入 `Actions`，手动运行 `Deploy GitHub Pages Snapshot`，或等待 push 到 `main` 后自动运行。
5. 部署完成后，GitHub 会给出一个类似 `https://<user>.github.io/<repo>/` 的网页地址。

这条路线最快，因为不需要服务器、数据库或域名配置。代价是它是“发布时的数据快照”，不是实时交易后台。

### 后续生产级网页方案

如果要让公网网页也能直接改参数、启动流水线、查看实时盘口/强平流和任务日志，需要把系统拆成两部分：

- 前端静态资源：继续放在 GitHub Pages、Cloudflare Pages、Vercel 或 Netlify。
- Python API/Worker：部署到 Render、Fly.io、Railway、VPS 或 Kubernetes，负责 OKX 数据、任务队列、回测、WebSocket 和持久化。

前端可以通过 `runtime-config.js` 指向后端：

```js
window.QFL_STATIC_SITE = false;
window.QFL_API_BASE_URL = "https://your-api.example.com";
```

生产级公网部署前，需要补齐鉴权、HTTPS、CORS 白名单、日志监控、任务队列、数据库备份和密钥管理。

## Project Layout

```text
quant_factor_lab/
  admin/       Local HTTP API and static admin console
  data.py        Market data providers and schema normalization
  quality.py     Market data quality checks and reports
  derivatives.py OKX funding-rate, open-interest, and long-short ratio context
  microstructure.py OKX spot order-book and recent-trade context
  source_health.py  Real-data source and freshness reporting
  factors.py     Operator factor generation and forward returns
  mining.py      Operator and ML factor miners
  evaluation.py  IC, quantile spread, and factor ranking
  backtest.py    Rank portfolio backtester
  signals.py     Current factor signal radar generation
  decisions.py   Trader-readable decision cards
  pipeline.py    End-to-end orchestration
  cli.py         Command line entrypoint
docs/
  SPEC.md        System specification and phased roadmap
examples/
  demo_config.json
tests/
  unittest coverage for the core pipeline
```

## Data Schema

All providers normalize to a long OHLCV table:

```text
timestamp, symbol, open, high, low, close, volume, asset_class, frequency
```

CSV files can be a single file or a directory of symbol files. Missing `symbol` values are inferred from the file name.

## Data Adjustments

Pipeline configs can include `data.adjustments` rules. Each rule modifies loaded market rows before factors are mined:

```json
{
  "symbol": "BTC-USD",
  "field": "close",
  "operation": "multiply",
  "value": 1.02,
  "start": "2022-02-01",
  "end": "2022-02-10",
  "note": "scenario shock"
}
```

Supported fields are `open`, `high`, `low`, `close`, and `volume`. Supported operations are `multiply`, `add`, and `set`.

## Phased Factor Mining

- Phase 1: deterministic operator/formula mining for momentum, volatility, liquidity, range, and reversal families.
- Phase 2: ML-assisted mining with out-of-sample prediction factors and feature importance ranking.
- Phase 3: planned walk-forward model refits, genetic programming, cross-market interaction factors, and intraday microstructure features.
