const state = {
  config: null,
  configPath: "",
  snapshot: null,
  market: { symbols: [], selectedSymbol: "", rows: [], limit: 240, hoverIndex: null },
  cockpit: { selectedSymbol: "", template: "trend_follow" },
  signals: { selectedSymbol: "", side: "ALL" },
  realtime: null,
  runs: [],
  activeJobId: null,
  jobLogs: [],
  dirty: false,
  activeView: "overview",
};

const API_BASE_STORAGE_KEY = "qflApiBaseUrl";
const REALTIME_POLL_INTERVAL_MS = 3000;
const REALTIME_RECONNECT_MAX_MS = 30000;
const REALTIME_SOCKET_DISABLE_MS = 60000;

const realtimeSocket = {
  ws: null,
  reconnectTimer: null,
  reconnectDelay: 1000,
  failureCount: 0,
  disabledUntil: 0,
  manualStop: false,
  opened: false,
};

const runtime = (() => {
  const apiBaseUrl = getConfiguredApiBaseUrl();
  return {
    apiBaseUrl,
    staticSite:
      !apiBaseUrl && (
      Boolean(window.QFL_STATIC_SITE) ||
      window.location.protocol === "file:" ||
      window.location.hostname.endsWith("github.io")),
  };
})();

const providers = ["synthetic", "csv", "yfinance", "ccxt", "okx"];
const frequencies = ["1d", "1h", "5m", "1m"];
const modes = ["long_short", "long_only"];
const assetClasses = ["crypto", "equity"];
const labels = {
  synthetic: "合成演示",
  csv: "CSV",
  yfinance: "Yahoo Finance",
  ccxt: "CCXT",
  okx: "OKX真实行情",
  crypto: "Crypto",
  equity: "美股",
  long_short: "多空",
  long_only: "只做多",
};

const indicatorCatalog = {
  ema: { name: "EMA 20/50/200", group: "趋势", role: "判断趋势排列和动态支撑阻力", status: "next" },
  supertrend: { name: "SuperTrend", group: "趋势", role: "识别趋势翻转和移动止损线", status: "next" },
  macd: { name: "MACD", group: "动量", role: "观察趋势动能扩张或衰减", status: "next" },
  adx: { name: "ADX / DMI", group: "趋势强度", role: "判断趋势是否值得跟随", status: "next" },
  rsi: { name: "RSI", group: "动量", role: "识别过热、超卖和背离风险", status: "next" },
  stoch_rsi: { name: "Stoch RSI", group: "动量", role: "捕捉短线动量拐点", status: "next" },
  bollinger: { name: "Bollinger Bands", group: "波动率", role: "判断波动压缩、突破和均值回归", status: "next" },
  atr: { name: "ATR", group: "波动率", role: "估算止损距离和追价风险", status: "next" },
  donchian: { name: "Donchian Channel", group: "突破", role: "识别区间突破和趋势延续", status: "next" },
  vwap: { name: "VWAP", group: "量价", role: "判断机构均价和回踩质量", status: "next" },
  obv: { name: "OBV", group: "量价", role: "验证价格上涨是否有成交量配合", status: "next" },
  mfi: { name: "MFI", group: "量价", role: "用价格和成交量识别资金流强弱", status: "next" },
  volume_zscore: { name: "Volume Z-score", group: "量价", role: "识别放量突破、缩量反弹和异常成交", status: "live" },
  orderbook: { name: "盘口深度倾斜", group: "微观结构", role: "观察短线买卖盘深度是否支持方向", status: "live" },
  cvd: { name: "CVD 主动买卖差", group: "微观结构", role: "识别主动成交是否跟随价格", status: "next" },
  liquidation: { name: "强平流", group: "微观结构", role: "捕捉恐慌尾声和挤仓风险", status: "live" },
  funding: { name: "Funding Rate", group: "衍生品", role: "识别多空拥挤和持仓成本", status: "live" },
  oi: { name: "Open Interest", group: "衍生品", role: "区分现货推动、杠杆追涨和空头回补", status: "live" },
  long_short: { name: "Long/Short Ratio", group: "衍生品", role: "观察账户层面的多空拥挤", status: "live" },
  active_addresses: { name: "Active Addresses", group: "链上", role: "观察网络活跃度是否领先价格", status: "live" },
  tx_count: { name: "Transaction Count", group: "链上", role: "验证链上活动是否扩张", status: "live" },
};

const technicalNumberFields = [
  "ema_20", "ema_50", "ema_200", "sma_20", "sma_50", "rsi_14", "macd", "macd_signal", "macd_hist",
  "bb_mid_20", "bb_upper_20", "bb_lower_20", "bb_percent_b_20", "atr_14", "atr_percent_14",
  "supertrend_10_3", "supertrend_direction_10_3", "vwap_20", "obv", "obv_slope_20", "adx_14",
  "plus_di_14", "minus_di_14", "mfi_14", "stoch_rsi_k_14", "stoch_rsi_d_14", "donchian_high_20",
  "donchian_low_20", "donchian_position_20"
];

const indicatorTemplates = {
  trend_follow: {
    name: "趋势延续模板",
    description: "适合判断 ETH/BTC 是否仍处于顺势结构，避免只看单个动量因子追价。",
    indicators: ["ema", "supertrend", "macd", "adx", "volume_zscore", "funding", "oi"],
  },
  pullback_long: {
    name: "回调接多模板",
    description: "关注趋势未破坏时的回踩质量，重点观察 VWAP/均线、成交量和盘口主动买盘。",
    indicators: ["ema", "vwap", "rsi", "atr", "orderbook", "cvd", "funding"],
  },
  breakout: {
    name: "突破确认模板",
    description: "用于观察波动压缩后的方向选择，突破必须获得成交量、盘口和 OI 的确认。",
    indicators: ["donchian", "bollinger", "atr", "volume_zscore", "orderbook", "oi", "liquidation"],
  },
  crowded_derivatives: {
    name: "合约拥挤模板",
    description: "判断行情是否主要由杠杆推动，避免在 funding/OI 过热时被动追单。",
    indicators: ["funding", "oi", "long_short", "liquidation", "orderbook", "rsi"],
  },
  onchain_lead: {
    name: "链上前瞻模板",
    description: "观察链上活跃度是否先于价格变化，寻找非纯价格共识的早期线索。",
    indicators: ["active_addresses", "tx_count", "volume_zscore", "ema", "oi", "funding"],
  },
};

document.addEventListener("DOMContentLoaded", () => {
  bindNavigation();
  bindActions();
  applyRuntimeMode();
  fillSelect("providerSelect", providers);
  fillSelect("frequencySelect", frequencies);
  fillSelect("modeSelect", modes);
  fillTemplateSelect();
  loadAll();
  window.setInterval(refreshRealtime, REALTIME_POLL_INTERVAL_MS);
});

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setActiveView(button.dataset.view));
  });
}

function bindActions() {
  byId("reloadButton").addEventListener("click", loadAll);
  byId("apiSettingsButton").addEventListener("click", configureApiBaseUrl);
  byId("saveButton").addEventListener("click", saveConfig);
  byId("runButton").addEventListener("click", runPipeline);
  byId("cancelJobButton").addEventListener("click", cancelActiveJob);
  byId("reloadRunsButton").addEventListener("click", refreshRunHistory);
  byId("applyJsonButton").addEventListener("click", applyJsonEditor);
  byId("addInstrumentButton").addEventListener("click", addInstrument);
  byId("startRealtimeButton").addEventListener("click", startRealtime);
  byId("stopRealtimeButton").addEventListener("click", stopRealtime);
  byId("klineSymbolSelect").addEventListener("change", handleKlineControls);
  byId("klineLimitSelect").addEventListener("change", handleKlineControls);
  byId("cockpitSymbolSelect").addEventListener("change", handleCockpitControls);
  byId("cockpitTemplateSelect").addEventListener("change", handleCockpitControls);
  byId("signalSymbolSelect").addEventListener("change", handleSignalControls);
  byId("signalSideSelect").addEventListener("change", handleSignalControls);
  byId("klineCanvas").addEventListener("mousemove", handleKlineHover);
  byId("klineCanvas").addEventListener("mouseleave", () => {
    state.market.hoverIndex = null;
    renderMarketPanel();
  });
  document.querySelectorAll("[data-preset]").forEach((button) => {
    button.addEventListener("click", () => applyPreset(button.dataset.preset));
  });
  document.body.addEventListener("input", handleFieldInput);
  document.body.addEventListener("change", handleFieldInput);
  document.body.addEventListener("click", handleTableActions);
}

