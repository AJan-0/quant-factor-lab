from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quant_factor_lab.adjustments import apply_market_data_adjustments
from quant_factor_lab.adjustments import normalize_adjustments
from quant_factor_lab.backtest import BacktestConfig, run_rank_backtest
from quant_factor_lab.data import OKXMarketDataProvider, SyntheticMarketDataProvider, build_provider, normalize_market_frame
from quant_factor_lab.decisions import build_decision_cards
from quant_factor_lab.derivatives import OKXDerivativesHistoryProvider, OKXDerivativesSnapshotProvider
from quant_factor_lab.evaluation import evaluate_factor_panel, select_top_factor
from quant_factor_lab.factors import build_operator_factor_panel, compute_forward_returns
from quant_factor_lab.microstructure import OKXMicrostructureSnapshotProvider
from quant_factor_lab.onchain import CoinMetricsCommunityOnchainProvider, load_onchain_context
from quant_factor_lab.pipeline import PipelineRunner, _json_ready
from quant_factor_lab.quality import build_market_data_quality_report
from quant_factor_lab.signals import build_factor_signal_radar
from quant_factor_lab.source_health import build_source_health_report
from quant_factor_lab.technicals import build_indicator_state_table, build_technical_indicator_panel
from quant_factor_lab.types import AssetClass, DataFrequency, DataRequest, Instrument
from quant_factor_lab.validation import run_walk_forward_validation


def make_request() -> DataRequest:
    return DataRequest(
        universe=(
            Instrument("BTC-USD", AssetClass.CRYPTO),
            Instrument("ETH-USD", AssetClass.CRYPTO),
            Instrument("AAPL", AssetClass.EQUITY),
            Instrument("MSFT", AssetClass.EQUITY),
        ),
        start=pd.Timestamp("2022-01-01"),
        end=pd.Timestamp("2023-01-01"),
        frequency=DataFrequency.DAILY,
    )


class TypeParsingTests(unittest.TestCase):
    def test_frequency_aliases_parse_to_architecture_values(self) -> None:
        self.assertEqual(DataFrequency.parse("daily"), DataFrequency.DAILY)
        self.assertEqual(DataFrequency.parse("5min"), DataFrequency.MINUTE_5)


class DataAdjustmentTests(unittest.TestCase):
    def test_adjustments_must_be_a_list(self) -> None:
        with self.assertRaises(ValueError):
            normalize_adjustments({"symbol": "BTC-USD", "field": "close", "operation": "multiply", "value": 1})

    def test_price_adjustments_apply_to_selected_symbol_and_dates(self) -> None:
        data = SyntheticMarketDataProvider(seed=10).load(make_request())
        original = data[(data["symbol"] == "BTC-USD") & (data["timestamp"] == pd.Timestamp("2022-03-01"))].iloc[0]

        adjusted = apply_market_data_adjustments(
            data,
            [
                {
                    "symbol": "BTC-USD",
                    "field": "close",
                    "operation": "multiply",
                    "value": 1.1,
                    "start": "2022-03-01",
                    "end": "2022-03-01",
                }
            ],
        )

        changed = adjusted[
            (adjusted["symbol"] == "BTC-USD") & (adjusted["timestamp"] == pd.Timestamp("2022-03-01"))
        ].iloc[0]
        untouched = adjusted[
            (adjusted["symbol"] == "ETH-USD") & (adjusted["timestamp"] == pd.Timestamp("2022-03-01"))
        ].iloc[0]
        self.assertAlmostEqual(changed["close"], original["close"] * 1.1)
        self.assertGreaterEqual(changed["high"], changed["close"])
        self.assertNotEqual(untouched["close"], changed["close"])


