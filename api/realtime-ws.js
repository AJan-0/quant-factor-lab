import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { WebSocket, WebSocketServer } from "ws";
import {
  appendEvent,
  createRealtimeState,
  handleOkxMessage,
  normalizeRealtimeRequest,
  snapshotState,
} from "../vercel/realtime-ws-core.js";

export const config = {
  maxDuration: 300,
};

const OKX_PUBLIC_WS_URL = process.env.QFL_OKX_PUBLIC_WS_URL || "wss://ws.okx.com:8443/ws/v5/public";
const SNAPSHOT_THROTTLE_MS = numberFromEnv("QFL_WS_SNAPSHOT_THROTTLE_MS", 250);
const HEARTBEAT_MS = numberFromEnv("QFL_WS_HEARTBEAT_MS", 15000);
const UPSTREAM_PING_MS = numberFromEnv("QFL_WS_UPSTREAM_PING_MS", 20000);
const RECONNECT_MAX_MS = numberFromEnv("QFL_WS_RECONNECT_MAX_MS", 15000);

const server = createServer((request, response) => {
  response.writeHead(200, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(JSON.stringify({ status: "ok", mode: "vercel-websocket", endpoint: "/ws/realtime" }));
});

const wss = new WebSocketServer({ server });

wss.on("connection", (client, request) => {
  if (!originAllowed(request)) {
    client.close(1008, "Origin not allowed");
    return;
  }
  if (!authorized(request)) {
    client.close(1008, "Admin token is required");
    return;
  }
  bridgeOkxRealtime(client, request);
});

export default server;

function bridgeOkxRealtime(client, request) {
  const url = new URL(request.url || "/ws/realtime", "http://localhost");
  const realtimeRequest = normalizeRealtimeRequest(url.searchParams);
  const state = createRealtimeState(realtimeRequest.args);
  let upstream = null;
  let closed = false;
  let reconnectDelay = 1000;
  let reconnectTimer = null;
  let flushTimer = null;

  const heartbeatTimer = setInterval(() => {
    sendSnapshot("heartbeat");
    if (client.readyState === WebSocket.OPEN) client.ping();
  }, HEARTBEAT_MS);

  const upstreamPingTimer = setInterval(() => {
    if (upstream?.readyState === WebSocket.OPEN) upstream.ping();
  }, UPSTREAM_PING_MS);

  client.on("message", (data) => {
    const message = parseClientMessage(data);
    if (message?.type === "ping") {
      sendJson({ type: "pong", ts: new Date().toISOString() });
    } else if (message?.type === "snapshot") {
      sendSnapshot("snapshot");
    }
  });
  client.on("close", cleanup);
  client.on("error", cleanup);

  appendEvent(state, "INFO", `Subscribing ${realtimeRequest.args.length} OKX public channels`);
  sendSnapshot("snapshot");
  connectUpstream();

  function connectUpstream() {
    if (closed) return;
    state.status = "starting";
    state.error = null;
    appendEvent(state, "INFO", "Connecting to OKX public WebSocket");
    sendSnapshot("snapshot");

    upstream = new WebSocket(OKX_PUBLIC_WS_URL, {
      handshakeTimeout: 10000,
      perMessageDeflate: false,
    });

    upstream.on("open", () => {
      if (closed || upstream?.readyState !== WebSocket.OPEN) return;
      reconnectDelay = 1000;
      state.status = "running";
      state.error = null;
      appendEvent(state, "INFO", "OKX WebSocket connected");
      upstream.send(JSON.stringify({ op: "subscribe", args: realtimeRequest.args }));
      sendSnapshot("snapshot");
    });

    upstream.on("message", (data) => {
      handleOkxMessage(state, data.toString());
      scheduleSnapshot();
    });

    upstream.on("pong", () => {
      appendEvent(state, "DEBUG", "OKX pong");
    });

    upstream.on("error", (error) => {
      state.error = error.message;
      appendEvent(state, "ERROR", `OKX WebSocket error: ${error.message}`);
      scheduleSnapshot(0);
    });

    upstream.on("close", (code, reason) => {
      if (closed) return;
      state.status = "starting";
      appendEvent(state, "WARN", `OKX WebSocket closed: ${code || ""} ${reason || ""}`.trim());
      scheduleSnapshot(0);
      const delay = reconnectDelay;
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
      reconnectTimer = setTimeout(connectUpstream, delay);
    });
  }

  function scheduleSnapshot(delay = SNAPSHOT_THROTTLE_MS) {
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      sendSnapshot("realtime");
    }, delay);
  }

  function sendSnapshot(type) {
    sendJson({ type, payload: snapshotState(state) });
  }

  function sendJson(payload) {
    if (client.readyState !== WebSocket.OPEN) return;
    client.send(JSON.stringify(payload));
  }

  function cleanup() {
    if (closed) return;
    closed = true;
    clearInterval(heartbeatTimer);
    clearInterval(upstreamPingTimer);
    clearTimeout(reconnectTimer);
    clearTimeout(flushTimer);
    if (upstream && upstream.readyState < WebSocket.CLOSING) {
      upstream.close(1000, "Client disconnected");
    }
  }
}

function parseClientMessage(data) {
  try {
    return JSON.parse(String(data));
  } catch (_) {
    return null;
  }
}

function authorized(request) {
  const token = process.env.QUANT_FACTOR_ADMIN_TOKEN || "";
  if (!token) return true;
  const url = new URL(request.url || "/ws/realtime", "http://localhost");
  return secureCompare(url.searchParams.get("token") || "", token);
}

function originAllowed(request) {
  const origin = String(request.headers.origin || "").replace(/\/$/, "");
  if (!origin) return true;
  const host = request.headers.host ? `https://${request.headers.host}` : "";
  if (origin === host) return true;
  const allowed = String(process.env.QUANT_FACTOR_ADMIN_CORS_ORIGINS || "")
    .split(",")
    .map((item) => item.trim().replace(/\/$/, ""))
    .filter(Boolean);
  return allowed.length === 0 || allowed.includes("*") || allowed.includes(origin);
}

function secureCompare(candidate, expected) {
  const candidateBuffer = Buffer.from(String(candidate));
  const expectedBuffer = Buffer.from(String(expected));
  return candidateBuffer.length === expectedBuffer.length && timingSafeEqual(candidateBuffer, expectedBuffer);
}

function numberFromEnv(name, fallback) {
  const number = Number(process.env[name]);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}