async function loadAll() {
  setBusy(true, "加载中");
  try {
    const configPayload = await fetchJson("/api/config");
    state.config = withDefaults(configPayload.config);
    state.configPath = configPayload.configPath;
    state.snapshot = await fetchJson("/api/summary");
    state.realtime = await fetchJson("/api/realtime");
    await loadRunHistory();
    await loadMarketSeries();
    state.dirty = false;
    render();
    maybeStartRealtimeSocket({ silent: true });
    setServerState(runtime.staticSite ? "静态快照" : "已连接", true);
  } catch (error) {
    setServerState("未连接", false);
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function saveConfig() {
  if (runtime.staticSite) {
    toast("静态网页版不能保存配置；部署后端 API 后可启用", true);
    return;
  }
  setBusy(true, "保存中");
  try {
    const payload = await fetchJson("/api/config", {
      method: "PUT",
      body: JSON.stringify({ config: state.config }),
    });
    state.config = withDefaults(payload.config);
    state.configPath = payload.configPath;
    state.dirty = false;
    render();
    toast("配置已保存");
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function runPipeline() {
  if (runtime.staticSite) {
    toast("静态网页版不能启动回测任务；请连接 Python 后端 API", true);
    return;
  }
  setBusy(true, "任务启动中");
  try {
    const payload = await fetchJson("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ config: state.config }),
    });
    state.activeJobId = payload.job.id;
    state.jobLogs = [];
    byId("cancelJobButton").disabled = false;
    toast("后台任务已启动");
    const job = isTerminalJob(payload.job) ? payload.job : await pollJob(payload.job.id);
    if (job.status === "succeeded") {
      state.snapshot = job.result.snapshot;
      await loadMarketSeries();
      toast("流水线运行完成");
    } else if (job.status === "canceled") {
      toast("任务已取消");
    }
    await loadRunHistory();
    render();
  } catch (error) {
    toast(error.message, true);
    await safeRefreshRunHistory();
  } finally {
    byId("cancelJobButton").disabled = true;
    setBusy(false);
  }
}

async function pollJob(jobId) {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const payload = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}`);
    await refreshJobLogs(jobId);
    const job = payload.job;
    if (job.status === "succeeded" || job.status === "failed" || job.status === "canceled") {
      if (job.status === "failed") throw new Error(job.error || "任务运行失败");
      return job;
    }
    setServerState(`运行中 ${attempt + 1}`, true);
    await sleep(1200);
  }
  throw new Error("任务等待超时");
}

function isTerminalJob(job) {
  return job && ["succeeded", "failed", "canceled"].includes(job.status);
}

async function cancelActiveJob() {
  if (runtime.staticSite) {
    toast("静态网页版没有后台任务可取消", true);
    return;
  }
  if (!state.activeJobId) return;
  try {
    await fetchJson(`/api/jobs/${encodeURIComponent(state.activeJobId)}/cancel`, { method: "POST", body: "{}" });
    toast("已请求取消任务");
  } catch (error) {
    toast(error.message, true);
  }
}

async function refreshJobLogs(jobId = state.activeJobId) {
  if (runtime.staticSite) return;
  if (!jobId) return;
  const lastId = state.jobLogs.length ? state.jobLogs[state.jobLogs.length - 1].id : null;
  const suffix = lastId ? `?afterId=${encodeURIComponent(lastId)}` : "";
  const payload = await fetchJson(`/api/jobs/${encodeURIComponent(jobId)}/logs${suffix}`);
  state.jobLogs.push(...(payload.logs || []));
  renderJobLogs();
}

async function refreshRealtime() {
  if (isRealtimeSocketActive() || realtimeSocket.reconnectTimer) return;
  try {
    const payload = await fetchJson("/api/realtime");
    payload.transport ||= "rest";
    state.realtime = payload;
    renderRealtime();
  } catch (_) {
  }
}

async function startRealtime() {
  if (runtime.staticSite) {
    toast("静态网页版不能连接实时流；请部署后端 WebSocket 服务", true);
    return;
  }
  if (connectRealtimeSocket({ force: true })) {
    toast("WebSocket 实时流连接中");
    return;
  }
  try {
    const payload = await fetchJson("/api/realtime/start", { method: "POST", body: "{}" });
    payload.transport ||= "rest";
    state.realtime = payload;
    renderRealtime();
    toast("实时流启动请求已发送");
  } catch (error) {
    toast(error.message, true);
  }
}

async function stopRealtime() {
  if (runtime.staticSite) {
    toast("静态网页版没有实时流服务", true);
    return;
  }
  if (disconnectRealtimeSocket()) {
    state.realtime = realtimeWithEvent("INFO", "WebSocket 实时流已停止", {
      status: "stopped",
      stoppedAt: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
      transport: "websocket",
    });
    renderRealtime();
    toast("实时流已停止");
    return;
  }
  try {
    const payload = await fetchJson("/api/realtime/stop", { method: "POST", body: "{}" });
    payload.transport ||= "rest";
    state.realtime = payload;
    renderRealtime();
    toast("实时流已停止");
  } catch (error) {
    toast(error.message, true);
  }
}

function maybeStartRealtimeSocket(options = {}) {
  if (state.config?.realtime?.enabled === false) return false;
  if (isRealtimeSocketActive() || realtimeSocket.reconnectTimer) return true;
  if (!options.force && !shouldAutoStartRealtimeSocket()) return false;
  return connectRealtimeSocket(options);
}

function connectRealtimeSocket({ silent = false, force = false } = {}) {
  if (!supportsRealtimeSocket()) return false;
  if (!force && Date.now() < realtimeSocket.disabledUntil) return false;
  if (isRealtimeSocketActive()) return true;

  const socketUrl = realtimeSocketUrl();
  if (!socketUrl) return false;

  realtimeSocket.manualStop = false;
  realtimeSocket.opened = false;
  realtimeSocket.disabledUntil = force ? 0 : realtimeSocket.disabledUntil;
  window.clearTimeout(realtimeSocket.reconnectTimer);
  realtimeSocket.reconnectTimer = null;

  state.realtime = realtimeWithEvent("INFO", "WebSocket 实时流连接中", {
    status: "starting",
    error: null,
    transport: "websocket",
  });
  renderRealtime();

  const ws = new WebSocket(socketUrl);
  realtimeSocket.ws = ws;

  ws.addEventListener("open", () => {
    if (realtimeSocket.ws !== ws) return;
    realtimeSocket.opened = true;
    realtimeSocket.failureCount = 0;
    realtimeSocket.reconnectDelay = 1000;
    state.realtime = realtimeWithEvent("INFO", "WebSocket 已连接", {
      status: "running",
      error: null,
      transport: "websocket",
    });
    renderRealtime();
  });

  ws.addEventListener("message", (event) => {
    if (realtimeSocket.ws !== ws) return;
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    if (["snapshot", "realtime", "heartbeat"].includes(message.type) && message.payload) {
      message.payload.transport = "websocket";
      state.realtime = message.payload;
      renderRealtime();
    }
  });

  ws.addEventListener("close", () => {
    if (realtimeSocket.ws !== ws) return;
    realtimeSocket.ws = null;
    if (realtimeSocket.manualStop) return;
    const failedBeforeOpen = !realtimeSocket.opened;
    if (failedBeforeOpen) realtimeSocket.failureCount += 1;
    if ((silent && failedBeforeOpen && !force) || realtimeSocket.failureCount >= 3) {
      fallbackRealtimeToRest("WebSocket 暂不可用，临时切换 REST 轮询");
      return;
    }
    scheduleRealtimeReconnect();
  });

  ws.addEventListener("error", () => {
    if (realtimeSocket.ws !== ws) return;
    state.realtime = realtimeWithEvent("WARN", "WebSocket 连接异常，等待重连", {
      status: "starting",
      transport: "websocket",
    });
    if (!silent) renderRealtime();
  });

  return true;
}

function fallbackRealtimeToRest(message) {
  realtimeSocket.disabledUntil = Date.now() + REALTIME_SOCKET_DISABLE_MS;
  realtimeSocket.failureCount = 0;
  realtimeSocket.reconnectDelay = 1000;
  state.realtime = realtimeWithEvent("WARN", message, {
    status: "running",
    error: null,
    transport: "rest",
  });
  renderRealtime();
  refreshRealtime();
}

function disconnectRealtimeSocket() {
  const hadSocket = Boolean(realtimeSocket.ws || realtimeSocket.reconnectTimer);
  realtimeSocket.manualStop = true;
  window.clearTimeout(realtimeSocket.reconnectTimer);
  realtimeSocket.reconnectTimer = null;
  realtimeSocket.disabledUntil = 0;
  realtimeSocket.failureCount = 0;
  realtimeSocket.reconnectDelay = 1000;
  const ws = realtimeSocket.ws;
  realtimeSocket.ws = null;
  if (ws && ws.readyState < WebSocket.CLOSING) ws.close(1000, "Stopped by user");
  return hadSocket;
}

function scheduleRealtimeReconnect() {
  if (realtimeSocket.manualStop || realtimeSocket.reconnectTimer) return;
  const delay = realtimeSocket.reconnectDelay;
  realtimeSocket.reconnectDelay = Math.min(realtimeSocket.reconnectDelay * 2, REALTIME_RECONNECT_MAX_MS);
  state.realtime = realtimeWithEvent("WARN", `WebSocket 已断开，${Math.ceil(delay / 1000)} 秒后重连`, {
    status: "starting",
    transport: "websocket",
  });
  renderRealtime();
  realtimeSocket.reconnectTimer = window.setTimeout(() => {
    realtimeSocket.reconnectTimer = null;
    connectRealtimeSocket({ silent: true });
  }, delay);
}

function supportsRealtimeSocket() {
  return !runtime.staticSite && "WebSocket" in window;
}

function shouldAutoStartRealtimeSocket() {
  if (runtime.staticSite) return false;
  if (runtime.apiBaseUrl) return true;
  return !["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
}

function isRealtimeSocketActive() {
  const ws = realtimeSocket.ws;
  return Boolean(ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING));
}

function realtimeSocketUrl() {
  try {
    const base = runtime.apiBaseUrl || window.location.origin;
    const url = new URL("/ws/realtime", ensureTrailingSlash(base));
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const symbols = realtimeSymbols();
    const channels = Array.isArray(state.config?.realtime?.channels) ? state.config.realtime.channels.filter(Boolean) : [];
    if (symbols.length) url.searchParams.set("symbols", symbols.join(","));
    if (channels.length) url.searchParams.set("channels", channels.join(","));
    url.searchParams.set("liquidations", String(state.config?.realtime?.liquidations_enabled !== false));
    const token = sessionStorage.getItem("qflAdminToken");
    if (token) url.searchParams.set("token", token);
    return url.toString();
  } catch (_) {
    return "";
  }
}

function realtimeSymbols() {
  return (state.config?.data?.universe || [])
    .filter((item) => String(item.asset_class || "").toLowerCase() === "crypto")
    .map((item) => item.exchange || item.symbol)
    .filter(Boolean);
}

function realtimeWithEvent(level, message, patch = {}) {
  const current = state.realtime || {};
  const events = [...(current.events || []), {
    timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    level,
    message,
  }].slice(-120);
  return {
    status: "starting",
    error: null,
    startedAt: current.startedAt || null,
    stoppedAt: current.stoppedAt || null,
    messageCount: current.messageCount || 0,
    subscribedArgs: current.subscribedArgs || [],
    orderBooks: current.orderBooks || [],
    trades: current.trades || [],
    liquidations: current.liquidations || [],
    transport: current.transport || "websocket",
    ...current,
    ...patch,
    events,
  };
}

async function loadMarketSeries() {
  if (runtime.staticSite) {
    const payload = await fetchJson("/api/market");
    const symbols = payload.symbols || sortedUnique((payload.rows || []).map((row) => row.symbol));
    const selected = state.market.selectedSymbol && symbols.includes(state.market.selectedSymbol)
      ? state.market.selectedSymbol
      : (payload.selectedSymbol || symbols[0] || "");
    state.market.symbols = symbols;
    state.market.selectedSymbol = selected;
    state.market.rows = (payload.rows || [])
      .filter((row) => !selected || row.symbol === selected)
      .slice(-(state.market.limit || 240));
    state.market.hoverIndex = null;
    return;
  }
  const params = new URLSearchParams();
  if (state.market.selectedSymbol) params.set("symbol", state.market.selectedSymbol);
  params.set("limit", String(state.market.limit || 240));
  const payload = await fetchJson(`/api/market?${params.toString()}`);
  state.market.symbols = payload.symbols || [];
  state.market.selectedSymbol = payload.selectedSymbol || state.market.symbols[0] || "";
  state.market.rows = payload.rows || [];
  state.market.hoverIndex = null;
}

async function loadRunHistory() {
  const payload = await fetchJson("/api/runs?limit=50");
  state.runs = payload.runs || [];
}

async function refreshRunHistory() {
  await loadRunHistory();
  renderRunHistory();
}

async function safeRefreshRunHistory() {
  try {
    await refreshRunHistory();
  } catch (_) {
  }
}

function render() {
  if (!state.config) return;
  renderForms();
  renderUniverse();
  renderOverview();
  renderCockpit();
  renderSourceHealth();
  renderMicrostructureSnapshot();
  renderRealtime();
  renderMarketPanel();
  renderDataQuality();
  renderOnchain();
  renderRawManifest();
  renderSignalRadar();
  renderDecisionCards();
  renderResults();
  renderRunHistory();
  renderJobLogs();
  byId("configPathLabel").textContent = state.configPath;
  byId("jsonEditor").value = JSON.stringify(state.config, null, 2);
}

function renderOverview() {
  const metricGrid = byId("metricGrid");
  if (!metricGrid) return;
  const summary = state.snapshot?.summary;
  const source = state.snapshot?.sourceHealth?.summary || summary?.source_health?.summary || {};
  const metrics = [
    ["数据源", source.provider_label ? `${source.provider_label} / ${sourceStatusMeta(source.status).label}` : "暂无"],
    ["标的数", summary?.symbols?.length ?? 0],
    ["因子数", summary?.factor_count ?? 0],
    ["首选因子", summary?.top_factor?.factor ?? "暂无"],
    ["Sharpe", formatNumber(summary?.backtest_metrics?.sharpe, 2)],
  ];
  metricGrid.innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function renderCockpit() {
  const symbols = cockpitSymbols();
  if (!state.cockpit.selectedSymbol || !symbols.includes(state.cockpit.selectedSymbol)) {
    state.cockpit.selectedSymbol = state.market.selectedSymbol || symbols[0] || "";
  }
  byId("cockpitSymbolSelect").innerHTML = symbols.map((symbol) => `<option value="${escapeAttr(symbol)}">${escapeHtml(symbol)}</option>`).join("");
  byId("cockpitSymbolSelect").value = state.cockpit.selectedSymbol;
  byId("cockpitTemplateSelect").value = state.cockpit.template;

  const rows = getCleanMarketRows().filter((row) => !state.cockpit.selectedSymbol || row.symbol === state.cockpit.selectedSymbol);
  const playbook = buildCockpitPlaybook(state.cockpit.selectedSymbol, rows);
  const template = indicatorTemplates[state.cockpit.template] || indicatorTemplates.trend_follow;
  const stance = stanceMeta(playbook.bias);

  byId("cockpitHeadline").textContent = `${state.cockpit.selectedSymbol || "-"} / ${playbook.regimeLabel}`;
  byId("cockpitChartNote").textContent = playbook.chartNote;
  byId("cockpitPlaybookSymbol").textContent = state.cockpit.selectedSymbol || "-";
  byId("cockpitPlaybookBias").className = `stance-badge ${stance.className}`;
  byId("cockpitPlaybookBias").textContent = stance.label;
  byId("cockpitPlaybookTitle").textContent = playbook.title;
  byId("cockpitPlaybookSummary").textContent = playbook.summary;
  byId("cockpitConfidence").textContent = playbook.confidence === null ? "-" : `${Math.round(playbook.confidence * 100)}/100`;
  byId("cockpitPlaybookFacts").innerHTML = playbook.facts.map((item) => `<div><dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(item.value)}</dd></div>`).join("");
  byId("cockpitTriggerList").innerHTML = listHtml(playbook.triggers, "等待指标与盘口形成明确触发");
  byId("cockpitInvalidationList").innerHTML = listHtml(playbook.invalidations, "等待主因子和结构低/高点确认失效条件");
  byId("cockpitEvidenceList").innerHTML = listHtml(playbook.evidence, "暂无足够支持证据");
  byId("cockpitConflictList").innerHTML = listHtml(playbook.conflicts, "暂无明显冲突");
  byId("cockpitTemplateNote").textContent = template.description;
  byId("cockpitActiveIndicators").innerHTML = template.indicators.map((key) => `<span>${escapeHtml(indicatorCatalog[key]?.name || key)}</span>`).join("");
  byId("cockpitIndicatorCatalog").innerHTML = template.indicators.map((key) => indicatorCardHtml(key, state.cockpit.selectedSymbol)).join("");
  byId("cockpitMarketStrip").innerHTML = marketStripHtml(rows, playbook);
  byId("cockpitContextSnapshot").innerHTML = contextSnapshotHtml(state.cockpit.selectedSymbol);
  drawCandlestickChart(byId("cockpitCanvas"), rows);
}

function buildCockpitPlaybook(symbol, rows) {
  const card = selectedDecisionCard(symbol);
  const signalRows = selectedSignalRows(symbol);
  const micro = selectedMicrostructure(symbol);
  const derivative = selectedDerivative(symbol);
  const onchain = selectedOnchain(symbol);
  const technical = selectedIndicatorState(symbol);
  const latest = rows[rows.length - 1];
  const previous = rows.length > 2 ? rows[rows.length - 2] : null;
  const change = latest && previous?.close ? latest.close / previous.close - 1 : null;
  const bias = card?.stance || technical?.technical_bias || inferBias(signalRows, micro, derivative);
  const confidence = numericOrNull(card?.confidence) ?? numericOrNull(technical?.confidence) ?? confidenceFromSignals(signalRows);
  const regimeLabel = regimeFromInputs(card, signalRows, micro, derivative, onchain, technical);
  const evidence = splitEvidence(card?.evidence);
  addIf(evidence, technical?.interpretation);
  addIf(evidence, technical ? `\u6280\u672f\u7ed3\u6784\uff1a${technical.trend_state || "\u6682\u65e0"}\uff0c${technical.momentum_state || "\u6682\u65e0"}\uff0c${technical.volume_state || "\u6682\u65e0"}` : "");
  addIf(evidence, topSignalText(signalRows));
  addIf(evidence, onchain?.interpretation);
  addIf(evidence, micro?.interpretation);
  const conflicts = [];
  addIf(conflicts, card?.risk_note);
  if (card?.stance && technical?.technical_bias && !sameBiasFamily(card.stance, technical.technical_bias)) {
    conflicts.push(`\u6280\u672f\u6307\u6807\u4e3a${technical.technical_bias_label || technical.technical_bias}\uff0c\u4e0e\u56e0\u5b50\u51b3\u7b56\u5361\u65b9\u5411\u4e0d\u5b8c\u5168\u4e00\u81f4`);
  }
  const funding = numericOrNull(derivative?.annualized_funding_rate);
  if (funding !== null && Math.abs(funding) > 0.1) conflicts.push(`\u8d44\u91d1\u8d39\u5e74\u5316 ${formatPercent(funding, 1)}\uff0c\u5408\u7ea6\u62e5\u6324\u9700\u8981\u964d\u6743`);
  const spread = numericOrNull(micro?.spread_bps);
  if (spread !== null && spread > 8) conflicts.push(`\u76d8\u53e3\u4ef7\u5dee ${formatNumber(spread, 2)}bps\uff0c\u8ffd\u4ef7\u6ed1\u70b9\u98ce\u9669\u504f\u9ad8`);
  if (!onchain) conflicts.push("\u94fe\u4e0a\u4e0a\u4e0b\u6587\u7f3a\u5931\uff0c\u65e0\u6cd5\u9a8c\u8bc1\u4ef7\u683c\u80cc\u540e\u7684\u7f51\u7edc\u6d3b\u52a8");
  const triggers = triggerSuggestions(bias, micro, card, technical);
  const invalidations = splitSentence(card?.invalidation_note || "") || [];
  addIf(invalidations, bias.includes("LONG") ? technical?.invalidation_long : technical?.invalidation_short);
  return {
    bias,
    confidence,
    regimeLabel,
    title: card?.stance_label ? `${regimeLabel} / ${card.stance_label}` : regimeLabel,
    summary: card?.action_hint || actionSummary(bias, micro, derivative, technical),
    chartNote: latest ? `${formatDateTime(latest.timestamp)} / \u6700\u65b0\u4ef7 ${formatNumber(latest.close, 2)} / \u6700\u8fd1\u4e00\u6839 ${change === null ? "\u6682\u65e0" : formatPercent(change, 2)}` : "\u6682\u65e0\u884c\u60c5\u6570\u636e",
    facts: [
      { label: "\u6700\u65b0\u4ef7", value: latest ? formatNumber(latest.close, 2) : "\u6682\u65e0" },
      { label: "\u6280\u672f\u7ed3\u6784", value: technical?.technical_bias_label || technical?.trend_state || "\u6682\u65e0" },
      { label: "RSI / ATR", value: technical ? `${formatNumber(technical.rsi_14, 1)} / ${formatPercent(technical.atr_percent_14, 2)}` : "\u6682\u65e0" },
      { label: "VWAP", value: technical?.above_vwap === true ? "\u4ef7\u683c\u5728\u4e0a\u65b9" : technical?.above_vwap === false ? "\u4ef7\u683c\u5728\u4e0b\u65b9" : "\u6682\u65e0" },
      { label: "\u4e3b\u56e0\u5b50", value: card?.primary_factor || signalRows[0]?.factor || "\u6682\u65e0" },
      { label: "\u6837\u672c\u5916", value: walkForwardSummary() },
    ],
    evidence,
    conflicts,
    triggers,
    invalidations,
  };
}

function indicatorCardHtml(key, symbol = state.cockpit.selectedSymbol) {
  const item = indicatorCatalog[key] || { name: key, group: "\u81ea\u5b9a\u4e49", role: "\u5f85\u5b9a\u4e49", status: "next" };
  const snapshot = indicatorSnapshot(key, symbol);
  const status = snapshot.hasValue ? (item.status === "live" ? "\u5df2\u8ba1\u7b97" : "\u5df2\u63a5\u5165") : (item.status === "live" ? "\u7b49\u5f85\u8fd0\u884c" : "\u5f85\u63a5\u5165");
  const className = snapshot.hasValue ? "is-live" : "is-next";
  return `<div class="indicator-card"><div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.group)}</span></div><p>${escapeHtml(item.role)}</p><p class="indicator-reading">${escapeHtml(snapshot.reading)}</p><em class="${className}">${status}</em></div>`;
}

function indicatorSnapshot(key, symbol) {
  const tech = selectedIndicatorState(symbol);
  const latest = latestMarketRow(symbol);
  const micro = selectedMicrostructure(symbol);
  const derivative = selectedDerivative(symbol);
  const onchain = selectedOnchain(symbol);
  const signals = selectedSignalRows(symbol);
  const value = (field) => numericOrNull(latest?.[field]) ?? numericOrNull(tech?.[field]);
  const signalByPrefix = (prefix) => signals.find((row) => String(row.factor || "").startsWith(prefix));
  const snapshots = {
    ema: [`EMA20 ${formatNumber(value("ema_20"), 2)} / EMA50 ${formatNumber(value("ema_50"), 2)}`, tech?.ema_alignment],
    supertrend: [tech?.supertrend_direction === 1 ? "\u591a\u5934" : tech?.supertrend_direction === -1 ? "\u7a7a\u5934" : "\u6682\u65e0", formatNumber(value("supertrend_10_3"), 2)],
    macd: [`\u67f1 ${formatNumber(value("macd_hist"), 4)}`, value("macd_hist") >= 0 ? "\u52a8\u80fd\u5411\u4e0a" : "\u52a8\u80fd\u5411\u4e0b"],
    adx: [`ADX ${formatNumber(value("adx_14"), 1)}`, `+DI ${formatNumber(value("plus_di_14"), 1)} / -DI ${formatNumber(value("minus_di_14"), 1)}`],
    rsi: [`RSI ${formatNumber(value("rsi_14"), 1)}`, tech?.momentum_state],
    stoch_rsi: [`K ${formatPercent(value("stoch_rsi_k_14"), 1)}`, `D ${formatPercent(value("stoch_rsi_d_14"), 1)}`],
    bollinger: [`%B ${formatPercent(value("bb_percent_b_20"), 1)}`, `\u4e0a\u8f68 ${formatNumber(value("bb_upper_20"), 2)}`],
    atr: [`ATR ${formatNumber(value("atr_14"), 2)}`, formatPercent(value("atr_percent_14"), 2)],
    donchian: [`\u901a\u9053\u4f4d\u7f6e ${formatPercent(value("donchian_position_20"), 1)}`, `\u4e0a\u6cbf ${formatNumber(value("donchian_high_20"), 2)}`],
    vwap: [`VWAP ${formatNumber(value("vwap_20"), 2)}`, tech?.above_vwap === true ? "\u4ef7\u683c\u5728\u4e0a\u65b9" : tech?.above_vwap === false ? "\u4ef7\u683c\u5728\u4e0b\u65b9" : "\u6682\u65e0"],
    obv: [`OBV\u659c\u7387 ${formatNumber(value("obv_slope_20"), 0)}`, tech?.volume_state],
    mfi: [`MFI ${formatNumber(value("mfi_14"), 1)}`, tech?.volume_state],
    volume_zscore: [signalByPrefix("volume_zscore")?.signal_label || "\u6682\u65e0", signalByPrefix("volume_zscore")?.interpretation],
    orderbook: [micro?.microstructure_label || "\u6682\u65e0", micro ? `\u4ef7\u5dee ${formatNumber(micro.spread_bps, 2)}bps` : ""],
    funding: [derivative ? formatPercent(derivative.annualized_funding_rate, 1) : "\u6682\u65e0", derivative?.crowding_label],
    oi: [derivative ? formatUsd(derivative.oi_usd) : "\u6682\u65e0", derivative?.crowding_label],
    long_short: [formatNumber(selectedDerivativeHistory(symbol)?.long_short_ratio, 2), "\u8d26\u6237\u591a\u7a7a\u6bd4"],
    active_addresses: [onchain ? formatNumber(onchain.active_addresses, 0) : "\u6682\u65e0", onchain?.onchain_label],
    tx_count: [onchain ? formatNumber(onchain.tx_count, 0) : "\u6682\u65e0", onchain?.onchain_label],
    liquidation: [(state.realtime?.liquidations || []).length ? `${state.realtime.liquidations.length} \u6761` : "\u6682\u65e0", "OKX\u5f3a\u5e73\u6d41"],
    cvd: ["\u5f85\u63a5\u5165", "\u9700\u8981\u9010\u7b14\u6210\u4ea4\u65b9\u5411\u7d2f\u8ba1"],
  };
  const parts = snapshots[key] || ["\u6682\u65e0", ""];
  const reading = parts.filter((part) => part !== undefined && part !== null && String(part).trim()).join(" / ") || "\u6682\u65e0";
  return { reading, hasValue: !reading.includes("\u6682\u65e0") && !reading.includes("\u5f85\u63a5\u5165") };
}

function latestMarketRow(symbol) {
  const rows = getCleanMarketRows().filter((row) => String(row.symbol) === String(symbol));
  return rows[rows.length - 1] || null;
}

function selectedIndicatorState(symbol) {
  const rows = state.snapshot?.indicatorStates || state.snapshot?.summary?.indicator_states || [];
  return rows.find((row) => String(row.symbol) === String(symbol)) || null;
}

function sameBiasFamily(left, right) {
  if (!left || !right) return true;
  if (String(left).includes("LONG") && String(right).includes("LONG")) return true;
  if (String(left).includes("SHORT") && String(right).includes("SHORT")) return true;
  return left === right;
}

function marketStripHtml(rows, playbook) {
  const latest = rows[rows.length - 1];
  if (!latest) return `<div class="strip-item"><span>状态</span><strong>暂无行情</strong></div>`;
  const dayReturn = numericOrNull(selectedDecisionCard(state.cockpit.selectedSymbol)?.return_1d);
  return [
    ["最新价", formatNumber(latest.close, 2)],
    ["1期变化", dayReturn === null ? "暂无" : formatPercent(dayReturn, 2)],
    ["结构", playbook.regimeLabel],
    ["置信度", playbook.confidence === null ? "暂无" : `${Math.round(playbook.confidence * 100)}分`],
  ].map(([label, value]) => `<div class="strip-item"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function contextSnapshotHtml(symbol) {
  const micro = selectedMicrostructure(symbol);
  const derivative = selectedDerivative(symbol);
  const onchain = selectedOnchain(symbol);
  const rows = [
    ["盘口价差", micro ? `${formatNumber(micro.spread_bps, 2)} bps` : "暂无"],
    ["深度倾斜", micro ? formatPercent(micro.depth_imbalance, 1) : "暂无"],
    ["OI", derivative ? formatUsd(derivative.oi_usd) : "暂无"],
    ["多空比", formatNumber(selectedDerivativeHistory(symbol)?.long_short_ratio, 2)],
    ["活跃地址", onchain ? formatNumber(onchain.active_addresses, 0) : "暂无"],
    ["链上状态", onchain?.onchain_label || "暂无"],
  ];
  return rows.map(([label, value]) => `<div><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
}

function renderSourceHealth() {
  const report = state.snapshot?.sourceHealth || state.snapshot?.summary?.source_health;
  const summary = report?.summary || {};
  const rows = state.snapshot?.sourceStatusRows || report?.rows || [];
  const status = sourceStatusMeta(summary.status);
  byId("sourceHealthNote").textContent = report ? `${summary.provider_label || summary.provider || "未知"} / ${status.label}` : "暂无数据源健康报告";
  byId("sourceHealthMessages").innerHTML = (report?.messages || []).length
    ? report.messages.map((message) => `<div class="source-message">${escapeHtml(message)}</div>`).join("")
    : `<div class="source-message">运行流水线后会显示真实数据源、新鲜度、上下文完整度和权限缺口。</div>`;
  byId("sourceStatusBody").innerHTML = rows.length
    ? rows.map((row) => {
        const meta = sourceStatusMeta(row.status);
        return `<tr><td>${escapeHtml(row.scope)}</td><td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.provider)}</td><td><span class="source-status ${meta.className}">${meta.label}</span></td><td>${escapeHtml(row.latest_timestamp || "-")}</td><td class="interpretation-cell">${escapeHtml(row.message || "")}</td></tr>`;
      }).join("")
    : emptyRow(6, "暂无数据源状态");
}

function renderMicrostructureSnapshot() {
  const rows = [...(state.snapshot?.microstructureSnapshot || state.snapshot?.summary?.microstructure_snapshot || [])].filter((row) => row?.symbol);
  byId("microstructureNote").textContent = rows.length ? `已载入 ${rows.length} 条OKX盘口快照` : "暂无OKX盘口快照";
  byId("microstructureBody").innerHTML = rows.length
    ? rows.map((row) => `<tr><td>${escapeHtml(row.symbol)}</td><td>${formatNumber(row.spread_bps, 2)}</td><td>${formatPercent(row.depth_imbalance, 1)}</td><td>${formatNumber(row.buy_trade_count, 0)}买 / ${formatNumber(row.sell_trade_count, 0)}卖</td><td><span class="source-status is-ok">${escapeHtml(row.microstructure_label || "已获取")}</span></td></tr>`).join("")
    : emptyRow(5, "启用OKX真实数据和盘口逐笔后重新运行。");
}

function renderRealtime() {
  const data = state.realtime || {};
  const meta = realtimeMeta(data.status);
  const transport = data.transport === "websocket" ? "WebSocket" : data.transport === "rest" ? "REST" : (isRealtimeSocketActive() ? "WebSocket" : "REST");
  byId("realtimeStatus").textContent = `${meta.label} / ${transport} / 消息 ${formatNumber(data.messageCount, 0)}${data.error ? ` / ${data.error}` : ""}`;
  byId("realtimeBookBody").innerHTML = (data.orderBooks || []).length
    ? data.orderBooks.map((row) => `<tr><td>${escapeHtml(row.inst_id)}</td><td>${formatNumber(row.best_bid, 2)}</td><td>${formatNumber(row.best_ask, 2)}</td><td>${formatNumber(row.spread_bps, 2)}</td><td>${formatUsd(row.bid_depth_usd)}</td><td>${formatUsd(row.ask_depth_usd)}</td></tr>`).join("")
    : emptyRow(6, "暂无实时盘口");
  byId("realtimeTradesBody").innerHTML = (data.trades || []).slice(0, 40).map((row) => `<tr><td>${formatDateTime(row.timestamp)}</td><td>${escapeHtml(row.inst_id)}</td><td>${escapeHtml(row.side)}</td><td>${formatNumber(row.price, 2)}</td><td>${formatNumber(row.size, 4)}</td></tr>`).join("") || emptyRow(5, "暂无逐笔成交");
  byId("realtimeLiquidationsBody").innerHTML = (data.liquidations || []).slice(0, 40).map((row) => `<tr><td>${formatDateTime(row.timestamp)}</td><td>${escapeHtml(row.inst_id)}</td><td>${escapeHtml(row.side)}</td><td>${formatNumber(row.size, 4)}</td><td>${formatNumber(row.bankruptcy_price, 2)}</td></tr>`).join("") || emptyRow(5, "暂无强平流");
  byId("realtimeEvents").innerHTML = (data.events || []).slice(-80).map((row) => `<div><span>${escapeHtml(row.timestamp || "")}</span><strong>${escapeHtml(row.level || "")}</strong> ${escapeHtml(row.message || "")}</div>`).join("") || `<div>暂无服务日志</div>`;
}

function renderMarketPanel() {
  const symbols = state.market.symbols.length ? state.market.symbols : (state.snapshot?.summary?.symbols || state.config?.data?.universe?.map((item) => item.symbol) || []);
  byId("klineSymbolSelect").innerHTML = symbols.map((symbol) => `<option value="${escapeAttr(symbol)}">${escapeHtml(symbol)}</option>`).join("");
  byId("klineSymbolSelect").value = state.market.selectedSymbol || symbols[0] || "";
  byId("klineLimitSelect").value = String(state.market.limit || 240);
  const rows = getCleanMarketRows();
  byId("klineStatus").textContent = rows.length ? `${state.market.selectedSymbol} / ${rows.length} 根K线` : "暂无K线数据";
  drawCandlestickChart(byId("klineCanvas"), rows);
  renderQuotePanel(rows);
  renderKlineTable(rows);
}

function renderQuotePanel(rows) {
  const panel = byId("quotePanel");
  const row = state.market.hoverIndex !== null && rows[state.market.hoverIndex] ? rows[state.market.hoverIndex] : rows[rows.length - 1];
  if (!row) {
    panel.innerHTML = `<div class="quote-empty">\u6682\u65e0\u884c\u60c5</div>`;
    return;
  }
  const previous = rows[Math.max(0, rows.indexOf(row) - 1)];
  const change = previous?.close ? row.close / previous.close - 1 : 0;
  const tone = change >= 0 ? "text-positive" : "text-danger";
  panel.innerHTML = `<div class="quote-title"><span>${escapeHtml(row.symbol || state.market.selectedSymbol)}</span><strong class="${tone}">${formatNumber(row.close, 2)}</strong><span>${escapeHtml(row.timestamp)}</span></div><dl class="quote-grid"><div><dt>\u6da8\u8dcc</dt><dd class="${tone}">${formatPercent(change, 2)}</dd></div><div><dt>\u5f00\u76d8</dt><dd>${formatNumber(row.open, 2)}</dd></div><div><dt>\u6700\u9ad8</dt><dd>${formatNumber(row.high, 2)}</dd></div><div><dt>\u6700\u4f4e</dt><dd>${formatNumber(row.low, 2)}</dd></div><div><dt>\u6210\u4ea4\u91cf</dt><dd>${formatNumber(row.volume, 0)}</dd></div><div><dt>EMA20</dt><dd>${formatNumber(row.ema_20, 2)}</dd></div><div><dt>VWAP</dt><dd>${formatNumber(row.vwap_20, 2)}</dd></div><div><dt>RSI</dt><dd>${formatNumber(row.rsi_14, 1)}</dd></div><div><dt>ATR%</dt><dd>${formatPercent(row.atr_percent_14, 2)}</dd></div></dl>`;
}

function renderKlineTable(rows) {
  byId("klineTableBody").innerHTML = rows.length
    ? rows.slice(-40).reverse().map((row, index, latestRows) => {
        const next = latestRows[index + 1];
        const change = next?.close ? row.close / next.close - 1 : 0;
        const tone = change >= 0 ? "text-positive" : "text-danger";
        return `<tr><td>${escapeHtml(row.timestamp)}</td><td>${formatNumber(row.open, 2)}</td><td>${formatNumber(row.high, 2)}</td><td>${formatNumber(row.low, 2)}</td><td>${formatNumber(row.close, 2)}</td><td>${formatNumber(row.volume, 0)}</td><td class="${tone}">${formatPercent(change, 2)}</td></tr>`;
      }).join("")
    : emptyRow(7, "暂无K线数据");
}

function renderForms() {
  document.querySelectorAll("[data-path]").forEach((input) => {
    if (input.closest("tbody")) return;
    const value = getPath(state.config, input.dataset.path);
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (Array.isArray(value)) input.value = value.join(", ");
    else input.value = value ?? "";
  });
}

function renderUniverse() {
  const universe = state.config.data.universe || [];
  byId("universeBody").innerHTML = universe.length
    ? universe.map((item, index) => `<tr><td><input aria-label="代码" value="${escapeAttr(item.symbol || "")}" data-array="universe" data-index="${index}" data-field="symbol" /></td><td>${selectHtml(assetClasses, item.asset_class || "crypto", `data-array="universe" data-index="${index}" data-field="asset_class"` )}</td><td><input aria-label="交易所覆盖" value="${escapeAttr(item.exchange || "")}" data-array="universe" data-index="${index}" data-field="exchange" /></td><td><input aria-label="计价" value="${escapeAttr(item.currency || "USD")}" data-array="universe" data-index="${index}" data-field="currency" /></td><td><button class="button button-danger" type="button" data-remove="universe" data-index="${index}">删除</button></td></tr>`).join("")
    : emptyRow(5, "暂无标的");
}

function renderDataQuality() {
  const report = state.snapshot?.dataQuality || state.snapshot?.summary?.data_quality;
  const rows = state.snapshot?.dataQualityRows || report?.symbols || [];
  byId("dataQualityNote").textContent = report ? `状态 ${qualityMeta(report.summary?.status).label}` : "暂无质量报告";
  const summary = report?.summary || {};
  const items = [["状态", qualityMeta(summary.status).label], ["问题数", summary.issue_count ?? 0], ["记录", summary.total_rows ?? 0], ["质量", formatNumber(summary.average_quality_score, 2)]];
  byId("dataQualitySummary").innerHTML = items.map(([label, value]) => `<div class="quality-summary-item"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
  byId("dataQualityBody").innerHTML = rows.length
    ? rows.map((row) => `<tr><td>${escapeHtml(row.symbol)}</td><td><span class="quality-status ${qualityMeta(row.status).className}">${qualityMeta(row.status).label}</span></td><td>${formatNumber(row.rows, 0)}</td><td>${formatNumber(row.duplicate_timestamps, 0)}</td><td>${formatNumber(row.missing_required_values, 0)}</td><td>${formatNumber(row.invalid_prices, 0)}</td><td>${formatNumber(row.zero_or_negative_volume, 0)}</td><td>${formatNumber(row.extreme_returns, 0)}</td><td>${formatNumber(row.estimated_missing_bars, 0)}</td><td>${formatNumber(row.quality_score, 2)}</td></tr>`).join("")
    : emptyRow(10, "暂无质量数据");
}