class CorePipelineTests(unittest.TestCase):
    def test_synthetic_provider_outputs_normalized_market_data(self) -> None:
        data = SyntheticMarketDataProvider(seed=1).load(make_request())
        normalized = normalize_market_frame(data)
        self.assertEqual(len(data), len(normalized))
        self.assertEqual(set(normalized["symbol"].unique()), {"BTC-USD", "ETH-USD", "AAPL", "MSFT"})
        self.assertTrue((normalized["high"] >= normalized[["open", "close"]].max(axis=1)).all())
        self.assertTrue((normalized["low"] <= normalized[["open", "close"]].min(axis=1)).all())

    def test_json_ready_serializes_pandas_timestamps(self) -> None:
        payload = _json_ready({"timestamp": pd.Timestamp("2026-07-09 01:02:03")})

        self.assertEqual(payload["timestamp"], "2026-07-09 01:02:03")
        json.dumps(payload)

    def test_build_provider_supports_okx(self) -> None:
        provider = build_provider({"provider": "okx", "page_limit": 100, "max_pages": 2})

        self.assertIsInstance(provider, OKXMarketDataProvider)

    def test_okx_provider_parses_public_candles(self) -> None:
        payloads = [
            {
                "code": "0",
                "msg": "",
                "data": [
                    ["1640995200000", "3700", "3750", "3650", "3725", "10", "37250", "37250", "1"],
                    ["1641081600000", "3725", "3800", "3700", "3780", "12", "45360", "45360", "1"],
                ],
            }
        ]
        calls: list[tuple[str, dict[str, str]]] = []

        def fetch_json(path: str, params: dict[str, str]) -> dict:
            calls.append((path, params))
            return payloads.pop(0)

        provider = OKXMarketDataProvider(fetch_json=fetch_json)
        request = DataRequest(
            universe=(Instrument("ETH-USD", AssetClass.CRYPTO),),
            start=pd.Timestamp("2022-01-01"),
            end=pd.Timestamp("2022-01-03"),
            frequency=DataFrequency.DAILY,
        )

        data = provider.load(request)

        self.assertEqual(calls[0][0], "/api/v5/market/history-candles")
        self.assertEqual(calls[0][1]["instId"], "ETH-USDT")
        self.assertEqual(calls[0][1]["bar"], "1Dutc")
        self.assertEqual(data["symbol"].unique().tolist(), ["ETH-USD"])
        self.assertEqual(len(data), 2)
        self.assertEqual(float(data.iloc[-1]["close"]), 3780.0)
        self.assertEqual(data.iloc[-1]["exchange"], "okx")
        self.assertEqual(data.iloc[-1]["okx_inst_id"], "ETH-USDT")

    def test_okx_derivatives_snapshot_merges_funding_and_open_interest(self) -> None:
        responses = {
            "/api/v5/public/funding-rate": {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "instType": "SWAP",
                        "fundingRate": "0.00025",
                        "premium": "0.0004",
                        "interestRate": "0.0001",
                        "nextFundingTime": "1641024000000",
                        "ts": "1640995200000",
                    }
                ],
            },
            "/api/v5/public/open-interest": {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "instType": "SWAP",
                        "oi": "1000",
                        "oiCcy": "100",
                        "oiUsd": "2500000000",
                        "ts": "1640995300000",
                    }
                ],
            },
        }
        calls: list[tuple[str, dict[str, str]]] = []

        def fetch_json(path: str, params: dict[str, str]) -> dict:
            calls.append((path, params))
            return responses[path]

        provider = OKXDerivativesSnapshotProvider(fetch_json=fetch_json)
        data = provider.load(
            DataRequest(
                universe=(Instrument("ETH-USD", AssetClass.CRYPTO),),
                start=pd.Timestamp("2022-01-01"),
                end=pd.Timestamp("2022-01-03"),
                frequency=DataFrequency.DAILY,
            )
        )

        row = data.iloc[0]
        self.assertEqual(calls[0][1]["instId"], "ETH-USDT-SWAP")
        self.assertEqual(calls[1][1]["instType"], "SWAP")
        self.assertEqual(row["symbol"], "ETH-USD")
        self.assertEqual(row["inst_id"], "ETH-USDT-SWAP")
        self.assertAlmostEqual(row["annualized_funding_rate"], 0.27375)
        self.assertEqual(row["oi_usd"], 2500000000.0)
        self.assertEqual(row["crowding"], "LONG_CROWDED")

    def test_okx_derivatives_history_merges_funding_oi_and_long_short_ratio(self) -> None:
        responses = {
            "/api/v5/public/funding-rate-history": {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "instType": "SWAP",
                        "fundingRate": "0.0002",
                        "realizedRate": "0.0002",
                        "fundingTime": "1640995200000",
                    }
                ],
            },
            "/api/v5/rubik/stat/contracts/open-interest-history": {
                "code": "0",
                "msg": "",
                "data": [["1640995200000", "1000", "100", "2500000000"]],
            },
            "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract": {
                "code": "0",
                "msg": "",
                "data": [["1640995200000", "1.8"]],
            },
        }

        def fetch_json(path: str, params: dict[str, str]) -> dict:
            return responses[path]

        provider = OKXDerivativesHistoryProvider(fetch_json=fetch_json, limit=20, period="1D")
        history = provider.load(
            DataRequest(
                universe=(Instrument("ETH-USD", AssetClass.CRYPTO),),
                start=pd.Timestamp("2022-01-01"),
                end=pd.Timestamp("2022-01-03"),
                frequency=DataFrequency.DAILY,
            )
        )

        row = history.iloc[0]
        self.assertEqual(row["symbol"], "ETH-USD")
        self.assertEqual(row["inst_id"], "ETH-USDT-SWAP")
        self.assertAlmostEqual(row["annualized_funding_rate"], 0.219)
        self.assertEqual(row["oi_usd"], 2500000000.0)
        self.assertEqual(row["long_short_ratio"], 1.8)

    def test_okx_microstructure_snapshot_parses_book_and_trades(self) -> None:
        responses = {
            "/api/v5/market/books": {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "ts": "1640995200000",
                        "bids": [["99", "3"], ["98", "2"]],
                        "asks": [["101", "1"], ["102", "2"]],
                    }
                ],
            },
            "/api/v5/market/trades": {
                "code": "0",
                "msg": "",
                "data": [
                    {"px": "100.5", "sz": "0.7", "side": "buy", "ts": "1640995201000"},
                    {"px": "100.1", "sz": "0.4", "side": "sell", "ts": "1640995200000"},
                ],
            },
        }

        def fetch_json(path: str, params: dict[str, str]) -> dict:
            return responses[path]

        provider = OKXMicrostructureSnapshotProvider(fetch_json=fetch_json, depth_size=20, trade_limit=20)
        snapshot = provider.load(
            DataRequest(
                universe=(Instrument("ETH-USD", AssetClass.CRYPTO),),
                start=pd.Timestamp("2022-01-01"),
                end=pd.Timestamp("2022-01-03"),
                frequency=DataFrequency.DAILY,
            )
        )

        row = snapshot.iloc[0]
        self.assertEqual(row["inst_id"], "ETH-USDT")
        self.assertEqual(row["best_bid"], 99.0)
        self.assertEqual(row["best_ask"], 101.0)
        self.assertAlmostEqual(row["spread_bps"], 200.0)
        self.assertEqual(row["recent_trade_count"], 2)
        self.assertIn("盘口价差", row["interpretation"])

    def test_coinmetrics_onchain_provider_parses_public_metrics(self) -> None:
        responses = {
            "AdrActCnt": {
                "data": [
                    {"time": f"2026-07-{day:02d}T00:00:00.000000000Z", "AdrActCnt": str(1000 + day * 100)}
                    for day in range(1, 10)
                ]
            },
            "TxCnt": {
                "data": [
                    {"time": f"2026-07-{day:02d}T00:00:00.000000000Z", "TxCnt": str(2000 + day * 250)}
                    for day in range(1, 10)
                ]
            },
        }

        def fetch_json(path: str, params: dict[str, str]) -> dict:
            return responses[params["metrics"]]

        provider = CoinMetricsCommunityOnchainProvider(fetch_json=fetch_json)
        result = provider.load(
            DataRequest(
                universe=(Instrument("ETH-USD", AssetClass.CRYPTO),),
                start=pd.Timestamp("2026-07-01"),
                end=pd.Timestamp("2026-07-10"),
                frequency=DataFrequency.DAILY,
            )
        )

        self.assertFalse(result.data.empty)
        latest = result.data.iloc[-1]
        self.assertEqual(latest["asset"], "eth")
        self.assertEqual(latest["active_addresses"], 1900.0)
        self.assertGreater(latest["active_address_change"], 0)
        self.assertEqual(latest["onchain_label"], "链上扩张")

    def test_onchain_context_is_disabled_unless_configured(self) -> None:
        result = load_onchain_context({}, make_request())

        self.assertTrue(result.data.empty)
        self.assertEqual(result.provider, "disabled")

    def test_source_health_marks_okx_market_and_context_status(self) -> None:
        market = pd.DataFrame(
            [
                {
                    "timestamp": "2026-07-09 00:00:00",
                    "symbol": "ETH-USD",
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100,
                    "volume": 10,
                    "asset_class": "crypto",
                    "frequency": "1h",
                }
            ]
        )
        derivatives = pd.DataFrame([{"timestamp": "2026-07-09 00:10:00", "symbol": "ETH-USD"}])
        microstructure = pd.DataFrame([{"timestamp": "2026-07-09 00:45:00", "symbol": "ETH-USD"}])
        report = build_source_health_report(
            {"data": {"provider": "okx", "frequency": "1h"}},
            market,
            derivatives,
            derivatives,
            microstructure,
            generated_at=pd.Timestamp("2026-07-09T01:00:00Z").to_pydatetime(),
        )

        self.assertEqual(report["summary"]["status"], "OK")
        self.assertTrue(report["summary"]["is_real_data"])
        self.assertIn("OKX", report["messages"][0])

    def test_decision_cards_combine_market_signals_and_derivatives(self) -> None:
        market = pd.DataFrame(
            [
                {"timestamp": "2022-01-01", "symbol": "ETH-USD", "open": 100, "high": 102, "low": 99, "close": 100, "volume": 1},
                {"timestamp": "2022-01-02", "symbol": "ETH-USD", "open": 100, "high": 111, "low": 99, "close": 110, "volume": 1},
            ]
        )
        factor_signals = pd.DataFrame(
            [
                {
                    "symbol": "ETH-USD",
                    "factor": "mom_20",
                    "signal": "BULLISH",
                    "signal_label": "偏多",
                    "confidence": 0.82,
                    "signal_score": 0.7,
                    "interpretation": "趋势因子偏多",
                }
            ]
        )
        derivatives = pd.DataFrame(
            [
                {
                    "symbol": "ETH-USD",
                    "annualized_funding_rate": 0.31,
                    "oi_usd": 2500000000.0,
                    "crowding": "LONG_CROWDED",
                    "crowding_label": "多头拥挤",
                    "interpretation": "多头拥挤",
                }
            ]
        )
        derivatives_history = pd.DataFrame(
            [
                {"timestamp": "2022-01-01", "symbol": "ETH-USD", "annualized_funding_rate": 0.2, "oi_usd": 2000000000.0, "long_short_ratio": 1.4},
                {"timestamp": "2022-01-02", "symbol": "ETH-USD", "annualized_funding_rate": 0.31, "oi_usd": 2500000000.0, "long_short_ratio": 1.8},
            ]
        )
        microstructure = pd.DataFrame(
            [
                {
                    "symbol": "ETH-USD",
                    "spread_bps": 3.5,
                    "depth_imbalance": 0.25,
                    "microstructure_label": "买盘占优",
                    "interpretation": "盘口买盘占优",
                }
            ]
        )
        onchain = pd.DataFrame(
            [
                {
                    "timestamp": "2022-01-02",
                    "symbol": "ETH-USD",
                    "active_addresses": 1000,
                    "active_address_change": 0.12,
                    "tx_count": 2000,
                    "tx_count_change": 0.2,
                    "onchain_label": "链上扩张",
                    "interpretation": "链上活跃度扩张",
                }
            ]
        )

        cards = build_decision_cards(market, factor_signals, derivatives, {"summary": {"status": "PASS"}}, derivatives_history, microstructure, onchain)

        card = cards.iloc[0]
        self.assertEqual(card["symbol"], "ETH-USD")
        self.assertEqual(card["stance"], "WATCH_LONG")
        self.assertIn("追多需等待", card["risk_note"])
        self.assertEqual(card["data_quality_status"], "PASS")
        self.assertAlmostEqual(card["funding_change"], 0.11)
        self.assertAlmostEqual(card["oi_change"], 0.25)
        self.assertEqual(card["microstructure_label"], "买盘占优")
        self.assertEqual(card["onchain_label"], "链上扩张")
        self.assertEqual(card["active_addresses"], 1000)
        self.assertIn("多空账户比", card["evidence"])
        self.assertIn("链上活跃度扩张", card["evidence"])
        self.assertIn("invalidation_note", card)

    def test_operator_factors_and_forward_returns_align(self) -> None:
        data = SyntheticMarketDataProvider(seed=2).load(make_request())
        factors = build_operator_factor_panel(data, windows=[3, 5, 10])
        target = compute_forward_returns(data, horizon=1)
        self.assertIn("mom_5", factors.columns)
        self.assertIn("fwd_return_1", target.columns)
        self.assertEqual(len(factors), len(target))
        self.assertGreater(factors["mom_5"].notna().sum(), 100)

    def test_technical_indicator_panel_and_state_are_trader_readable(self) -> None:
        data = SyntheticMarketDataProvider(seed=12).load(make_request())

        technicals = build_technical_indicator_panel(data)
        states = build_indicator_state_table(technicals)

        self.assertEqual(len(technicals), len(data))
        for column in [
            "ema_20",
            "ema_50",
            "rsi_14",
            "macd_hist",
            "bb_upper_20",
            "atr_percent_14",
            "supertrend_direction_10_3",
            "vwap_20",
            "obv",
            "adx_14",
            "mfi_14",
            "stoch_rsi_k_14",
            "donchian_position_20",
        ]:
            self.assertIn(column, technicals.columns)
            self.assertGreater(technicals[column].notna().sum(), 0, column)

        self.assertEqual(set(states["symbol"]), {"AAPL", "BTC-USD", "ETH-USD", "MSFT"})
        state = states[states["symbol"] == "ETH-USD"].iloc[0]
        self.assertIn(state["technical_bias"], {"LEAN_LONG", "LEAN_SHORT", "NEUTRAL", "WATCH_LONG", "WATCH_SHORT"})
        self.assertIn("EMA", state["interpretation"])
        self.assertGreaterEqual(float(state["confidence"]), 0.0)
        self.assertLessEqual(float(state["confidence"]), 1.0)

    def test_evaluation_and_backtest_produce_metrics(self) -> None:
        data = SyntheticMarketDataProvider(seed=3).load(make_request())
        factors = build_operator_factor_panel(data, windows=[3, 5, 10, 20])
        evaluation = evaluate_factor_panel(data, factors, horizons=[1], min_observations=50)
        selected = select_top_factor(evaluation, horizon=1)
        result = run_rank_backtest(
            data,
            factors,
            selected["factor"],
            BacktestConfig(horizon=1, top_n=1, bottom_n=1, transaction_cost_bps=2.0, direction=int(selected["direction"])),
        )
        self.assertFalse(evaluation.empty)
        self.assertIn("sharpe", result.metrics)
        self.assertGreater(len(result.returns), 50)
        self.assertFalse(result.weights.empty)

    def test_backtest_models_slippage_spread_and_funding_costs(self) -> None:
        data = SyntheticMarketDataProvider(seed=5).load(make_request())
        factors = build_operator_factor_panel(data, windows=[3, 5, 10])
        microstructure = pd.DataFrame(
            [
                {"symbol": "BTC-USD", "spread_bps": 4.0},
                {"symbol": "ETH-USD", "spread_bps": 5.0},
            ]
        )
        derivatives_history = pd.DataFrame(
            [
                {"timestamp": "2022-01-01 00:00:00.000001", "symbol": "BTC-USD", "annualized_funding_rate": 0.12},
                {"timestamp": "2022-01-01 00:00:00.000001", "symbol": "ETH-USD", "annualized_funding_rate": 0.10},
            ]
        )

        result = run_rank_backtest(
            data,
            factors,
            "mom_5",
            BacktestConfig(
                horizon=1,
                top_n=1,
                bottom_n=1,
                transaction_cost_bps=2.0,
                slippage_bps=1.5,
                spread_cost_multiplier=0.5,
                funding_enabled=True,
                annualization=365,
            ),
            microstructure_snapshot=microstructure,
            derivatives_history=derivatives_history,
        )

        self.assertIn("execution_cost", result.returns.columns)
        self.assertIn("funding_cost", result.returns.columns)
        self.assertGreater(result.metrics["average_execution_cost"], 0)
        self.assertIn("average_funding_cost", result.metrics)

    def test_walk_forward_validation_uses_purged_test_windows(self) -> None:
        data = SyntheticMarketDataProvider(seed=6).load(make_request())
        factors = build_operator_factor_panel(data, windows=[3, 5, 10, 20])
        result = run_walk_forward_validation(
            data,
            factors,
            {
                "mining": {"target_horizon": 1},
                "evaluation": {"forward_horizons": [1], "min_cross_section_assets": 2},
                "backtest": {"horizon": 1, "top_n": 1, "bottom_n": 1},
                "validation": {
                    "enabled": True,
                    "n_splits": 3,
                    "train_fraction": 0.5,
                    "embargo_bars": 2,
                    "min_train_observations": 40,
                    "min_test_bars": 20,
                },
            },
        )

        self.assertGreaterEqual(len(result), 1)
        first = result.iloc[0]
        self.assertIsNone(first["error"])
        self.assertGreater(pd.Timestamp(first["test_start"]), pd.Timestamp(first["train_end"]))
        self.assertIn("test_sharpe", result.columns)

    def test_market_data_quality_report_flags_data_defects(self) -> None:
        data = pd.DataFrame(
            [
                {
                    "timestamp": "2022-01-01",
                    "symbol": "ETH-USD",
                    "open": 100,
                    "high": 105,
                    "low": 95,
                    "close": 101,
                    "volume": 1000,
                    "asset_class": "crypto",
                    "frequency": "1d",
                },
                {
                    "timestamp": "2022-01-01",
                    "symbol": "ETH-USD",
                    "open": 101,
                    "high": 90,
                    "low": 99,
                    "close": 102,
                    "volume": 0,
                    "asset_class": "crypto",
                    "frequency": "1d",
                },
                {
                    "timestamp": "2022-01-04",
                    "symbol": "ETH-USD",
                    "open": 102,
                    "high": 110,
                    "low": 100,
                    "close": 108,
                    "volume": 900,
                    "asset_class": "crypto",
                    "frequency": "1d",
                },
            ]
        )

        report = build_market_data_quality_report(data)

        eth = report["symbols"][0]
        self.assertEqual(report["summary"]["status"], "FAIL")
        self.assertEqual(eth["duplicate_timestamps"], 1)
        self.assertEqual(eth["invalid_prices"], 1)
        self.assertEqual(eth["zero_or_negative_volume"], 1)
        self.assertGreaterEqual(eth["estimated_missing_bars"], 1)
        self.assertTrue(any(issue["type"] == "invalid_prices" for issue in report["issues"]))

    def test_factor_signal_radar_translates_latest_factor_state(self) -> None:
        factor_panel = pd.DataFrame(
            {
                "timestamp": pd.date_range("2022-01-01", periods=30, freq="D").tolist() * 2,
                "symbol": ["ETH-USD"] * 30 + ["BTC-USD"] * 30,
                "mom_5": list(range(30)) + list(reversed(range(30))),
            }
        )
        factor_backtests = pd.DataFrame(
            [
                {
                    "factor": "mom_5",
                    "horizon": 20,
                    "direction": 1,
                    "score": 4.5,
                    "sharpe": 2.4,
                    "total_return": 0.32,
                    "max_drawdown": -0.08,
                    "win_rate": 0.61,
                }
            ]
        )

        signals = build_factor_signal_radar(factor_panel, factor_backtests, min_history=20, limit_per_symbol=4)

        eth = signals[signals["symbol"] == "ETH-USD"].iloc[0]
        btc = signals[signals["symbol"] == "BTC-USD"].iloc[0]
        self.assertEqual(eth["signal"], "BULLISH")
        self.assertEqual(eth["signal_label"], "偏多")
        self.assertGreater(eth["percentile"], 0.95)
        self.assertEqual(btc["signal"], "BEARISH")
        self.assertIn("历史", eth["interpretation"])

    def test_full_pipeline_writes_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "run"
            config = {
                "data": {
                    "provider": "synthetic",
                    "start": "2022-01-01",
                    "end": "2023-01-01",
                    "frequency": "1d",
                    "seed": 4,
                    "universe": [
                        {"symbol": "BTC-USD", "asset_class": "crypto"},
                        {"symbol": "ETH-USD", "asset_class": "crypto"},
                        {"symbol": "AAPL", "asset_class": "equity"},
                        {"symbol": "MSFT", "asset_class": "equity"},
                    ],
                },
                "mining": {
                    "enable_operator_miner": True,
                    "enable_ml_miner": True,
                    "operator_windows": [3, 5, 10],
                    "target_horizon": 1,
                    "ml_n_estimators": 20,
                    "random_state": 4,
                },
                "evaluation": {"forward_horizons": [1], "min_observations": 50},
                "backtest": {"horizon": 1, "top_n": 1, "bottom_n": 1, "transaction_cost_bps": 2.0},
                "output_dir": str(output_dir),
            }
            result = PipelineRunner(config).run()
            self.assertGreater(result.summary["factor_count"], 0)
            self.assertTrue((output_dir / "summary.json").exists())
            self.assertTrue((output_dir / "factor_backtests.csv").exists())
            self.assertTrue((output_dir / "factor_signals.csv").exists())
            self.assertTrue((output_dir / "data_quality.json").exists())
            self.assertTrue((output_dir / "data_quality.csv").exists())
            self.assertTrue((output_dir / "source_health.json").exists())
            self.assertTrue((output_dir / "source_status.csv").exists())
            self.assertTrue((output_dir / "derivatives_snapshot.csv").exists())
            self.assertTrue((output_dir / "derivatives_history.csv").exists())
            self.assertTrue((output_dir / "microstructure_snapshot.csv").exists())
            self.assertTrue((output_dir / "onchain_metrics.csv").exists())
            self.assertTrue((output_dir / "onchain_health.json").exists())
            self.assertTrue((output_dir / "raw_data_manifest.csv").exists())
            self.assertTrue((output_dir / "decision_cards.csv").exists())
            self.assertTrue((output_dir / "technical_indicators.csv").exists())
            self.assertTrue((output_dir / "indicator_states.csv").exists())
            self.assertTrue((output_dir / "walk_forward.csv").exists())
            with (output_dir / "summary.json").open("r", encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(summary["output_dir"], str(output_dir))
            self.assertIn("factor_backtests", summary["outputs"])
            self.assertIn("factor_signals", summary["outputs"])
            self.assertIn("data_quality", summary["outputs"])
            self.assertIn("source_health", summary["outputs"])
            self.assertIn("source_status", summary["outputs"])
            self.assertIn("derivatives_snapshot", summary["outputs"])
            self.assertIn("derivatives_history", summary["outputs"])
            self.assertIn("microstructure_snapshot", summary["outputs"])
            self.assertIn("onchain_metrics", summary["outputs"])
            self.assertIn("onchain_health", summary["outputs"])
            self.assertIn("raw_data_manifest", summary["outputs"])
            self.assertIn("decision_cards", summary["outputs"])
            self.assertIn("walk_forward", summary["outputs"])
            self.assertIn("technical_indicators", summary["outputs"])
            self.assertIn("indicator_states", summary["outputs"])
            self.assertGreater(len(summary["raw_data_manifest"]), 0)
            self.assertGreater(len(summary["factor_backtests"]), 0)
            self.assertIn("sharpe", summary["factor_backtests"][0])
            self.assertGreater(len(summary["factor_signals"]), 0)
            self.assertIn("signal", summary["factor_signals"][0])
            self.assertEqual(summary["data_quality"]["summary"]["status"], "PASS")
            self.assertEqual(summary["source_health"]["summary"]["status"], "SIMULATED")
            self.assertGreater(len(summary["indicator_states"]), 0)
            self.assertIn("technical_bias", summary["indicator_states"][0])
            self.assertIn("ema_20", summary["technical_indicators"][0])


if __name__ == "__main__":
    unittest.main()
