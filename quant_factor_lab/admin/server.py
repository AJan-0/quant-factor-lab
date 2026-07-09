from __future__ import annotations

import json
import hmac
import mimetypes
import posixpath
import os
import threading
import time
import uuid
from http import HTTPStatus
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

import pandas as pd

from quant_factor_lab.adjustments import normalize_adjustments
from quant_factor_lab.pipeline import PipelineRunner
from quant_factor_lab.realtime import OKXRealtimeService
from quant_factor_lab.run_store import RunStore, hash_config
from quant_factor_lab.runtime import PipelineCancelled, PipelineContext
from quant_factor_lab.types import DataRequest

# Production-hardening helpers live below.

@dataclass
class AdminJob:
    id: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    config_hash: str | None = None
    output_dir: str | None = None
    run_id: str | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "createdAt": _format_timestamp(self.created_at),
            "startedAt": _format_timestamp(self.started_at),
            "finishedAt": _format_timestamp(self.finished_at),
            "error": self.error,
            "configHash": self.config_hash,
            "outputDir": self.output_dir,
            "runId": self.run_id or self.id,
            "cancelRequested": self.cancel_requested,
            "result": self.result,
        }



class AdminJobManager:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store
        self._jobs: dict[str, AdminJob] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, target: Any, config_hash: str, output_dir: str) -> AdminJob:
        run_id = uuid.uuid4().hex
        self.run_store.create_run(run_id=run_id, config_hash=config_hash, output_dir=output_dir)
        cancel_event = threading.Event()
        job = AdminJob(
            id=run_id,
            status="queued",
            created_at=time.time(),
            config_hash=config_hash,
            output_dir=output_dir,
            run_id=run_id,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._cancel_events[job.id] = cancel_event
        thread = threading.Thread(target=self._run, args=(job.id, target, cancel_event), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> AdminJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[AdminJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def cancel(self, job_id: str) -> AdminJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            cancel_event = self._cancel_events.get(job_id)
            if job is None or cancel_event is None:
                return None
            if job.status in {"succeeded", "failed", "canceled"}:
                return job
            job.cancel_requested = True
            if job.status == "queued":
                job.status = "canceling"
            cancel_event.set()
        self.run_store.append_log(job_id, "WARN", "Cancellation requested")
        return job

    def logs(self, job_id: str, limit: int = 500, after_id: int | None = None) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.run_store.list_logs(job_id, limit=limit, after_id=after_id)]

    def _run(self, job_id: str, target: Any, cancel_event: threading.Event) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = time.time()
        try:
            self.run_store.mark_running(job_id)
            self.run_store.append_log(job_id, "INFO", "Job started")
            context = PipelineContext(
                run_id=job_id,
                log=lambda level, message: self.run_store.append_log(job_id, level, message),
                is_cancelled=cancel_event.is_set,
            )
            result = target(context)
        except PipelineCancelled as exc:
            self.run_store.append_log(job_id, "WARN", str(exc))
            self.run_store.mark_canceled(job_id, str(exc))
            with self._lock:
                job = self._jobs[job_id]
                job.status = "canceled"
                job.error = str(exc)
                job.cancel_requested = True
                job.finished_at = time.time()
            return
        except Exception as exc:
            self.run_store.append_log(job_id, "ERROR", str(exc))
            self.run_store.mark_failed(job_id, str(exc))
            with self._lock:
                job = self._jobs[job_id]
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = time.time()
            return
        self.run_store.append_log(job_id, "INFO", "Job succeeded")
        self.run_store.mark_succeeded(job_id, result.get("summary", {}))
        with self._lock:
            job = self._jobs[job_id]
            job.status = "succeeded"
            job.result = result
            job.finished_at = time.time()


class RateLimiter:
    def __init__(self, max_requests: int = 240, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            events = [event for event in self._events.get(key, []) if event >= cutoff]
            if len(events) >= self.max_requests:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


def _format_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


class AdminApp:
    def __init__(self, config_path: str | Path, root: str | Path | None = None) -> None:
        self.root = Path(root or Path.cwd()).resolve()
        self.config_path = self._resolve_under_root(Path(config_path))
        self.run_store = RunStore(self._resolve_under_root(Path("runs/admin.sqlite3")))

    def load_config(self) -> dict[str, Any]:
        with self.config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        self.validate_config(config)
        return config

    def save_config(self, config: dict[str, Any]) -> dict[str, Any]:
        self.validate_config(config)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        tmp_path.replace(self.config_path)
        return config

    def prepare_run_context(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if config is not None:
            self.save_config(config)
        active_config = self.load_config()
        runtime_config = json.loads(json.dumps(active_config))
        output_dir = self._output_dir(active_config)
        runtime_config["output_dir"] = str(output_dir)
        return {
            "config": active_config,
            "runtimeConfig": runtime_config,
            "configHash": hash_config(runtime_config),
            "outputDir": str(output_dir),
        }

    def run_pipeline(
        self,
        config: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        pipeline_context: PipelineContext | None = None,
    ) -> dict[str, Any]:
        run_context = context or self.prepare_run_context(config)
        result = PipelineRunner(run_context["runtimeConfig"], context=pipeline_context).run()
        return {"summary": result.summary, "snapshot": self.load_snapshot(run_context["config"])}

    def list_runs(self, query: str) -> dict[str, Any]:
        params = parse_qs(query)
        try:
            limit = max(1, min(int(params.get("limit", ["50"])[0]), 500))
        except ValueError:
            limit = 50
        return {"runs": [run.to_dict() for run in self.run_store.list_runs(limit=limit)]}

    def get_run(self, run_id: str) -> dict[str, Any]:
        record = self.run_store.get_run(run_id)
        if record is None:
            raise FileNotFoundError(run_id)
        return {"run": record.to_dict()}

    def load_snapshot(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        active_config = config or self.load_config()
        output_dir = self._output_dir(active_config)
        summary_path = output_dir / "summary.json"
        summary = _read_json_if_exists(summary_path)
        data_quality = _read_json_if_exists(output_dir / "data_quality.json")
        source_health = _read_json_if_exists(output_dir / "source_health.json")
        source_status_rows = _read_csv_records(output_dir / "source_status.csv", limit=100)
        data_quality_rows = _read_csv_records(output_dir / "data_quality.csv", limit=100)
        derivatives_snapshot = _read_csv_records(output_dir / "derivatives_snapshot.csv", limit=50)
        derivatives_history = _read_csv_records(output_dir / "derivatives_history.csv", limit=200)
        microstructure_snapshot = _read_csv_records(output_dir / "microstructure_snapshot.csv", limit=50)
        onchain_metrics = _read_csv_records(output_dir / "onchain_metrics.csv", limit=120)
        onchain_health = _read_json_if_exists(output_dir / "onchain_health.json")
        raw_data_manifest = _read_csv_records(output_dir / "raw_data_manifest.csv", limit=100)
        technical_indicators = _read_csv_records(output_dir / "technical_indicators.csv", limit=300)
        indicator_states = _read_csv_records(output_dir / "indicator_states.csv", limit=50)
        decision_cards = _read_csv_records(output_dir / "decision_cards.csv", limit=50)
        factor_rows = _read_csv_records(output_dir / "factor_evaluation.csv", limit=25)
        factor_backtest_rows = _read_csv_records(output_dir / "factor_backtests.csv", limit=50)
        walk_forward_rows = _read_csv_records(output_dir / "walk_forward.csv", limit=50)
        factor_signal_rows = _read_csv_records(output_dir / "factor_signals.csv", limit=100)
        returns_rows = _read_csv_records(output_dir / "backtest_returns.csv", limit=800)
        market_rows = _read_csv_records(output_dir / "market_data.csv", limit=50)
        artifacts = []
        if output_dir.exists():
            artifacts = [
                {"name": path.name, "size": path.stat().st_size}
                for path in sorted(output_dir.iterdir())
                if path.is_file()
            ]
        return {
            "outputDir": str(output_dir),
            "hasRun": summary is not None,
            "summary": summary,
            "dataQuality": data_quality,
            "sourceHealth": source_health,
            "sourceStatusRows": source_status_rows,
            "dataQualityRows": data_quality_rows,
            "derivativesSnapshot": derivatives_snapshot,
            "derivativesHistory": derivatives_history,
            "microstructureSnapshot": microstructure_snapshot,
            "onchainMetrics": onchain_metrics,
            "onchainHealth": onchain_health,
            "rawDataManifest": raw_data_manifest,
            "decisionCards": decision_cards,
            "technicalIndicators": technical_indicators,
            "indicatorStates": indicator_states,
            "topFactors": factor_rows,
            "factorBacktests": factor_backtest_rows,
            "walkForward": walk_forward_rows,
            "factorSignals": factor_signal_rows,
            "equityCurve": [
                {
                    "timestamp": row.get("timestamp"),
                    "equityCurve": row.get("equity_curve"),
                    "netReturn": row.get("net_return"),
                }
                for row in returns_rows
            ],
            "marketPreview": market_rows,
            "artifacts": artifacts,
        }

    def validate_config(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("配置必须是JSON对象")
        if "data" not in config:
            raise ValueError("配置必须包含 data 设置")
        DataRequest.from_config(config["data"])
        normalize_adjustments(config.get("data", {}).get("adjustments", []))
        self._output_dir(config)

    def load_market_series(self, query: str) -> dict[str, Any]:
        params = parse_qs(query)
        requested_symbol = params.get("symbol", [""])[0]
        try:
            limit = max(20, min(int(params.get("limit", ["240"])[0]), 1000))
        except ValueError:
            limit = 240

        output_dir = self._output_dir(self.load_config())
        market_path = output_dir / "market_data.csv"
        technical_path = output_dir / "technical_indicators.csv"
        if not market_path.is_file():
            return {"symbols": [], "selectedSymbol": requested_symbol, "rows": []}

        frame = pd.read_csv(technical_path if technical_path.is_file() else market_path)
        if "symbol" not in frame.columns:
            return {"symbols": [], "selectedSymbol": requested_symbol, "rows": []}

        symbols = sorted(str(symbol) for symbol in frame["symbol"].dropna().unique())
        selected_symbol = requested_symbol if requested_symbol in symbols else (symbols[0] if symbols else "")
        if selected_symbol:
            frame = frame[frame["symbol"].astype(str) == selected_symbol]
        frame = frame.sort_values("timestamp").tail(limit)
        return {
            "symbols": symbols,
            "selectedSymbol": selected_symbol,
            "rows": [_json_ready(row) for row in frame.to_dict(orient="records")],
        }

    def _output_dir(self, config: dict[str, Any]) -> Path:
        return self._resolve_under_root(Path(str(config.get("output_dir", "runs/latest"))))

    def _resolve_under_root(self, path: Path) -> Path:
        resolved = path if path.is_absolute() else self.root / path
        resolved = resolved.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"路径必须位于当前工作区内：{path}") from exc
        return resolved


def create_admin_server(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    root: str | Path | None = None,
    admin_token: str | None = None,
    rate_limit: int = 240,
    rate_window_seconds: int = 60,
    cors_origins: str | Iterable[str] | None = None,
) -> ThreadingHTTPServer:
    app = AdminApp(config_path=config_path, root=root)
    static_dir = (Path(__file__).parent / "static").resolve()
    job_manager = AdminJobManager(app.run_store)
    realtime_service = OKXRealtimeService()
    limiter = RateLimiter(max_requests=rate_limit, window_seconds=rate_window_seconds)
    token = admin_token if admin_token is not None else os.environ.get("QUANT_FACTOR_ADMIN_TOKEN")
    allowed_cors_origins = _normalize_cors_origins(cors_origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "QuantFactorAdmin/0.1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not self._preflight(parsed.path):
                return
            try:
                if parsed.path == "/api/health":
                    self._send_json({"status": "ok"})
                elif parsed.path == "/api/config":
                    self._send_json({"configPath": str(app.config_path), "config": app.load_config()})
                elif parsed.path == "/api/summary":
                    self._send_json(app.load_snapshot())
                elif parsed.path == "/api/market":
                    self._send_json(app.load_market_series(parsed.query))
                elif parsed.path == "/api/runs":
                    self._send_json(app.list_runs(parsed.query))
                elif parsed.path.startswith("/api/runs/"):
                    run_id = parsed.path.rsplit("/", 1)[-1]
                    self._send_json(app.get_run(run_id))
                elif parsed.path == "/api/jobs":
                    self._send_json({"jobs": [job.to_dict() for job in job_manager.list()]})
                elif parsed.path.startswith("/api/jobs/"):
                    parts = [part for part in parsed.path.split("/") if part]
                    job_id = parts[2] if len(parts) >= 3 else ""
                    job = job_manager.get(job_id)
                    if job is None:
                        self._send_error_json(FileNotFoundError(job_id), status=HTTPStatus.NOT_FOUND)
                    elif len(parts) == 4 and parts[3] == "logs":
                        params = parse_qs(parsed.query)
                        try:
                            limit = max(1, min(int(params.get("limit", ["500"])[0]), 2000))
                        except ValueError:
                            limit = 500
                        try:
                            after_id = int(params["afterId"][0]) if "afterId" in params else None
                        except ValueError:
                            after_id = None
                        self._send_json({"logs": job_manager.logs(job_id, limit=limit, after_id=after_id)})
                    else:
                        self._send_json({"job": job.to_dict()})
                elif parsed.path == "/api/artifact":
                    self._send_artifact(parsed.query)
                elif parsed.path == "/api/realtime":
                    self._send_json(realtime_service.store.snapshot())
                else:
                    self._send_static(parsed.path, static_dir)
            except Exception as exc:
                self._send_error_json(exc)

        def do_PUT(self) -> None:
            try:
                parsed = urlparse(self.path)
                if not self._preflight(parsed.path):
                    return
                if parsed.path != "/api/config":
                    self._send_error_json(ValueError("未知接口"), status=HTTPStatus.NOT_FOUND)
                    return
                payload = self._read_json_body()
                config = payload.get("config", payload)
                saved = app.save_config(config)
                self._send_json({"configPath": str(app.config_path), "config": saved})
            except Exception as exc:
                self._send_error_json(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                if not self._preflight(parsed.path):
                    return
                if parsed.path.startswith("/api/jobs/"):
                    parts = [part for part in parsed.path.split("/") if part]
                    job_id = parts[2] if len(parts) >= 3 else ""
                    action = parts[3] if len(parts) >= 4 else ""
                    if action != "cancel":
                        self._send_error_json(ValueError("未知接口"), status=HTTPStatus.NOT_FOUND)
                        return
                    job = job_manager.cancel(job_id)
                    if job is None:
                        self._send_error_json(FileNotFoundError(job_id), status=HTTPStatus.NOT_FOUND)
                    else:
                        self._send_json({"job": job.to_dict()}, status=HTTPStatus.ACCEPTED)
                    return
                if parsed.path == "/api/realtime/start":
                    active_config = app.load_config()
                    request = DataRequest.from_config(active_config["data"])
                    self._send_json(
                        realtime_service.start(request, active_config.get("realtime", {})),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if parsed.path == "/api/realtime/stop":
                    self._send_json(realtime_service.stop(), status=HTTPStatus.ACCEPTED)
                    return
                if parsed.path not in {"/api/run", "/api/jobs"}:
                    self._send_error_json(ValueError("未知接口"), status=HTTPStatus.NOT_FOUND)
                    return
                payload = self._read_json_body(optional=True)
                config = payload.get("config") if isinstance(payload, dict) else None
                if parsed.path == "/api/jobs":
                    context = app.prepare_run_context(config)
                    job = job_manager.start(
                        lambda pipeline_context: app.run_pipeline(context=context, pipeline_context=pipeline_context),
                        config_hash=context["configHash"],
                        output_dir=context["outputDir"],
                    )
                    self._send_json({"job": job.to_dict()}, status=HTTPStatus.ACCEPTED)
                else:
                    self._send_json(app.run_pipeline(config=config))
            except Exception as exc:
                self._send_error_json(exc)

        def do_OPTIONS(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/") and self.headers.get("Origin") and not self._cors_origin():
                self._send_json(
                    {"error": {"code": "CORS_ORIGIN_DENIED", "message": "Origin is not allowed"}},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            self.send_response(HTTPStatus.NO_CONTENT.value)
            self.send_header("Content-Length", "0")
            self._send_security_headers()
            self.end_headers()


        def _preflight(self, path: str) -> bool:
            client = self.client_address[0] if self.client_address else "unknown"
            if not limiter.allow(client):
                self._send_json(
                    {"error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
                    status=HTTPStatus.TOO_MANY_REQUESTS,
                )
                return False
            if token and path.startswith("/api/") and path != "/api/health" and not self._authorized():
                self._send_json(
                    {"error": {"code": "UNAUTHORIZED", "message": "Admin token is required"}},
                    status=HTTPStatus.UNAUTHORIZED,
                )
                return False
            return True

        def _authorized(self) -> bool:
            if not token:
                return True
            auth_header = self.headers.get("Authorization", "")
            expected_bearer = f"Bearer {token}"
            if hmac.compare_digest(auth_header, expected_bearer):
                return True
            token_header = self.headers.get("X-Admin-Token", "")
            return hmac.compare_digest(token_header, token)

        def _send_security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            csp = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            self.send_header("Content-Security-Policy", csp)
            self._send_cors_headers()

        def _cors_origin(self) -> str | None:
            origin = self.headers.get("Origin", "").strip()
            if not origin or not allowed_cors_origins:
                return None
            if "*" in allowed_cors_origins:
                return "*"
            normalized = _normalize_cors_origin(origin)
            if normalized in allowed_cors_origins:
                return normalized
            return None

        def _send_cors_headers(self) -> None:
            origin = self._cors_origin()
            if not origin:
                return
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Token")
            self.send_header("Access-Control-Max-Age", "600")


        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self, optional: bool = False) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0 and optional:
                return {}
            if length <= 0:
                raise ValueError("请求体不能为空")
            if length > 5_000_000:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是JSON对象")
            return payload

        def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(_json_ready(payload), indent=2).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, exc: Exception, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
            if isinstance(exc, FileNotFoundError):
                status = HTTPStatus.NOT_FOUND
            body = {"error": {"code": status.name, "message": str(exc)}}
            self._send_json(body, status=status)

        def _send_static(self, raw_path: str, static_root: Path) -> None:
            path = posixpath.normpath(unquote(raw_path)).lstrip("/")
            if path in ("", "."):
                path = "index.html"
            file_path = (static_root / path).resolve()
            try:
                file_path.relative_to(static_root)
            except ValueError:
                self._send_error_json(ValueError("静态资源路径越界"), status=HTTPStatus.NOT_FOUND)
                return
            if not file_path.is_file():
                self._send_error_json(FileNotFoundError(path), status=HTTPStatus.NOT_FOUND)
                return
            body = file_path.read_bytes()
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or file_path.suffix in {".js", ".json"}:
                content_type = f"{content_type}; charset=utf-8"
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _send_artifact(self, query: str) -> None:
            params = parse_qs(query)
            name = params.get("name", [""])[0]
            if not name:
                raise ValueError("产物名称不能为空")
            output_dir = app._output_dir(app.load_config())
            file_path = (output_dir / name).resolve()
            try:
                file_path.relative_to(output_dir)
            except ValueError:
                raise ValueError("产物路径越界")
            if not file_path.is_file():
                raise FileNotFoundError(name)
            body = file_path.read_bytes()
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, int(port)), Handler)


def serve_admin(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    admin_token: str | None = None,
    rate_limit: int = 240,
    rate_window_seconds: int = 60,
    cors_origins: str | Iterable[str] | None = None,
) -> None:
    server = create_admin_server(
        config_path=config_path,
        host=host,
        port=port,
        admin_token=admin_token,
        rate_limit=rate_limit,
        rate_window_seconds=rate_window_seconds,
        cors_origins=cors_origins,
    )
    address = f"http://{server.server_address[0]}:{server.server_address[1]}"
    print(f"Quant Factor Lab admin running at {address}")
    print(f"Config: {Path(config_path).resolve()}")
    if admin_token or os.environ.get("QUANT_FACTOR_ADMIN_TOKEN"):
        print("Admin token protection enabled")
    active_cors_origins = _normalize_cors_origins(cors_origins)
    if active_cors_origins:
        print(f"CORS origins: {', '.join(sorted(active_cors_origins))}")
    server.serve_forever()


def _normalize_cors_origins(origins: str | Iterable[str] | None) -> set[str]:
    if origins is None:
        origins = os.environ.get("QUANT_FACTOR_ADMIN_CORS_ORIGINS", "")
    if isinstance(origins, str):
        items = origins.split(",")
    else:
        items = []
        for origin in origins:
            items.extend(str(origin).split(","))
    return {
        normalized
        for item in items
        if (normalized := _normalize_cors_origin(str(item)))
    }


def _normalize_cors_origin(origin: str) -> str:
    value = origin.strip()
    if not value:
        return ""
    if value == "*":
        return "*"
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return value.rstrip("/")


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _read_csv_records(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    frame = pd.read_csv(path)
    if len(frame) > limit:
        frame = frame.tail(limit)
    return [_json_ready(row) for row in frame.to_dict(orient="records")]


def _json_ready(value: Any) -> Any:
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