function renderOnchain() {
  const rows = [...(state.snapshot?.onchainMetrics || state.snapshot?.summary?.onchain_metrics || [])].filter((row) => row?.symbol);
  byId("onchainNote").textContent = rows.length ? `已载入 ${rows.length} 条链上指标` : "未启用或暂无链上指标";
  byId("onchainBody").innerHTML = rows.length
    ? rows.slice(-80).reverse().map((row) => `<tr><td>${formatDateTime(row.timestamp)}</td><td>${escapeHtml(row.symbol)}</td><td>${formatNumber(row.active_addresses, 0)}</td><td>${formatPercent(row.active_address_change, 1)}</td><td>${formatNumber(row.tx_count, 0)}</td><td>${formatPercent(row.tx_count_change, 1)}</td><td><span class="source-status is-ok">${escapeHtml(row.onchain_label || "-")}</span></td><td class="interpretation-cell">${escapeHtml(row.interpretation || "")}</td></tr>`).join("")
    : emptyRow(8, "在参数中启用 Coin Metrics 链上数据后重新运行。");
}

function renderRawManifest() {
  const rows = state.snapshot?.rawDataManifest || state.snapshot?.summary?.raw_data_manifest || [];
  byId("rawManifestBody").innerHTML = rows.length
    ? rows.map((row) => `<tr><td>${escapeHtml(row.dataset)}</td><td>${escapeHtml(row.provider)}</td><td>${escapeHtml(row.symbols)}</td><td>${formatNumber(row.rows, 0)}</td><td>${escapeHtml(row.start_time || "-")} → ${escapeHtml(row.end_time || "-")}</td><td class="hash-cell">${escapeHtml(String(row.content_hash || "").slice(0, 16))}</td><td class="path-cell">${escapeHtml(row.artifact_path)}</td></tr>`).join("")
    : emptyRow(7, "暂无原始数据版本记录");
}

