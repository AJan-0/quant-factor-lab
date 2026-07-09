from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .adjustments import apply_market_data_adjustments
from .backtest import BacktestConfig, BacktestResult, run_rank_backtest
from .data import build_provider
from .decisions import build_decision_cards
from .derivatives import load_derivatives_history, load_derivatives_snapshot
from .evaluation import evaluate_factor_panel, select_top_factor
from .mining import FactorCandidate, MLFeatureMiner, OperatorFactorMiner, combine_factor_panels
from .microstructure import load_microstructure_snapshot
from .onchain import load_onchain_context
from .quality import build_market_data_quality_report, quality_symbols_frame
from .raw_store import RawDataVersionStore, raw_snapshot_frame
from .runtime import NULL_PIPELINE_CONTEXT, PipelineContext
from .signals import build_factor_signal_radar
from .source_health import build_source_health_report, source_status_frame
from .technicals import build_indicator_state_table, build_technical_indicator_panel
from .types import DataRequest, KEY_COLUMNS
from .validation import run_walk_forward_validation


@dataclass(frozen=True)
class PipelineResult:
    market_data: pd.DataFrame
    factor_panel: pd.DataFrame
    candidates: tuple[FactorCandidate, ...]
    evaluation: pd.DataFrame
    backtest: BacktestResult
    summary: dict[str, Any]


