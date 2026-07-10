import test from "node:test";
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { WebSocket, WebSocketServer } from "ws";

import {
  buildOkxPublicSubscriptions,
  createRealtimeState,
  handleOkxMessage,
  normalizeRealtimeRequest,
  okxSpotInstId,
  okxSwapInstId,
  snapshotState,
} from "../vercel/realtime-ws-core.js";

test("normalizes OKX spot and swap instrument ids", () => {
  assert.equal(okxSpotInstId("ETH-USD"), "ETH-USDT");
  assert.equal(okxSpotInstId("BTC/USDT"), "BTC-USDT");
  assert.equal(okxSwapInstId("ETH-USD"), "ETH-USDT-SWAP");
  assert.equal(okxSwapInstId("BTC-USDT-SWAP"), "BTC-USDT-SWAP");
});

test("builds de-duplicated public subscriptions for books, trades, and liquidations", () => {
  const args = buildOkxPublicSubscriptions({
    symbols: ["ETH-USD", "ETH-USD"],
    channels: ["books5", "trades", "unknown"],
    liquidationsEnabled: true,
  });

  assert.deepEqual(args, [
    { channel: "books5", instId: "ETH-USDT" },
    { channel: "trades", instId: "ETH-USDT" },
    { channel: "liquidation-orders", instType: "SWAP", instId: "ETH-USDT-SWAP" },
  ]);
});

test("parses query params into a bounded realtime request", () => {
  const params = new URLSearchParams("symbols=eth-usd,btc-usd,aapl&channels=trades,books5,bad&liquidations=false");
  const request = normalizeRealtimeRequest(params);

  assert.deepEqual(request.symbols, ["ETH-USD", "BTC-USD", "AAPL"]);
  assert.deepEqual(request.channels, ["trades", "books5"]);
  assert.equal(request.liquidationsEnabled, false);
  assert.equal(request.args.length, 6);
});

test("handles OKX book, trade, and liquidation messages", () => {
  const state = createRealtimeState([{ channel: "books5", instId: "ETH-USDT" }]);

  handleOkxMessage(
    state,
    JSON.stringify({
      arg: { channel: "books5", instId: "ETH-USDT" },
      data: [{ ts: "1640995200000", bids: [["99", "2"]], asks: [["101", "3"]], seqId: 10 }],
    }),
  );
  handleOkxMessage(
    state,
    JSON.stringify({
      arg: { channel: "trades", instId: "ETH-USDT" },
      data: [{ ts: "1640995201000", px: "100.5", sz: "0.7", side: "buy", tradeId: "1" }],
    }),
  );
  handleOkxMessage(
    state,
    JSON.stringify({
      arg: { channel: "liquidation-orders", instType: "SWAP", instId: "ETH-USDT-SWAP" },
      data: [{ details: [{ ts: "1640995202000", instId: "ETH-USDT-SWAP", side: "sell", sz: "2", bkPx: "98" }] }],
    }),
  );

  const snapshot = snapshotState(state);
  assert.equal(snapshot.messageCount, 3);
  assert.equal(snapshot.orderBooks[0].spread_bps, 200);
  assert.equal(snapshot.trades[0].side, "buy");
  assert.equal(snapshot.liquidations[0].inst_id, "ETH-USDT-SWAP");
  assert.equal(snapshot.transport, "websocket");
});

test("Vercel WebSocket endpoint bridges fake OKX messages to the browser client", async (t) => {
  const fakeOkx = new WebSocketServer({ port: 0, host: "127.0.0.1" });
  const fakeOkxPort = await listeningPort(fakeOkx);
  const upstreamSubscriptions = [];

  fakeOkx.on("connection", (ws) => {
    ws.on("message", (data) => {
      const payload = JSON.parse(data.toString());
      upstreamSubscriptions.push(payload);
      ws.send(JSON.stringify({ event: "subscribe", arg: payload.args?.[0], connId: "test" }));
      ws.send(
        JSON.stringify({
          arg: { channel: "trades", instId: "ETH-USDT" },
          data: [{ ts: "1640995201000", px: "100.5", sz: "0.7", side: "buy", tradeId: "1" }],
        }),
      );
    });
  });

  process.env.QFL_OKX_PUBLIC_WS_URL = `ws://127.0.0.1:${fakeOkxPort}`;
  process.env.QFL_WS_SNAPSHOT_THROTTLE_MS = "5";
  process.env.QFL_WS_HEARTBEAT_MS = "10000";
  process.env.QFL_WS_UPSTREAM_PING_MS = "10000";

  const moduleUrl = pathToFileURL(resolve("api/realtime-ws.js"));
  moduleUrl.search = `test=${Date.now()}`;
  const { default: server } = await import(moduleUrl.href);
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  const serverPort = server.address().port;

  t.after(() => {
    delete process.env.QFL_OKX_PUBLIC_WS_URL;
    delete process.env.QFL_WS_SNAPSHOT_THROTTLE_MS;
    delete process.env.QFL_WS_HEARTBEAT_MS;
    delete process.env.QFL_WS_UPSTREAM_PING_MS;
    server.close();
    fakeOkx.close();
  });

  const client = new WebSocket(`ws://127.0.0.1:${serverPort}/ws/realtime?symbols=ETH-USD&channels=trades&liquidations=false`);
  t.after(() => client.close());
  const snapshot = await waitForPayload(client, (payload) => payload.trades?.length === 1);

  assert.deepEqual(upstreamSubscriptions[0], {
    op: "subscribe",
    args: [{ channel: "trades", instId: "ETH-USDT" }],
  });
  assert.equal(snapshot.status, "running");
  assert.equal(snapshot.trades[0].price, 100.5);
  assert.equal(snapshot.transport, "websocket");
});

function listeningPort(server) {
  const address = server.address();
  if (address && typeof address === "object") return Promise.resolve(address.port);
  return new Promise((resolvePort) => {
    server.on("listening", () => {
      const current = server.address();
      resolvePort(typeof current === "object" && current ? current.port : 0);
    });
  });
}

function waitForPayload(client, predicate) {
  return new Promise((resolvePayload, reject) => {
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("Timed out waiting for realtime WebSocket payload"));
    }, 2000);
    const onMessage = (data) => {
      const message = JSON.parse(data.toString());
      const payload = message.payload;
      if (payload && predicate(payload)) {
        cleanup();
        resolvePayload(payload);
      }
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    const cleanup = () => {
      clearTimeout(timer);
      client.off("message", onMessage);
      client.off("error", onError);
    };
    client.on("message", onMessage);
    client.on("error", onError);
  });
}