function renderSignalRadar() {
  const rows = [...(state.snapshot?.factorSignals || state.snapshot?.summary?.factor_signals || [])].filter((row) => row?.symbol);
  const symbols = sortedUnique(rows.map((row) => row.symbol));
  byId("signalSymbolSelect").innerHTML = [`<option value="">全部</option>`, ...symbols.map((symbol) => `<option value="${escapeAttr(symbol)}">${escapeHtml(symbol)}</option>`)].join("");
  byId("signalSymbolSelect").value = state.signals.selectedSymbol;
  byId("signalSideSelect").value = state.signals.side;
  const filtered = rows.filter((row) => (!state.signals.selectedSymbol || row.symbol === state.signals.selectedSymbol) && (state.signals.side === "ALL" || row.signal === state.signals.side));
  byId("signalRadarNote").textContent = rows.length ? `共 ${rows.length} 条因子信号` : "暂无因子信号";
  const summary = [["偏多", rows.filter((row) => row.signal === "BULLISH").length], ["偏空", rows.filter((row) => row.signal === "BEARISH").length], ["中性", rows.filter((row) => row.signal === "NEUTRAL").length], ["强信号", rows.filter((row) => Number(row.confidence) >= 0.7).length]];
  byId("signalSummary").innerHTML = summary.map(([label, value]) => `<div class="signal-summary-item"><span>${label}</span><strong>${value}</strong></div>`).join("");
  byId("signalRadarBody").innerHTML = filtered.length
    ? filtered.slice(0, 120).map((row) => `<tr><td>${escapeHtml(row.symbol)}</td><td>${escapeHtml(row.factor)}</td><td>${escapeHtml(row.theme || "")}</td><td>${formatNumber(row.current_value, 4)}</td><td>${formatPercent(row.percentile, 1)}</td><td>${formatNumber(row.sharpe, 2)}</td><td>${formatPercent(row.win_rate, 1)}</td><td><span class="signal-badge ${signalMeta(row.signal).className}">${signalMeta(row.signal).label}</span></td><td>${formatPercent(row.confidence, 0)}</td><td class="interpretation-cell">${escapeHtml(row.interpretation || "")}</td></tr>`).join("")
    : emptyRow(10, "暂无匹配信号");
}

