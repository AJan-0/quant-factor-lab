const DEFAULT_SYMBOLS = ["ETH-USD", "BTC-USD"];
const DEFAULT_CHANNELS = ["books5", "trades"];
const MAX_SYMBOLS = 8;
const MAX_EVENTS = 120;
const ALLOWED_CHANNELS = new Set(["books", "books5", "bbo-tbt", "trades"]);

export function normalizeRealtimeRequest(searchParams = new URLSearchParams()) {
  const symbols = normalizeSymbols(searchParams.getAll("symbols").concat(searchParams.get("symbol") || []));
  const channels = normalizeChannels(searchParams.getAll("channels").concat(searchParams.get("channel") || []));
  const liquidationsEnabled = parseBoolean(
    searchParams.get("liquidations") ?? searchParams.get("liquidations_enabled"),
    true,
  );
  const args = buildOkxPublicSubscriptions({ symbols, channels, liquidationsEnabled });
  return { symbols, channels, liquidationsEnabled, args };
}

export function normalizeSymbols(value) {
  const symbols = splitValues(value)
    .map((item) => item.trim().toUpperCase().replace(/\//g, "-"))
    .filter(Boolean);
  return unique(symbols.length ? symbols : DEFAULT_SYMBOLS).slice(0, MAX_SYMBOLS);
}

export function normalizeChannels(value) {
  const channels = splitValues(value)
    .map((item) => item.trim())
    .filter((item) => ALLOWED_CHANNELS.has(item));
  return unique(channels.length ? channels : DEFAULT_CHANNELS);
}

export function buildOkxPublicSubscriptions({ symbols, channels = DEFAULT_CHANNELS, liquidationsEnabled = true }) {
  const args = [];
  const seen = new Set();
  for (const symbol of normalizeSymbols(symbols)) {
    const spotInstId = okxSpotInstId(symbol);
    for (const channel of normalizeChannels(channels)) {
      const key = `${channel}:${spotInstId}`;
      if (!seen.has(key)) {
        args.push({ channel, instId: spotInstId });
        seen.add(key);
      }
    }
    if (liquidationsEnabled) {
      const swapInstId = okxSwapInstId(symbol);
      const key = `liquidation-orders:${swapInstId}`;
      if (!seen.has(key)) {
        args.push({ channel: "liquidation-orders", instType: "SWAP", instId: swapInstId });
        seen.add(key);
      }
    }
  }
  return args;
}

export function okxSpotInstId(symbol) {
  const normalized = String(symbol || "").trim().toUpperCase().replace(/\//g, "-");
  if (!normalized) return "";
  if (normalized.endsWith("-USD")) return `${normalized.slice(0, -4)}-USDT`;
  if (normalized.includes("-")) return normalized;
  return `${normalized}-USDT`;
}

export function okxSwapInstId(symbol) {
  const normalized = String(symbol || "").trim().toUpperCase().replace(/\//g, "-");
  if (!normalized) return "";
  if (normalized.endsWith("-SWAP")) return normalized;
  if (normalized.endsWith("-USD")) return `${normalized.slice(0, -4)}-USDT-SWAP`;
  if (normalized.endsWith("-USDT")) return `${normalized}-SWAP`;
  return `${normalized}-USDT-SWAP`;
}

export function createRealtimeState(subscribedArgs = []) {
  return {
    status: "starting",
    error: null,
    startedAt: formatTimestamp(Date.now()),
    stoppedAt: null,
    messageCount: 0,
    subscribedArgs: [...subscribedArgs],
    orderBooks: {},
    trades: [],
    liquidations: [],
    events: [eventRow("INFO", "Vercel WebSocket realtime stream starting")],
    transport: "websocket",
  };
}

export function handleOkxMessage(state, rawMessage) {
  const message = String(rawMessage);
  if (message === "pong") {
    appendEvent(state, "DEBUG", "OKX pong");
    return 0;
  }

  let payload;
  try {
    payload = JSON.parse(message);
  } catch (_) {
    appendEvent(state, "WARN", `Unparseable OKX message: ${message.slice(0, 120)}`);
    return 0;
  }

  if (payload.event) {
    const level = payload.event === "error" ? "ERROR" : "INFO";
    appendEvent(state, level, payload.msg || payload.event);
    if (level === "ERROR") {
      state.status = "error";
      state.error = payload.msg || payload.event;
    }
    return 0;
  }

  const arg = payload.arg || {};
  const channel = String(arg.channel || "");
  const instId = String(arg.instId || "");
  let count = 0;
  for (const item of payload.data || []) {
    if (["books", "books5", "bbo-tbt"].includes(channel)) {
      const row = bookRow(instId, item);
      state.orderBooks[row.inst_id] = row;
      count += 1;
    } else if (channel === "trades") {
      state.trades.unshift(tradeRow(instId, item));
      trimFront(state.trades);
      count += 1;
    } else if (channel === "liquidation-orders") {
      for (const row of liquidationRows(arg, item)) {
        state.liquidations.unshift(row);
        trimFront(state.liquidations);
        count += 1;
      }
    }
  }
  state.messageCount += count;
  return count;
}

export function appendEvent(state, level, message) {
  state.events.push(eventRow(level, message));
  trimBack(state.events);
}

export function snapshotState(state) {
  return {
    status: state.status,
    error: state.error,
    startedAt: state.startedAt,
    stoppedAt: state.stoppedAt,
    messageCount: state.messageCount,
    subscribedArgs: state.subscribedArgs,
    orderBooks: Object.values(state.orderBooks),
    trades: state.trades.slice(0, MAX_EVENTS),
    liquidations: state.liquidations.slice(0, MAX_EVENTS),
    events: state.events.slice(-MAX_EVENTS),
    transport: state.transport || "websocket",
  };
}

function bookRow(instId, item) {
  const bids = levels(item.bids);
  const asks = levels(item.asks);
  const bestBid = bids.length ? bids[0][0] : null;
  const bestAsk = asks.length ? asks[0][0] : null;
  const mid = bestBid !== null && bestAsk !== null ? (bestBid + bestAsk) / 2 : null;
  const spreadBps = mid ? ((bestAsk - bestBid) / mid) * 10000 : null;
  const bidDepth = bids.reduce((total, [price, size]) => total + price * size, 0);
  const askDepth = asks.reduce((total, [price, size]) => total + price * size, 0);
  return {
    timestamp: timestamp(item.ts),
    inst_id: instId,
    best_bid: bestBid,
    best_ask: bestAsk,
    mid_price: mid,
    spread_bps: spreadBps,
    bid_depth_usd: bidDepth,
    ask_depth_usd: askDepth,
    seq_id: item.seqId,
  };
}

function tradeRow(instId, item) {
  return {
    timestamp: timestamp(item.ts),
    inst_id: instId,
    price: safeNumber(item.px),
    size: safeNumber(item.sz),
    side: item.side,
    trade_id: item.tradeId,
  };
}

function liquidationRows(arg, item) {
  const details = Array.isArray(item.details) ? item.details : [item];
  return details.map((detail) => ({
    timestamp: timestamp(detail.ts || item.ts),
    inst_id: detail.instId || item.instId || arg.instId,
    inst_type: detail.instType || item.instType || arg.instType,
    side: detail.side || item.side,
    size: safeNumber(detail.sz || item.sz),
    bankruptcy_price: safeNumber(detail.bkPx || item.bkPx),
  }));
}

function levels(rawLevels) {
  const rows = [];
  for (const level of rawLevels || []) {
    if (!Array.isArray(level) || level.length < 2) continue;
    const price = safeNumber(level[0]);
    const size = safeNumber(level[1]);
    if (price !== null && size !== null) rows.push([price, size]);
  }
  return rows;
}

function timestamp(value) {
  const number = safeNumber(value);
  if (number === null) return null;
  const date = new Date(number);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().replace("T", " ").replace(/\.\d{3}Z$/, "");
}

function eventRow(level, message) {
  return { timestamp: formatTimestamp(Date.now()), level: String(level).toUpperCase(), message: String(message || "") };
}

function formatTimestamp(value) {
  return new Date(value).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function safeNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function parseBoolean(value, fallback) {
  if (value === null || value === undefined || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(value).trim().toLowerCase());
}

function splitValues(value) {
  const items = Array.isArray(value) ? value : [value];
  return items.flatMap((item) => String(item || "").split(/[,\s]+/));
}

function unique(values) {
  return [...new Set(values)];
}

function trimFront(values) {
  if (values.length > MAX_EVENTS) values.length = MAX_EVENTS;
}

function trimBack(values) {
  if (values.length > MAX_EVENTS) values.splice(0, values.length - MAX_EVENTS);
}
