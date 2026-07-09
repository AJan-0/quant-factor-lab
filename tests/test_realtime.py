from __future__ import annotations

import json
import unittest

import pandas as pd

from quant_factor_lab.realtime import OKXRealtimeService, RealtimeEventStore, build_okx_public_subscriptions
from quant_factor_lab.types import AssetClass, DataFrequency, DataRequest, Instrument


class RealtimeTests(unittest.TestCase):
    def test_builds_okx_public_subscriptions_for_books_trades_and_liquidations(self) -> None:
        request = DataRequest(
            universe=(Instrument("ETH-USD", AssetClass.CRYPTO), Instrument("AAPL", AssetClass.EQUITY)),
            start=pd.Timestamp("2026-07-01"),
            end=pd.Timestamp("2026-07-02"),
            frequency=DataFrequency.MINUTE_1,
        )

        args = build_okx_public_subscriptions(request, {"channels": ["books5", "trades"], "liquidations_enabled": True})

        self.assertIn({"channel": "books5", "instId": "ETH-USDT"}, args)
        self.assertIn({"channel": "trades", "instId": "ETH-USDT"}, args)
        self.assertIn({"channel": "liquidation-orders", "instType": "SWAP", "instId": "ETH-USDT-SWAP"}, args)
        self.assertFalse(any(item.get("instId") == "AAPL-USDT" for item in args))

    def test_realtime_service_parses_book_trade_and_liquidation_messages(self) -> None:
        store = RealtimeEventStore()
        service = OKXRealtimeService(store=store)

        service.handle_message(
            json.dumps(
                {
                    "arg": {"channel": "books5", "instId": "ETH-USDT"},
                    "data": [{"ts": "1640995200000", "bids": [["99", "2"]], "asks": [["101", "3"]], "seqId": 10}],
                }
            )
        )
        service.handle_message(
            json.dumps(
                {
                    "arg": {"channel": "trades", "instId": "ETH-USDT"},
                    "data": [{"ts": "1640995201000", "px": "100.5", "sz": "0.7", "side": "buy", "tradeId": "1"}],
                }
            )
        )
        service.handle_message(
            json.dumps(
                {
                    "arg": {"channel": "liquidation-orders", "instType": "SWAP", "instId": "ETH-USDT-SWAP"},
                    "data": [{"details": [{"ts": "1640995202000", "instId": "ETH-USDT-SWAP", "side": "sell", "sz": "2", "bkPx": "98"}]}],
                }
            )
        )

        snapshot = store.snapshot()
        self.assertEqual(snapshot["orderBooks"][0]["spread_bps"], 200.0)
        self.assertEqual(snapshot["trades"][0]["side"], "buy")
        self.assertEqual(snapshot["liquidations"][0]["inst_id"], "ETH-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