function renderDecisionCards() {
  const rows = [...(state.snapshot?.decisionCards || state.snapshot?.summary?.decision_cards || [])].filter((row) => row?.symbol);
  byId("decisionCardsNote").textContent = rows.length ? `共 ${rows.length} 张决策卡` : "暂无决策卡";
  byId("decisionCardsGrid").innerHTML = rows.length
    ? rows.map((row) => {
        const stance = stanceMeta(row.stance);
        return `<article class="decision-card ${stance.className}"><div class="decision-card-header"><div><span class="decision-symbol">${escapeHtml(row.symbol)}</span><strong>${formatNumber(row.last_close, 2)}</strong></div><span class="stance-badge ${stance.className}">${stance.label}</span></div><dl class="decision-facts"><div><dt>主因子</dt><dd>${escapeHtml(row.primary_factor || "-")}</dd></div><div><dt>资金费</dt><dd>${formatPercent(row.funding_annualized, 1)}</dd></div><div><dt>盘口</dt><dd>${escapeHtml(row.microstructure_label || "-")}</dd></div><div><dt>链上</dt><dd>${escapeHtml(row.onchain_label || "-")}</dd></div></dl><p class="decision-evidence">${escapeHtml(row.evidence || "")}</p><p class="decision-risk">${escapeHtml(row.risk_note || "")}</p><p class="decision-invalid">${escapeHtml(row.invalidation_note || "")}</p></article>`;
      }).join("")
    : `<div class="decision-empty">暂无决策卡</div>`;
  byId("decisionCardsBody").innerHTML = rows.length
    ? rows.map((row) => `<tr><td>${escapeHtml(row.symbol)}</td><td>${stanceMeta(row.stance).label}</td><td>${formatPercent(row.confidence, 0)}</td><td>${escapeHtml(row.primary_factor || "-")}</td><td>${escapeHtml(row.crowding_label || "-")}</td><td>${escapeHtml(row.onchain_label || "-")}</td><td class="interpretation-cell">${escapeHtml(row.evidence || "")}</td><td class="interpretation-cell">${escapeHtml(row.risk_note || "")}</td><td class="interpretation-cell">${escapeHtml(row.invalidation_note || "")}</td></tr>`).join("")
    : emptyRow(9, "暂无决策明细");
}

function renderResults() {
  const factorBacktests = state.snapshot?.factorBacktests || state.snapshot?.summary?.factor_backtests || [];
  byId("factorBacktestBody").innerHTML = factorBacktests.length
    ? factorBacktests.map((row) => `<tr><td>${escapeHtml(row.factor)}</td><td>${row.horizon ?? ""}</td><td>${formatPercent(row.total_return, 2)}</td><td>${formatPercent(row.annualized_return, 2)}</td><td>${formatNumber(row.sharpe, 2)}</td><td>${formatPercent(row.max_drawdown, 2)}</td><td>${formatPercent(row.win_rate, 1)}</td><td>${formatNumber(row.average_turnover, 2)}</td><td>${formatPercent(row.average_execution_cost, 3)}</td><td>${formatPercent(row.average_funding_cost, 3)}</td></tr>`).join("")
    : emptyRow(10, "暂无因子回测结果");
  const walk = state.snapshot?.walkForward || state.snapshot?.summary?.walk_forward || [];
  byId("walkForwardBody").innerHTML = walk.length
    ? walk.map((row) => `<tr><td>${row.fold ?? ""}</td><td>${shortDate(row.train_start)} → ${shortDate(row.train_end)}</td><td>${shortDate(row.test_start)} → ${shortDate(row.test_end)}</td><td>${escapeHtml(row.selected_factor || "-")}</td><td>${formatNumber(row.train_score, 2)}</td><td>${formatPercent(row.test_total_return, 2)}</td><td>${formatNumber(row.test_sharpe, 2)}</td><td>${formatPercent(row.test_max_drawdown, 2)}</td><td class="run-error">${escapeHtml(row.error || "")}</td></tr>`).join("")
    : emptyRow(9, "未启用或暂无walk-forward结果");
  renderArtifacts();
  drawEquityCurve();
  drawFactorScores();
}

function renderArtifacts() {
  const artifacts = state.snapshot?.artifacts || [];
  byId("artifactList").innerHTML = artifacts.length
    ? artifacts.map((item) => `<a class="artifact" href="${artifactHref(item.name)}" target="_blank" rel="noreferrer"><strong>${escapeHtml(item.name)}</strong><span>${formatBytes(item.size)}</span></a>`).join("")
    : `<div class="empty-row">暂无产物</div>`;
}

