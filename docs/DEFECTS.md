# 当前缺陷与改进清单

## 已在本轮修复

- 管理台主要可见文案已中文化。
- 后台管理 API 新增 `/api/market`，支持按标的读取 OHLCV 序列。
- 新增 K 线面板，包含蜡烛图、成交量柱、行情摘要和 OHLCV 表格。
- 管理台相关校验错误改为中文。
- 新增后台任务接口 `/api/jobs`、运行历史 `/api/runs`、安全响应头、可选 bearer token 和基础限流。
- 新增 OKX 原生公开 K 线数据源，可用 `provider=okx` 获取 BTC/ETH 真实现货 OHLCV。
- 新增 OKX 永续衍生品快照，展示资金费率、年化资金费率、Premium、OI USD 和拥挤度。
- 新增数据质量报告和数据质量面板，展示重复时间戳、缺失值、价格结构异常、非正成交量、极端收益和疑似缺失 K 线。
- 新增逐因子回测排行，展示每个因子的收益、Sharpe、回撤、胜率和方向。
- 新增当前因子信号雷达，将最新因子值、历史分位、逐因子回测质量翻译成偏多、偏空或中性信号。
- New: `derivatives_history.csv` now captures OKX historical funding rate, open interest, and long-short account ratio context.
- New: `microstructure_snapshot.csv` captures OKX spot best bid/ask, spread bps, bid/ask depth, depth imbalance, and recent buy/sell trade counts.
- New: `source_health.json` and `source_status.csv` expose real-data status, provider identity, latest timestamps, freshness, and missing context warnings.
- New: `decision_cards.csv` and the transaction-decision panel combine market returns, factor signals, derivatives crowding/history, order-book/trade context, data quality, risk notes, invalidation conditions, and action hints.
- New: `/api/summary` exposes `derivativesHistory` and `decisionCards` directly for the admin UI.
- New: the config UI includes one-click OKX presets for ETH trend research, smoke verification, and deeper research.

## 仍存在的限制

- `/api/run` 为兼容旧调用仍同步执行；前端已改用后台任务，但还缺少可取消任务、分阶段进度和日志流。
- 管理台默认仅绑定 `127.0.0.1`，可选 token 只适合本地加固，暂不适合作为多人或公网服务。
- K 线面板使用本地 Canvas 绘制，支持基础 hover，但还没有缩放、拖拽、指标叠加、画线工具或多周期聚合。
- 当前数据修改以规则形式作用于加载后的 OHLCV 数据，没有原始数据版本管理、审计日志和回滚界面。
- OKX 当前接入的是公开 REST K 线、衍生品快照、历史资金费率、历史持仓量、多空账户比、REST 盘口深度和近期逐笔成交；尚未接入 WebSocket 实时盘口深度、强平流、基差曲线和跨交易所校验。
- 当前信号雷达仍主要依赖已有 OHLCV 因子结果；生产使用前必须继续接入链上流入流出、活跃地址、巨鲸转账等可审计数据源。
- 因子挖掘仍以规则算子和随机森林为主，遗传规划、walk-forward 重训、组合约束优化还在后续阶段。
- 自动浏览器截图验证在当前会话不可用；已用 HTTP、API、测试和 JS 语法检查替代。