class PipelineRunner:
    def __init__(self, config: dict[str, Any], context: PipelineContext | None = None) -> None:
        self.config = config
        self.context = context or NULL_PIPELINE_CONTEXT

    @classmethod
    def from_config_path(cls, path: str | Path) -> "PipelineRunner":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    def run(self) -> PipelineResult:
        self.context.checkpoint("Preparing data request")
        data_config = self.config.get("data", {})
        request = DataRequest.from_config(data_config)
        provider = build_provider(data_config)
        self.context.checkpoint("Loading market data")
        market_data = provider.load(request)
        self.context.checkpoint(f"Loaded {len(market_data)} market rows")
        market_data = apply_market_data_adjustments(market_data, data_config.get("adjustments", []))
        data_quality = build_market_data_quality_report(market_data)
        self.context.checkpoint("Loading derivatives and microstructure context")
        derivatives_snapshot = load_derivatives_snapshot(self.config, request)
        derivatives_history = load_derivatives_history(self.config, request)
        microstructure_snapshot = load_microstructure_snapshot(self.config, request)
        onchain_result = load_onchain_context(self.config, request)
        source_health = build_source_health_report(
            self.config,
            market_data,
            derivatives_snapshot,
            derivatives_history,
            microstructure_snapshot,
            onchain_result.data,
            onchain_result.warnings,
        )

        self.context.checkpoint("Computing technical indicator panel")
        technical_indicators = build_technical_indicator_panel(market_data)
        indicator_states = build_indicator_state_table(technical_indicators)

        self.context.checkpoint("Mining factor candidates")
        mining_config = self.config.get("mining", {})
        windows = mining_config.get("operator_windows", [3, 5, 10, 20, 60])
        panels: list[pd.DataFrame] = []
        candidates: list[FactorCandidate] = []

        if mining_config.get("enable_operator_miner", True):
            operator_result = OperatorFactorMiner(windows).mine(market_data)
            panels.append(operator_result.factor_panel)
            candidates.extend(operator_result.candidates)
            self.context.checkpoint(f"Operator miner produced {len(operator_result.candidates)} candidates")

        if mining_config.get("enable_ml_miner", False):
            ml_result = MLFeatureMiner(
                windows=windows,
                target_horizon=int(mining_config.get("target_horizon", 1)),
                max_features=int(mining_config.get("ml_max_features", 10)),
                train_fraction=float(mining_config.get("ml_train_fraction", 0.7)),
                random_state=int(mining_config.get("random_state", 7)),
                n_estimators=int(mining_config.get("ml_n_estimators", 80)),
            ).mine(market_data)
            panels.append(ml_result.factor_panel)
            candidates.extend(ml_result.candidates)
            self.context.checkpoint(f"ML miner produced {len(ml_result.candidates)} candidates")

        factor_panel = combine_factor_panels(panels)
        evaluation_config = self.config.get("evaluation", {})
        self.context.checkpoint("Evaluating factor panel")
        evaluation = evaluate_factor_panel(
            market_data,
            factor_panel,
            horizons=evaluation_config.get("forward_horizons", [1, 5, 20]),
            min_observations=int(evaluation_config.get("min_observations", 60)),
            min_cross_section_assets=int(evaluation_config.get("min_cross_section_assets", 3)),
        )

        backtest_config_raw = self.config.get("backtest", {})
        backtest_horizon = int(backtest_config_raw.get("horizon", mining_config.get("target_horizon", 1)))
        top_factor = select_top_factor(evaluation, horizon=backtest_horizon)
        backtest_config = BacktestConfig.from_config(backtest_config_raw, direction=int(top_factor["direction"]))
        self.context.checkpoint(f"Running primary backtest for {top_factor['factor']}")
        backtest = run_rank_backtest(
            market_data,
            factor_panel,
            str(top_factor["factor"]),
            backtest_config,
            microstructure_snapshot=microstructure_snapshot,
            derivatives_history=derivatives_history,
        )
        self.context.checkpoint("Running per-factor backtests")
        factor_backtests = self._run_factor_backtests(
            market_data=market_data,
            factor_panel=factor_panel,
            evaluation=evaluation,
            raw_backtest_config=backtest_config_raw,
            horizon=backtest_horizon,
            microstructure_snapshot=microstructure_snapshot,
            derivatives_history=derivatives_history,
        )
        walk_forward = run_walk_forward_validation(
            market_data,
            factor_panel,
            self.config,
            microstructure_snapshot=microstructure_snapshot,
            derivatives_history=derivatives_history,
        )
        factor_signals = build_factor_signal_radar(factor_panel=factor_panel, factor_backtests=factor_backtests)
        self.context.checkpoint("Building decision cards")
        decision_cards = build_decision_cards(
            market_data,
            factor_signals,
            derivatives_snapshot,
            data_quality,
            derivatives_history,
            microstructure_snapshot,
            onchain_result.data,
        )
        output_dir = Path(self.config.get("output_dir", "runs/latest"))
        self.context.checkpoint("Writing run artifacts")
        summary = self._write_outputs(
            output_dir=output_dir,
            market_data=market_data,
            data_quality=data_quality,
            source_health=source_health,
            derivatives_snapshot=derivatives_snapshot,
            derivatives_history=derivatives_history,
            microstructure_snapshot=microstructure_snapshot,
            onchain_metrics=onchain_result.data,
            onchain_warnings=onchain_result.warnings,
            technical_indicators=technical_indicators,
            indicator_states=indicator_states,
            factor_panel=factor_panel,
            candidates=tuple(candidates),
            evaluation=evaluation,
            backtest=backtest,
            factor_backtests=factor_backtests,
            walk_forward=walk_forward,
            factor_signals=factor_signals,
            decision_cards=decision_cards,
            top_factor=top_factor,
        )
        self.context.checkpoint("Pipeline completed")
        return PipelineResult(
            market_data=market_data,
            factor_panel=factor_panel,
            candidates=tuple(candidates),
            evaluation=evaluation,
            backtest=backtest,
            summary=summary,
        )

    def _run_factor_backtests(
        self,
        market_data: pd.DataFrame,
        factor_panel: pd.DataFrame,
        evaluation: pd.DataFrame,
        raw_backtest_config: dict[str, Any],
        horizon: int,
        microstructure_snapshot: pd.DataFrame | None = None,
        derivatives_history: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        horizon_evaluation = evaluation[evaluation["horizon"].astype(int) == int(horizon)].copy()
        horizon_evaluation = horizon_evaluation.sort_values("score", ascending=False)

        for _, factor_row in horizon_evaluation.iterrows():
            factor_name = str(factor_row["factor"])
            direction = int(factor_row.get("direction", 1))
            config = BacktestConfig.from_config(raw_backtest_config, direction=direction)
            base = {
                "factor": factor_name,
                "horizon": int(horizon),
                "direction": direction,
                "score": factor_row.get("score"),
                "pooled_spearman": factor_row.get("pooled_spearman"),
                "quantile_spread": factor_row.get("quantile_spread"),
                "observations": factor_row.get("observations"),
            }
            try:
                result = run_rank_backtest(
                    market_data,
                    factor_panel,
                    factor_name,
                    config,
                    microstructure_snapshot=microstructure_snapshot,
                    derivatives_history=derivatives_history,
                )
            except Exception as exc:
                rows.append({**base, "error": str(exc)})
                continue
            rows.append(
                {
                    **base,
                    "total_return": result.metrics.get("total_return"),
                    "annualized_return": result.metrics.get("annualized_return"),
                    "annualized_volatility": result.metrics.get("annualized_volatility"),
                    "sharpe": result.metrics.get("sharpe"),
                    "max_drawdown": result.metrics.get("max_drawdown"),
                    "win_rate": result.metrics.get("win_rate"),
                    "average_turnover": result.metrics.get("average_turnover"),
                    "average_execution_cost": result.metrics.get("average_execution_cost"),
                    "average_funding_cost": result.metrics.get("average_funding_cost"),
                    "periods": result.metrics.get("periods"),
                    "error": None,
                }
            )

        return pd.DataFrame(rows)

    def _write_outputs(
        self,
        output_dir: Path,
        market_data: pd.DataFrame,
        data_quality: dict[str, Any],
        source_health: dict[str, Any],
        derivatives_snapshot: pd.DataFrame,
        derivatives_history: pd.DataFrame,
        microstructure_snapshot: pd.DataFrame,
        onchain_metrics: pd.DataFrame,
        onchain_warnings: tuple[str, ...],
        technical_indicators: pd.DataFrame,
        indicator_states: pd.DataFrame,
        factor_panel: pd.DataFrame,
        candidates: tuple[FactorCandidate, ...],
        evaluation: pd.DataFrame,
        backtest: BacktestResult,
        factor_backtests: pd.DataFrame,
        walk_forward: pd.DataFrame,
        factor_signals: pd.DataFrame,
        decision_cards: pd.DataFrame,
        top_factor: dict[str, Any],
    ) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates_frame = pd.DataFrame([candidate.to_dict() for candidate in candidates])
        paths = {
            "market_data": output_dir / "market_data.csv",
            "data_quality": output_dir / "data_quality.json",
            "data_quality_symbols": output_dir / "data_quality.csv",
            "source_health": output_dir / "source_health.json",
            "source_status": output_dir / "source_status.csv",
            "derivatives_snapshot": output_dir / "derivatives_snapshot.csv",
            "derivatives_history": output_dir / "derivatives_history.csv",
            "microstructure_snapshot": output_dir / "microstructure_snapshot.csv",
            "onchain_metrics": output_dir / "onchain_metrics.csv",
            "onchain_health": output_dir / "onchain_health.json",
            "raw_data_manifest": output_dir / "raw_data_manifest.csv",
            "decision_cards": output_dir / "decision_cards.csv",
            "technical_indicators": output_dir / "technical_indicators.csv",
            "indicator_states": output_dir / "indicator_states.csv",
            "factor_panel": output_dir / "factor_panel.csv",
            "factor_candidates": output_dir / "factor_candidates.csv",
            "factor_evaluation": output_dir / "factor_evaluation.csv",
            "factor_backtests": output_dir / "factor_backtests.csv",
            "walk_forward": output_dir / "walk_forward.csv",
            "factor_signals": output_dir / "factor_signals.csv",
            "backtest_returns": output_dir / "backtest_returns.csv",
            "backtest_weights": output_dir / "backtest_weights.csv",
            "backtest_metrics": output_dir / "backtest_metrics.json",
            "summary": output_dir / "summary.json",
        }
        market_data.to_csv(paths["market_data"], index=False)
        quality_symbols_frame(data_quality).to_csv(paths["data_quality_symbols"], index=False)
        source_status_frame(source_health).to_csv(paths["source_status"], index=False)
        derivatives_snapshot.to_csv(paths["derivatives_snapshot"], index=False)
        derivatives_history.to_csv(paths["derivatives_history"], index=False)
        microstructure_snapshot.to_csv(paths["microstructure_snapshot"], index=False)
        onchain_metrics.to_csv(paths["onchain_metrics"], index=False)
        decision_cards.to_csv(paths["decision_cards"], index=False)
        technical_indicators.to_csv(paths["technical_indicators"], index=False)
        indicator_states.to_csv(paths["indicator_states"], index=False)
        factor_panel.to_csv(paths["factor_panel"], index=False)
        candidates_frame.to_csv(paths["factor_candidates"], index=False)
        evaluation.to_csv(paths["factor_evaluation"], index=False)
        factor_backtests.to_csv(paths["factor_backtests"], index=False)
        walk_forward.to_csv(paths["walk_forward"], index=False)
        factor_signals.to_csv(paths["factor_signals"], index=False)
        backtest.returns.to_csv(paths["backtest_returns"], index=False)
        backtest.weights.to_csv(paths["backtest_weights"], index=False)
        raw_data_manifest = self._record_raw_snapshots(
            output_dir=output_dir,
            paths=paths,
            market_data=market_data,
            derivatives_snapshot=derivatives_snapshot,
            derivatives_history=derivatives_history,
            microstructure_snapshot=microstructure_snapshot,
            onchain_metrics=onchain_metrics,
        )
        raw_data_manifest.to_csv(paths["raw_data_manifest"], index=False)
        with paths["backtest_metrics"].open("w", encoding="utf-8") as handle:
            json.dump(_json_ready(backtest.metrics), handle, indent=2)
        with paths["data_quality"].open("w", encoding="utf-8") as handle:
            json.dump(_json_ready(data_quality), handle, indent=2, ensure_ascii=False)
        with paths["source_health"].open("w", encoding="utf-8") as handle:
            json.dump(_json_ready(source_health), handle, indent=2, ensure_ascii=False)
        with paths["onchain_health"].open("w", encoding="utf-8") as handle:
            json.dump({"warnings": list(onchain_warnings), "rows": len(onchain_metrics)}, handle, indent=2, ensure_ascii=False)

        factor_columns = [column for column in factor_panel.columns if column not in KEY_COLUMNS]
        summary = {
            "rows": int(len(market_data)),
            "symbols": sorted(market_data["symbol"].unique().tolist()),
            "factor_count": int(len(factor_columns)),
            "candidate_count": int(len(candidates)),
            "top_factor": _json_ready(top_factor),
            "data_quality": _json_ready(data_quality),
            "source_health": _json_ready(source_health),
            "derivatives_snapshot": _json_ready(derivatives_snapshot.head(50).to_dict(orient="records")),
            "derivatives_history": _json_ready(derivatives_history.tail(100).to_dict(orient="records")),
            "microstructure_snapshot": _json_ready(microstructure_snapshot.head(50).to_dict(orient="records")),
            "onchain_metrics": _json_ready(onchain_metrics.tail(100).to_dict(orient="records")),
            "onchain_warnings": _json_ready(list(onchain_warnings)),
            "raw_data_manifest": _json_ready(raw_data_manifest.to_dict(orient="records")),
            "decision_cards": _json_ready(decision_cards.to_dict(orient="records")),
            "backtest_metrics": _json_ready(backtest.metrics),
            "technical_indicators": _json_ready(technical_indicators.tail(120).to_dict(orient="records")),
            "indicator_states": _json_ready(indicator_states.to_dict(orient="records")),
            "factor_backtests": _json_ready(factor_backtests.head(50).to_dict(orient="records")),
            "walk_forward": _json_ready(walk_forward.head(50).to_dict(orient="records")),
            "factor_signals": _json_ready(factor_signals.head(80).to_dict(orient="records")),
            "output_dir": str(output_dir),
            "outputs": {name: str(path) for name, path in paths.items()},
        }
        with paths["summary"].open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2)
        return summary

    def _record_raw_snapshots(
        self,
        output_dir: Path,
        paths: dict[str, Path],
        market_data: pd.DataFrame,
        derivatives_snapshot: pd.DataFrame,
        derivatives_history: pd.DataFrame,
        microstructure_snapshot: pd.DataFrame,
        onchain_metrics: pd.DataFrame,
    ) -> pd.DataFrame:
        provider = str(self.config.get("data", {}).get("provider", "unknown")).lower()
        frequency = str(self.config.get("data", {}).get("frequency", "")) or None
        raw_store_config = self.config.get("raw_store", {})
        db_path = Path(str(raw_store_config.get("db_path", output_dir.parent / "raw_data_versions.sqlite3")))
        if not db_path.is_absolute():
            db_path = output_dir.parent / db_path
        store = RawDataVersionStore(db_path)
        run_id = self.context.run_id or output_dir.name
        snapshots = [
            store.record_frame(
                run_id=run_id,
                dataset="market_data",
                provider=provider,
                frame=market_data,
                artifact_path=paths["market_data"],
                frequency=frequency,
            ),
            store.record_frame(
                run_id=run_id,
                dataset="derivatives_snapshot",
                provider=provider,
                frame=derivatives_snapshot,
                artifact_path=paths["derivatives_snapshot"],
                frequency=frequency,
            ),
            store.record_frame(
                run_id=run_id,
                dataset="derivatives_history",
                provider=provider,
                frame=derivatives_history,
                artifact_path=paths["derivatives_history"],
                frequency=frequency,
            ),
            store.record_frame(
                run_id=run_id,
                dataset="microstructure_snapshot",
                provider=provider,
                frame=microstructure_snapshot,
                artifact_path=paths["microstructure_snapshot"],
                frequency=frequency,
            ),
            store.record_frame(
                run_id=run_id,
                dataset="onchain_metrics",
                provider="coinmetrics_community",
                frame=onchain_metrics,
                artifact_path=paths["onchain_metrics"],
                frequency="1d",
            ),
        ]
        return raw_snapshot_frame(snapshots)


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if hasattr(value, "item"):
        return _json_ready(value.item())
    if isinstance(value, float) and pd.isna(value):
        return None
    return value