function renderRunHistory() {
  byId("runHistoryBody").innerHTML = state.runs.length
    ? state.runs.map((run) => `<tr><td><span class="run-status ${runStatusMeta(run.status).className}">${runStatusMeta(run.status).label}</span></td><td>${formatDateTime(run.createdAt)}</td><td>${formatDateTime(run.finishedAt)}</td><td>${run.summary?.factor_count ?? ""}</td><td>${formatNumber(run.summary?.backtest_metrics?.sharpe, 2)}</td><td>${escapeHtml(run.summary?.top_factor?.factor || "")}</td><td class="path-cell">${escapeHtml(run.outputDir || "")}</td><td class="run-error">${escapeHtml(run.error || "")}</td></tr>`).join("")
    : emptyRow(8, "暂无运行历史");
}

function renderJobLogs() {
  byId("jobLogNote").textContent = state.activeJobId ? `任务 ${state.activeJobId}` : "暂无任务";
  byId("jobLogStream").innerHTML = state.jobLogs.length
    ? state.jobLogs.map((row) => `<div><span>${escapeHtml(row.timestamp)}</span><strong>${escapeHtml(row.level)}</strong> ${escapeHtml(row.message)}</div>`).join("")
    : `<div>暂无任务日志</div>`;
}

function cockpitSymbols() {
  return sortedUnique([
    ...(state.market.symbols || []),
    ...(state.snapshot?.summary?.symbols || []),
    ...(state.config?.data?.universe || []).map((item) => item.symbol),
  ]);
}

async function handleCockpitControls() {
  state.cockpit.selectedSymbol = byId("cockpitSymbolSelect").value;
  state.cockpit.template = byId("cockpitTemplateSelect").value || "trend_follow";
  if (state.market.selectedSymbol !== state.cockpit.selectedSymbol) {
    state.market.selectedSymbol = state.cockpit.selectedSymbol;
    try {
      await loadMarketSeries();
    } catch (error) {
      toast(error.message, true);
    }
  }
  renderCockpit();
}

function fillTemplateSelect() {
  byId("cockpitTemplateSelect").innerHTML = Object.entries(indicatorTemplates)
    .map(([key, template]) => `<option value="${key}">${escapeHtml(template.name)}</option>`)
    .join("");
}

function selectedDecisionCard(symbol) {
  const rows = state.snapshot?.decisionCards || state.snapshot?.summary?.decision_cards || [];
  return rows.find((row) => String(row.symbol) === String(symbol)) || null;
}

function selectedSignalRows(symbol) {
  return [...(state.snapshot?.factorSignals || state.snapshot?.summary?.factor_signals || [])]
    .filter((row) => String(row.symbol) === String(symbol))
    .sort((left, right) => Number(right.signal_score || 0) - Number(left.signal_score || 0));
}

function selectedMicrostructure(symbol) {
  const rows = state.snapshot?.microstructureSnapshot || state.snapshot?.summary?.microstructure_snapshot || [];
  return rows.find((row) => String(row.symbol) === String(symbol)) || null;
}

function selectedDerivative(symbol) {
  const rows = state.snapshot?.derivativesSnapshot || state.snapshot?.summary?.derivatives_snapshot || [];
  return rows.find((row) => String(row.symbol) === String(symbol)) || null;
}

function selectedDerivativeHistory(symbol) {
  const rows = [...(state.snapshot?.derivativesHistory || state.snapshot?.summary?.derivatives_history || [])]
    .filter((row) => String(row.symbol) === String(symbol));
  return rows[rows.length - 1] || null;
}

function selectedOnchain(symbol) {
  const rows = [...(state.snapshot?.onchainMetrics || state.snapshot?.summary?.onchain_metrics || [])]
    .filter((row) => String(row.symbol) === String(symbol));
  return rows[rows.length - 1] || null;
}

function inferBias(signals, micro, derivative) {
  const bullish = signals.filter((row) => row.signal === "BULLISH").length;
  const bearish = signals.filter((row) => row.signal === "BEARISH").length;
  const crowding = derivative?.crowding;
  if (bullish > bearish && ["LONG_CROWDED", "LONG_WARM"].includes(crowding)) return "WATCH_LONG";
  if (bearish > bullish && ["SHORT_CROWDED", "SHORT_WARM"].includes(crowding)) return "WATCH_SHORT";
  if (bullish > bearish) return "LEAN_LONG";
  if (bearish > bullish) return "LEAN_SHORT";
  if (micro?.microstructure_label === "买盘占优") return "WATCH_LONG";
  if (micro?.microstructure_label === "卖盘占优") return "WATCH_SHORT";
  return "NEUTRAL";
}

function confidenceFromSignals(signals) {
  const values = signals.map((row) => Number(row.confidence)).filter(Number.isFinite);
  if (!values.length) return null;
  return Math.max(...values);
}

function regimeFromInputs(card, signals, micro, derivative, onchain, technical) {
  if (card?.stance === "WATCH_LONG") return "趋势偏多但需等待";
  if (card?.stance === "WATCH_SHORT") return "趋势偏空但需等待";
  if (card?.stance === "LEAN_LONG") return "趋势偏多";
  if (card?.stance === "LEAN_SHORT") return "趋势偏空";
  if (technical?.trend_state) return technical.trend_state;
  const topTheme = signals[0]?.theme;
  if (topTheme === "趋势") return "趋势结构观察";
  if (topTheme === "波动") return "波动结构观察";
  if (topTheme === "流动性") return "流动性结构观察";
  if (micro?.microstructure_label) return micro.microstructure_label;
  if (onchain?.onchain_label) return onchain.onchain_label;
  if (derivative?.crowding_label) return derivative.crowding_label;
  return "等待结构确认";
}

function splitEvidence(text) {
  if (!text) return [];
  return String(text)
    .split(/[；;。]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 6);
}

function splitSentence(text, maxItems = 6) {
  if (!text) return [];
  return String(text)
    .split(/[；;。.\n|]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, maxItems);
}

function addIf(list, value) {
  if (value && !list.includes(value)) list.push(value);
}

function topSignalText(signals) {
  const signal = signals[0];
  if (!signal) return "";
  return `${signal.factor} 当前为 ${signal.signal_label || signal.signal || "未知"}，历史分位 ${formatPercent(signal.percentile, 0)}，回测 Sharpe ${formatNumber(signal.sharpe, 2)}`;
}

function triggerSuggestions(bias, micro, card, technical) {
  if (card?.action_hint) return splitEvidence(card.action_hint);
  if (technical && (bias === "LEAN_LONG" || bias === "WATCH_LONG")) return [technical.trigger_long, "\u76d8\u53e3\u4e70\u76d8\u91cd\u65b0\u5360\u4f18\uff0c\u4e14\u4ef7\u683c\u4e0d\u8dcc\u56deVWAP\u4e0b\u65b9"].filter(Boolean);
  if (technical && (bias === "LEAN_SHORT" || bias === "WATCH_SHORT")) return [technical.trigger_short, "\u76d8\u53e3\u5356\u76d8\u91cd\u65b0\u5360\u4f18\uff0c\u4e14\u4ef7\u683c\u4e0d\u7ad9\u56deVWAP\u4e0a\u65b9"].filter(Boolean);
  if (bias === "LEAN_LONG" || bias === "WATCH_LONG") {
    const base = ["等待回踩 VWAP / EMA20 一带不破", "盘口主动买单重新占优", "重新站回短线前高后确认"];
    if (micro?.microstructure_label === "买盘占优") base.unshift("盘口买盘占优维持，允许更积极观察多头触发");
    return base;
  }
  if (bias === "LEAN_SHORT" || bias === "WATCH_SHORT") {
    const base = ["等待反弹失败或跌破短线低点", "盘口卖盘继续占优", "资金费或 OI 过热后价格无法继续上行"];
    if (micro?.microstructure_label === "卖盘占优") base.unshift("盘口卖盘占优维持，空头触发更清晰");
    return base;
  }
  return ["等待趋势、盘口和衍生品至少两类证据同向", "避免在证据冲突时主动押方向"];
}

function actionSummary(bias, micro, derivative, technical) {
  if (bias === "LEAN_LONG") return "可以优先寻找顺势多头结构，但仍需等待盘口和回踩质量确认。";
  if (bias === "WATCH_LONG") return "方向偏多，但当前存在拥挤或追价风险，更适合等待低风险触发。";
  if (bias === "LEAN_SHORT") return "可以优先寻找反弹失败后的空头结构，注意强平回补风险。";
  if (bias === "WATCH_SHORT") return "方向偏空，但需要等待反弹失败或盘口卖盘重新确认。";
  if (micro?.microstructure_label || derivative?.crowding_label) return "结构还不够统一，先观察盘口、资金费和主因子是否形成共振。";
  return "暂无足够证据建立方向性假设。";
}

function walkForwardSummary() {
  const rows = state.snapshot?.walkForward || state.snapshot?.summary?.walk_forward || [];
  if (!rows.length) return "暂无";
  const passed = rows.filter((row) => Number(row.test_sharpe) > 0).length;
  return `${passed}/${rows.length}折为正`;
}

function listHtml(items, emptyText) {
  const clean = (items || []).map((item) => String(item).trim()).filter(Boolean).slice(0, 8);
  return clean.length ? clean.map((item) => `<li>${escapeHtml(item)}</li>`).join("") : `<li class="muted-item">${escapeHtml(emptyText)}</li>`;
}

function numericOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function handleFieldInput(event) {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement)) return;
  if (target.dataset.path) {
    setPath(state.config, target.dataset.path, parseInputValue(target));
    markDirty();
  } else if (target.dataset.array === "universe") {
    state.config.data.universe[Number(target.dataset.index)][target.dataset.field] = parseInputValue(target);
    markDirty();
  }
}

function handleTableActions(event) {
  const button = event.target.closest("button[data-remove]");
  if (!button) return;
  if (button.dataset.remove === "universe") {
    state.config.data.universe.splice(Number(button.dataset.index), 1);
    markDirty();
    renderUniverse();
  }
}

function addInstrument() {
  state.config.data.universe.push({ symbol: "ETH-USD", asset_class: "crypto", currency: "USD" });
  markDirty();
  renderUniverse();
}

function applyPreset(name) {
  const today = new Date();
  const end = addDays(today, 1);
  const base = {
    data: {
      provider: "okx",
      frequency: "1h",
      page_limit: 300,
      max_pages: 3,
      include_unconfirmed: false,
      universe: [
        { symbol: "ETH-USD", asset_class: "crypto", currency: "USD" },
        { symbol: "BTC-USD", asset_class: "crypto", currency: "USD" },
      ],
      adjustments: [],
    },
    mining: { enable_operator_miner: true, enable_ml_miner: false, operator_windows: [5, 10, 20, 60], target_horizon: 20, ml_max_features: 10, ml_train_fraction: 0.7, ml_n_estimators: 30, random_state: 7 },
    evaluation: { forward_horizons: [5, 20], min_observations: 50, min_cross_section_assets: 2 },
    derivatives: { enabled: true, history_enabled: true, history_limit: 40, history_period: "1D" },
    microstructure: { enabled: true, depth_size: 50, trade_limit: 100 },
    onchain: { enabled: true, provider: "coinmetrics_community", metrics: ["AdrActCnt", "TxCnt"] },
    validation: { enabled: true, n_splits: 3, train_fraction: 0.55, embargo_bars: 1, min_train_observations: 40, min_test_bars: 12 },
    realtime: { enabled: true, channels: ["books5", "trades"], liquidations_enabled: true },
    backtest: { horizon: 20, mode: "long_short", top_n: 1, bottom_n: 1, transaction_cost_bps: 5, slippage_bps: 2, spread_cost_multiplier: 0.5, funding_enabled: true, funding_rate_column: "annualized_funding_rate", funding_cost_multiplier: 1, annualization: 438 },
    output_dir: "runs/eth-trend-fast",
  };
  if (name === "okx_smoke") {
    base.data.start = toDateInput(addDays(today, -10));
    base.data.end = toDateInput(end);
    base.data.max_pages = 1;
    base.mining.operator_windows = [5, 10, 20];
    base.mining.target_horizon = 5;
    base.evaluation.forward_horizons = [5];
    base.backtest.horizon = 5;
    base.backtest.annualization = 1752;
    base.derivatives.history_limit = 20;
    base.validation.n_splits = 2;
    base.output_dir = "runs/okx-smoke";
  } else if (name === "deep_research") {
    base.data.start = toDateInput(addDays(today, -365));
    base.data.end = toDateInput(end);
    base.data.max_pages = 20;
    base.mining.enable_ml_miner = true;
    base.derivatives.history_limit = 100;
    base.output_dir = "runs/eth-trend-deep";
  } else {
    base.data.start = toDateInput(addDays(today, -45));
    base.data.end = toDateInput(end);
  }
  state.config = withDefaults(base);
  markDirty();
  render();
  setActiveView("config");
  toast("研究档位已应用，可保存或直接运行");
}

function applyJsonEditor() {
  try {
    state.config = withDefaults(JSON.parse(byId("jsonEditor").value));
    markDirty();
    render();
    toast("JSON已应用");
  } catch (error) {
    toast(error.message, true);
  }
}

async function handleKlineControls() {
  state.market.selectedSymbol = byId("klineSymbolSelect").value;
  state.market.limit = Number(byId("klineLimitSelect").value) || 240;
  try {
    await loadMarketSeries();
    renderMarketPanel();
  } catch (error) {
    toast(error.message, true);
  }
}

function handleSignalControls() {
  state.signals.selectedSymbol = byId("signalSymbolSelect").value;
  state.signals.side = byId("signalSideSelect").value || "ALL";
  renderSignalRadar();
}

function handleKlineHover(event) {
  const rows = getCleanMarketRows();
  if (!rows.length) return;
  const canvas = event.currentTarget;
  const rect = canvas.getBoundingClientRect();
  const x = ((event.clientX - rect.left) / rect.width) * canvas.width;
  const plot = getKlinePlot(canvas);
  if (x < plot.left || x > plot.right) state.market.hoverIndex = null;
  else {
    const step = (plot.right - plot.left) / rows.length;
    state.market.hoverIndex = Math.max(0, Math.min(rows.length - 1, Math.floor((x - plot.left) / step)));
  }
  renderMarketPanel();
}

function getCleanMarketRows() {
  return (state.market.rows || []).map((row) => {
    const clean = {
      timestamp: row.timestamp,
      symbol: row.symbol,
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume),
    };
    technicalNumberFields.forEach((field) => { clean[field] = numericOrNull(row[field]); });
    return clean;
  }).filter((row) => [row.open, row.high, row.low, row.close, row.volume].every(Number.isFinite));
}

function drawCandlestickChart(canvas, rows) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const plot = getKlinePlot(canvas);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#101614";
  ctx.fillRect(0, 0, width, height);
  if (rows.length < 2) {
    drawCanvasText(ctx, width / 2, height / 2, "暂无K线数据");
    return;
  }
  const highs = rows.map((row) => row.high);
  const lows = rows.map((row) => row.low);
  const volumes = rows.map((row) => row.volume);
  const priceMin = Math.min(...lows);
  const priceMax = Math.max(...highs);
  const spread = priceMax - priceMin || 1;
  const maxVolume = Math.max(...volumes, 1);
  const priceToY = (price) => plot.priceBottom - ((price - priceMin) / spread) * (plot.priceBottom - plot.top);
  const step = (plot.right - plot.left) / rows.length;
  const candleWidth = Math.max(3, Math.min(12, step * 0.62));
  ctx.strokeStyle = "#24302c";
  ctx.lineWidth = 1;
  ctx.font = "12px Inter, sans-serif";
  ctx.fillStyle = "#9ba6a0";
  for (let index = 0; index <= 5; index += 1) {
    const y = plot.top + ((plot.priceBottom - plot.top) / 5) * index;
    const price = priceMax - (spread / 5) * index;
    ctx.beginPath();
    ctx.moveTo(plot.left, y);
    ctx.lineTo(plot.right, y);
    ctx.stroke();
    ctx.fillText(formatNumber(price, 2), plot.right + 8, y + 4);
  }
  rows.forEach((row, index) => {
    const x = plot.left + index * step + step / 2;
    const color = row.close >= row.open ? "#22ab94" : "#f23645";
    const openY = priceToY(row.open);
    const closeY = priceToY(row.close);
    const highY = priceToY(row.high);
    const lowY = priceToY(row.low);
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1, Math.abs(openY - closeY));
    const volumeHeight = (row.volume / maxVolume) * (plot.bottom - plot.volumeTop);
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    ctx.globalAlpha = 0.42;
    ctx.fillRect(x - candleWidth / 2, plot.bottom - volumeHeight, candleWidth, volumeHeight);
    ctx.globalAlpha = 1;
  });
  drawOverlayLine(ctx, rows, "ema_20", "#4db6ff", priceToY, plot, step);
  drawOverlayLine(ctx, rows, "ema_50", "#f4b942", priceToY, plot, step);
  drawOverlayLine(ctx, rows, "ema_200", "#8f9ba3", priceToY, plot, step);
  drawOverlayLine(ctx, rows, "vwap_20", "#b9e769", priceToY, plot, step);
  drawOverlayLine(ctx, rows, "bb_upper_20", "#7f8f86", priceToY, plot, step, [3, 4]);
  drawOverlayLine(ctx, rows, "bb_lower_20", "#7f8f86", priceToY, plot, step, [3, 4]);
  drawSupertrendLine(ctx, rows, priceToY, plot, step);
  drawOverlayLegend(ctx, plot);

  ctx.strokeStyle = "#2f3b37";
  ctx.beginPath();
  ctx.moveTo(plot.left, plot.volumeTop);
  ctx.lineTo(plot.right, plot.volumeTop);
  ctx.stroke();
  if (state.market.hoverIndex !== null && rows[state.market.hoverIndex]) {
    const x = plot.left + state.market.hoverIndex * step + step / 2;
    const y = priceToY(rows[state.market.hoverIndex].close);
    ctx.strokeStyle = "#d7dfd9";
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, plot.top);
    ctx.lineTo(x, plot.bottom);
    ctx.moveTo(plot.left, y);
    ctx.lineTo(plot.right, y);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#d7dfd9";
  ctx.font = "13px Inter, sans-serif";
  ctx.fillText(`${rows[0].timestamp}  →  ${rows[rows.length - 1].timestamp}`, plot.left, height - 10);
}

function drawOverlayLine(ctx, rows, field, color, priceToY, plot, step, dash = []) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  ctx.setLineDash(dash);
  ctx.beginPath();
  let started = false;
  rows.forEach((row, index) => {
    const value = numericOrNull(row[field]);
    if (value === null) {
      started = false;
      return;
    }
    const x = plot.left + index * step + step / 2;
    const y = priceToY(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.restore();
}

function drawSupertrendLine(ctx, rows, priceToY, plot, step) {
  ctx.save();
  ctx.lineWidth = 1.8;
  for (let index = 1; index < rows.length; index += 1) {
    const previous = rows[index - 1];
    const row = rows[index];
    const previousValue = numericOrNull(previous.supertrend_10_3);
    const value = numericOrNull(row.supertrend_10_3);
    if (previousValue === null || value === null) continue;
    ctx.strokeStyle = Number(row.supertrend_direction_10_3) >= 0 ? "#22ab94" : "#f23645";
    ctx.beginPath();
    ctx.moveTo(plot.left + (index - 1) * step + step / 2, priceToY(previousValue));
    ctx.lineTo(plot.left + index * step + step / 2, priceToY(value));
    ctx.stroke();
  }
  ctx.restore();
}

function drawOverlayLegend(ctx, plot) {
  const items = [["EMA20", "#4db6ff"], ["EMA50", "#f4b942"], ["EMA200", "#8f9ba3"], ["VWAP", "#b9e769"], ["BB", "#7f8f86"], ["SuperTrend", "#22ab94"]];
  ctx.save();
  ctx.font = "12px Inter, sans-serif";
  let x = plot.left;
  items.forEach(([label, color]) => {
    ctx.fillStyle = color;
    ctx.fillRect(x, plot.top - 18, 10, 3);
    ctx.fillStyle = "#d7dfd9";
    ctx.fillText(label, x + 14, plot.top - 13);
    x += 74;
  });
  ctx.restore();
}

function drawEquityCurve() {
  const rows = state.snapshot?.equityCurve || [];
  byId("equityLabel").textContent = rows.length ? `${rows.length} 个点` : "暂无运行结果";
  drawLineChart(byId("equityCanvas"), rows.map((row) => Number(row.equityCurve)).filter(Number.isFinite), "#1f766d", "暂无权益数据");
}

function drawFactorScores() {
  const rows = (state.snapshot?.topFactors || state.snapshot?.summary?.factor_backtests || []).slice(0, 8);
  drawBarChart(byId("factorCanvas"), rows.map((row) => ({ label: row.factor, value: Math.abs(Number(row.score || row.sharpe) || 0) })), "#9b6500");
}

function drawLineChart(canvas, values, color, emptyText) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  drawChartFrame(ctx, width, height);
  if (values.length < 2) {
    drawCanvasText(ctx, width / 2, height / 2, emptyText);
    return;
  }
  const padding = 34;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const spread = max - min || 1;
  ctx.strokeStyle = color;
  ctx.lineWidth = 3;
  ctx.beginPath();
  values.forEach((value, index) => {
    const x = padding + (index / (values.length - 1)) * (width - padding * 2);
    const y = height - padding - ((value - min) / spread) * (height - padding * 2);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawBarChart(canvas, rows, color) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  drawChartFrame(ctx, width, height);
  if (!rows.length) {
    drawCanvasText(ctx, width / 2, height / 2, "暂无因子数据");
    return;
  }
  const padding = 34;
  const max = Math.max(...rows.map((row) => row.value), 1);
  const gap = 8;
  const barHeight = Math.max(18, (height - padding * 2 - gap * (rows.length - 1)) / rows.length);
  ctx.font = "12px Inter, sans-serif";
  rows.forEach((row, index) => {
    const y = padding + index * (barHeight + gap);
    const barWidth = ((width - padding * 2) * row.value) / max;
    ctx.fillStyle = color;
    ctx.fillRect(padding, y, barWidth, barHeight);
    ctx.fillStyle = "#202421";
    ctx.fillText(String(row.label).slice(0, 24), padding + 6, y + barHeight / 2 + 4);
  });
}

function drawChartFrame(ctx, width, height) {
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = "#d9ded8";
  ctx.strokeRect(0.5, 0.5, width - 1, height - 1);
}

function drawCanvasText(ctx, x, y, text) {
  ctx.fillStyle = "#9ba6a0";
  ctx.font = "14px Inter, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(text, x, y);
  ctx.textAlign = "left";
}

function getKlinePlot(canvas) {
  return { left: 58, right: canvas.width - 78, top: 28, priceBottom: Math.round(canvas.height * 0.72), volumeTop: Math.round(canvas.height * 0.78), bottom: canvas.height - 34 };
}

function setActiveView(view) {
  state.activeView = view;
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("is-active", section.id === `${view}View`));
  const titles = { overview: "总览", realtime: "实时流", market: "K线", config: "参数", data: "数据", signals: "信号", decisions: "决策", results: "验证", history: "运行" };
  byId("viewTitle").textContent = titles[view] || "总览";
}

function applyRuntimeMode() {
  document.body.classList.toggle("is-static-site", runtime.staticSite);
  updateApiSettingsButton();
  if (!runtime.staticSite) return;
  byId("saveButton").title = "静态网页版不能保存配置，需要连接后端 API";
  byId("runButton").title = "静态网页版不能启动流水线，需要连接后端 API";
  byId("cancelJobButton").title = "静态网页版没有后台任务";
  const realtimeStart = byId("startRealtimeButton");
  const realtimeStop = byId("stopRealtimeButton");
  if (realtimeStart) realtimeStart.title = "静态网页版不能启动 WebSocket 实时流";
  if (realtimeStop) realtimeStop.title = "静态网页版没有实时流服务";
}

function updateApiSettingsButton() {
  const button = byId("apiSettingsButton");
  if (!button) return;
  button.textContent = runtime.apiBaseUrl ? "切换 API" : "连接 API";
  button.title = runtime.apiBaseUrl || "设置 HTTPS 后端 API 地址";
}

function configureApiBaseUrl() {
  const current = runtime.apiBaseUrl || "";
  const raw = window.prompt("输入后端 API 地址；留空回到静态快照模式", current);
  if (raw === null) return;
  const trimmed = raw.trim();
  if (!trimmed) {
    clearStoredApiBaseUrl();
    window.location.reload();
    return;
  }
  const normalized = normalizeApiBaseUrl(trimmed);
  if (!normalized) {
    toast("API 地址必须以 http:// 或 https:// 开头", true);
    return;
  }
  if (window.location.protocol === "https:" && new URL(normalized).protocol === "http:") {
    toast("GitHub Pages 页面需要连接 HTTPS API", true);
    return;
  }
  storeApiBaseUrl(normalized);
  window.location.reload();
}

async function fetchJson(url, options = {}) {
  const { __retriedAuth, ...fetchOptions } = options;
  const targetUrl = resolveApiUrl(url);
  if (runtime.staticSite && (fetchOptions.method || "GET").toUpperCase() !== "GET") {
    throw new Error("静态网页版只支持读取快照数据");
  }
  const headers = { "Content-Type": "application/json", ...(fetchOptions.headers || {}) };
  const token = sessionStorage.getItem("qflAdminToken");
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(targetUrl, { ...fetchOptions, headers });
  const payload = await response.json();
  if (response.status === 401 && !__retriedAuth) {
    const prompted = window.prompt("请输入后台访问令牌");
    if (prompted) {
      sessionStorage.setItem("qflAdminToken", prompted.trim());
      return fetchJson(url, { ...fetchOptions, __retriedAuth: true });
    }
  }
  if (!response.ok) throw new Error(payload?.error?.message || `请求失败 ${response.status}`);
  return payload;
}

function resolveApiUrl(url) {
  if (runtime.staticSite) return staticEndpointFor(url);
  if (!runtime.apiBaseUrl) return url;
  return new URL(String(url).replace(/^\/+/, ""), ensureTrailingSlash(runtime.apiBaseUrl)).toString();
}

function staticEndpointFor(url) {
  const parsed = new URL(url, window.location.href);
  const map = {
    "/api/config": "api/config.json",
    "/api/summary": "api/summary.json",
    "/api/realtime": "api/realtime.json",
    "/api/runs": "api/runs.json",
    "/api/market": "api/market.json",
    "/api/health": "api/health.json",
  };
  const endpoint = map[parsed.pathname];
  if (!endpoint) throw new Error(`静态快照缺少接口：${parsed.pathname}`);
  return endpoint;
}

function artifactHref(name) {
  const safeName = encodeURIComponent(name);
  return runtime.staticSite ? `artifacts/${safeName}` : resolveApiUrl(`/api/artifact?name=${safeName}`);
}

function getConfiguredApiBaseUrl() {
  const queryValue = apiBaseUrlFromQuery();
  if (queryValue !== null) return queryValue;
  const configured = normalizeApiBaseUrl(window.QFL_API_BASE_URL || "");
  if (configured) return configured;
  return normalizeApiBaseUrl(readStoredApiBaseUrl());
}

function apiBaseUrlFromQuery() {
  const params = new URLSearchParams(window.location.search);
  if (!params.has("apiBaseUrl")) return null;
  const normalized = normalizeApiBaseUrl(params.get("apiBaseUrl") || "");
  if (normalized) storeApiBaseUrl(normalized);
  else clearStoredApiBaseUrl();
  return normalized;
}

function normalizeApiBaseUrl(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  try {
    const parsed = new URL(trimmed);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    if (window.location.protocol === "https:" && parsed.protocol === "http:") return "";
    return parsed.toString().replace(/\/$/, "");
  } catch (_) {
    return "";
  }
}

function readStoredApiBaseUrl() {
  try {
    return localStorage.getItem(API_BASE_STORAGE_KEY) || "";
  } catch (_) {
    return "";
  }
}

function storeApiBaseUrl(value) {
  try {
    localStorage.setItem(API_BASE_STORAGE_KEY, value);
  } catch (_) {
  }
}

function clearStoredApiBaseUrl() {
  try {
    localStorage.removeItem(API_BASE_STORAGE_KEY);
  } catch (_) {
  }
}

function ensureTrailingSlash(value) {
  return value.endsWith("/") ? value : `${value}/`;
}

function withDefaults(config) {
  config.data ||= {};
  config.data.universe ||= [];
  config.data.adjustments ||= [];
  config.mining ||= {};
  config.evaluation ||= {};
  config.derivatives ||= {};
  config.microstructure ||= {};
  config.onchain ||= {};
  config.validation ||= {};
  config.realtime ||= {};
  config.backtest ||= {};
  return config;
}

function parseInputValue(input) {
  if (input.dataset.type === "boolean") return input.checked;
  if (input.dataset.type === "number") return input.value === "" ? null : Number(input.value);
  if (input.dataset.type === "csv_numbers") return input.value.split(",").map((value) => value.trim()).filter(Boolean).map(Number);
  return input.value;
}

function getPath(object, path) {
  return path.split(".").reduce((current, key) => current?.[key], object);
}

function setPath(object, path, value) {
  const parts = path.split(".");
  let current = object;
  parts.slice(0, -1).forEach((key) => {
    current[key] ||= {};
    current = current[key];
  });
  current[parts.at(-1)] = value;
}

function sourceStatusMeta(value) {
  const map = { OK: ["可用", "is-ok"], PASS: ["通过", "is-ok"], WARN: ["警告", "is-warn"], STALE: ["过期", "is-stale"], MISSING: ["缺失", "is-missing"], SIMULATED: ["模拟", "is-simulated"], SKIPPED: ["跳过", "is-skipped"] };
  const item = map[value] || [value || "未知", "is-skipped"];
  return { label: item[0], className: item[1] };
}

function qualityMeta(value) {
  const map = { PASS: ["通过", "is-pass"], WARN: ["警告", "is-warn"], FAIL: ["失败", "is-fail"] };
  const item = map[value] || [value || "未知", "is-warn"];
  return { label: item[0], className: item[1] };
}

function signalMeta(value) {
  const map = { BULLISH: ["偏多", "is-bullish"], BEARISH: ["偏空", "is-bearish"], NEUTRAL: ["中性", "is-neutral"] };
  const item = map[value] || [value || "-", "is-neutral"];
  return { label: item[0], className: item[1] };
}

function stanceMeta(value) {
  const map = { LEAN_LONG: ["偏多", "is-bullish"], WATCH_LONG: ["偏多等待", "is-watch-long"], LEAN_SHORT: ["偏空", "is-bearish"], WATCH_SHORT: ["偏空等待", "is-watch-short"], NEUTRAL: ["观望", "is-neutral"] };
  const item = map[value] || [value || "-", "is-neutral"];
  return { label: item[0], className: item[1] };
}

function runStatusMeta(value) {
  const map = { queued: ["排队", "is-queued"], running: ["运行中", "is-running"], canceling: ["取消中", "is-running"], canceled: ["已取消", "is-failed"], succeeded: ["成功", "is-succeeded"], failed: ["失败", "is-failed"] };
  const item = map[value] || [value || "-", "is-queued"];
  return { label: item[0], className: item[1] };
}

function realtimeMeta(value) {
  const map = { stopped: "未启动", starting: "启动中", running: "运行中", reconnecting: "重连中", error: "错误" };
  return { label: map[value] || value || "未知" };
}

function markDirty() {
  state.dirty = true;
  const editor = byId("jsonEditor");
  if (document.activeElement !== editor) editor.value = JSON.stringify(state.config, null, 2);
}

function setBusy(isBusy, label = "") {
  ["reloadButton", "saveButton", "runButton"].forEach((id) => { byId(id).disabled = isBusy; });
  if (label) setServerState(label, true);
}

function setServerState(text, ok) {
  byId("serverState").textContent = text;
  document.querySelector(".status-dot").classList.toggle("is-ok", ok);
}

function toast(message, isError = false) {
  const node = byId("toast");
  node.textContent = message;
  node.classList.toggle("text-danger", isError);
  node.classList.add("is-visible");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("is-visible"), 3200);
}

function fillSelect(id, values) {
  byId(id).innerHTML = values.map((value) => `<option value="${value}">${labels[value] || value}</option>`).join("");
}

function selectHtml(values, selected, attrs) {
  return `<select ${attrs}>${values.map((value) => `<option value="${value}" ${value === selected ? "selected" : ""}>${labels[value] || value}</option>`).join("")}</select>`;
}

function emptyRow(colspan, text) {
  return `<tr><td class="empty-row" colspan="${colspan}">${escapeHtml(text)}</td></tr>`;
}

function byId(id) {
  return document.getElementById(id);
}

function sortedUnique(values) {
  return [...new Set(values.filter(Boolean).map(String))].sort();
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "暂无";
  const number = Number(value);
  if (!Number.isFinite(number)) return "暂无";
  return number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "暂无";
  const number = Number(value);
  if (!Number.isFinite(number)) return "暂无";
  return `${(number * 100).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

function formatUsd(value) {
  if (value === null || value === undefined || value === "") return "暂无";
  const number = Number(value);
  if (!Number.isFinite(number)) return "暂无";
  if (Math.abs(number) >= 1_000_000_000) return `$${(number / 1_000_000_000).toFixed(2)}B`;
  if (Math.abs(number) >= 1_000_000) return `$${(number / 1_000_000).toFixed(1)}M`;
  return `$${number.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
}

function formatFreshness(minutes) {
  const number = Number(minutes);
  if (!Number.isFinite(number)) return "-";
  if (number < 90) return `${number.toFixed(0)}分钟`;
  const hours = number / 60;
  if (hours < 72) return `${hours.toFixed(1)}小时`;
  return `${(hours / 24).toFixed(1)}天`;
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function shortDate(value) {
  if (!value) return "";
  return String(value).slice(0, 10);
}

function formatBytes(value) {
  const number = Number(value) || 0;
  if (number < 1024) return `${number} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${(number / 1024 / 1024).toFixed(1)} MB`;
}

function addDays(date, days) {
  const result = new Date(date.getTime());
  result.setDate(result.getDate() + days);
  return result;
}

function toDateInput(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}
