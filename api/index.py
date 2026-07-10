from __future__ import annotations

import json
import hmac
import os
import time
import uuid
from importlib import resources
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from quant_factor_lab.admin.server import AdminApp, _json_ready
from quant_factor_lab.microstructure import OKXMicrostructureSnapshotProvider, okx_spot_inst_id
from quant_factor_lab.realtime import _book_row, _trade_row
from quant_factor_lab.run_store import hash_config
from quant_factor_lab.runtime import PipelineCancelled, PipelineContext
from quant_factor_lab.types import AssetClass, DataRequest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = PROJECT_ROOT / "public"
RUNTIME_ROOT = Path(os.environ.get("QFL_VERCEL_RUNTIME_ROOT", "/tmp/quant-factor-lab")).resolve()
CONFIG_PATH = RUNTIME_ROOT / "config.json"
JOBS_DIR = RUNTIME_ROOT / "jobs"
REALTIME_CACHE_PATH = RUNTIME_ROOT / "realtime.json"
REALTIME_CACHE_SECONDS = int(os.environ.get("QFL_VERCEL_REALTIME_CACHE_SECONDS", "15"))
STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class handler(BaseHTTPRequestHandler):
    server_version = "QuantFactorVercel/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self._send_common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._preflight(parsed.path):
                return
            if self._send_static_route(parsed.path):
                return
            app = _admin_app()
            if parsed.path == "/api/health":
                self._send_json({"status": "ok", "mode": "vercel"})
            elif parsed.path == "/api/config":
                self._send_json({"configPath": "vercel:/tmp/config.json", "config": app.load_config()})
            elif parsed.path == "/api/summary":
                self._send_json(app.load_snapshot())
            elif parsed.path == "/api/market":
                self._send_json(app.load_market_series(parsed.query))
            elif parsed.path == "/api/runs":
                self._send_json(app.list_runs(parsed.query))
            elif parsed.path.startswith("/api/runs/"):
                self._send_json(app.get_run(parsed.path.rsplit("/", 1)[-1]))
            elif parsed.path == "/api/jobs":
                self._send_json({"jobs": _list_jobs()})
            elif parsed.path.startswith("/api/jobs/"):
                self._send_job_route(app, parsed.path, parsed.query)
            elif parsed.path == "/api/realtime":
                self._send_json(_realtime_snapshot(app.load_config()))
            elif parsed.path == "/api/artifact":
                self._send_artifact(app, parsed.query)
            else:
                self._send_error_json(ValueError("Unknown API route"), status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._preflight(parsed.path):
                return
            app = _admin_app()
            if parsed.path != "/api/config":
                self._send_error_json(ValueError("Unknown API route"), status=HTTPStatus.NOT_FOUND)
                return
            payload = self._read_json_body()
            config = payload.get("config", payload)
            saved = app.save_config(_vercel_safe_config(config))
            self._send_json({"configPath": "vercel:/tmp/config.json", "config": saved})
        except Exception as exc:
            self._send_error_json(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not self._preflight(parsed.path):
                return
            app = _admin_app()
            if parsed.path in {"/api/jobs", "/api/run"}:
                payload = self._read_json_body(optional=True)
                config = payload.get("config") if isinstance(payload, dict) else None
                job = _run_job(app, config)
                self._send_json({"job": job} if parsed.path == "/api/jobs" else job.get("result"), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/realtime/start":
                self._send_json(_realtime_snapshot(app.load_config(), force_refresh=True), status=HTTPStatus.ACCEPTED)
            elif parsed.path == "/api/realtime/stop":
                self._send_json(_stopped_realtime_snapshot(), status=HTTPStatus.ACCEPTED)
            elif parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                job_id = [part for part in parsed.path.split("/") if part][2]
                job = _load_job(job_id)
                if job is None:
                    self._send_error_json(FileNotFoundError(job_id), status=HTTPStatus.NOT_FOUND)
                else:
                    self._send_json({"job": job}, status=HTTPStatus.ACCEPTED)
            else:
                self._send_error_json(ValueError("Unknown API route"), status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def _send_job_route(self, app: AdminApp, path: str, query: str) -> None:
        parts = [part for part in path.split("/") if part]
        job_id = parts[2] if len(parts) >= 3 else ""
        if len(parts) == 4 and parts[3] == "logs":
            params = parse_qs(query)
            try:
                limit = max(1, min(int(params.get("limit", ["500"])[0]), 2000))
            except ValueError:
                limit = 500
            try:
                after_id = int(params["afterId"][0]) if "afterId" in params else None
            except ValueError:
                after_id = None
            self._send_json({"logs": [record.to_dict() for record in app.run_store.list_logs(job_id, limit=limit, after_id=after_id)]})
            return
        job = _load_job(job_id)
        if job is None:
            self._send_error_json(FileNotFoundError(job_id), status=HTTPStatus.NOT_FOUND)
            return
        self._send_json({"job": job})

    def _send_artifact(self, app: AdminApp, query: str) -> None:
        params = parse_qs(query)
        name = params.get("name", [""])[0]
        if not name:
            raise ValueError("Artifact name is required")
        output_dir = app._output_dir(app.load_config())
        file_path = (output_dir / name).resolve()
        file_path.relative_to(output_dir)
        if not file_path.is_file():
            raise FileNotFoundError(name)
        body = file_path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self._send_common_headers(content_type="application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static_route(self, path: str) -> bool:
        if path in {"", "/", "/index.html"}:
            return self._send_public_file("index.html")
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT.value)
            self._send_common_headers(content_type="image/x-icon")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        static_name = path.lstrip("/")
        if "/" in static_name or "\\" in static_name:
            return False
        if static_name in {"app.css", "app.js", "runtime-config.js"}:
            return self._send_public_file(static_name)
        return False

    def _send_public_file(self, name: str) -> bool:
        loaded = _load_static_asset(name)
        if loaded is None:
            return False
        body, suffix = loaded
        content_type = STATIC_CONTENT_TYPES.get(suffix, "application/octet-stream")
        if content_type.startswith("text/") or "javascript" in content_type or "json" in content_type:
            body = body.replace(b"QFL_STATIC_SITE = true", b"QFL_STATIC_SITE = false")
        self.send_response(HTTPStatus.OK.value)
        self._send_common_headers(content_type=content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _preflight(self, path: str) -> bool:
        token = os.environ.get("QUANT_FACTOR_ADMIN_TOKEN")
        if token and path.startswith("/api/") and path != "/api/health" and not self._authorized(token):
            self._send_json(
                {"error": {"code": "UNAUTHORIZED", "message": "Admin token is required"}},
                status=HTTPStatus.UNAUTHORIZED,
            )
            return False
        return True

    def _authorized(self, token: str) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {token}"):
            return True
        return hmac.compare_digest(self.headers.get("X-Admin-Token", ""), token)

    def _read_json_body(self, optional: bool = False) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0 and optional:
            return {}
        if length <= 0:
            raise ValueError("Request body is required")
        if length > 5_000_000:
            raise ValueError("Request body is too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(_json_ready(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self._send_common_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, exc: Exception, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        if isinstance(exc, FileNotFoundError):
            status = HTTPStatus.NOT_FOUND
        self._send_json({"error": {"code": status.name, "message": str(exc)}}, status=status)

    def _send_common_headers(self, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        origin = self.headers.get("Origin", "").strip()
        allowed = _allowed_cors_origins()
        if origin and ("*" in allowed or origin in allowed):
            self.send_header("Access-Control-Allow-Origin", "*" if "*" in allowed else origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Admin-Token")
            self.send_header("Access-Control-Max-Age", "600")

    def log_message(self, format: str, *args: Any) -> None:
        return


def _admin_app() -> AdminApp:
    _ensure_runtime_config()
    return AdminApp(config_path=CONFIG_PATH, root=RUNTIME_ROOT)


def _load_static_asset(name: str) -> tuple[bytes, str] | None:
    local_path = (PUBLIC_ROOT / name).resolve()
    try:
        local_path.relative_to(PUBLIC_ROOT)
    except ValueError:
        return None
    if local_path.is_file():
        return local_path.read_bytes(), local_path.suffix
    try:
        packaged = resources.files("quant_factor_lab.admin").joinpath("static", name)
        if packaged.is_file():
            return packaged.read_bytes(), Path(name).suffix
    except (FileNotFoundError, ModuleNotFoundError):
        return None
    return None


def _ensure_runtime_config() -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.is_file():
        return
    default_config = json.loads((PROJECT_ROOT / "examples" / "demo_config.json").read_text(encoding="utf-8"))
    default_config = _vercel_safe_config(default_config)
    default_config["output_dir"] = "runs/vercel-latest"
    CONFIG_PATH.write_text(json.dumps(default_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_job(app: AdminApp, config: dict[str, Any] | None) -> dict[str, Any]:
    config = _vercel_safe_config(config) if config is not None else None
    context = app.prepare_run_context(config)
    job_id = uuid.uuid4().hex
    now = time.time()
    app.run_store.create_run(run_id=job_id, config_hash=context["configHash"], output_dir=context["outputDir"])
    app.run_store.mark_running(job_id)
    app.run_store.append_log(job_id, "INFO", "Vercel serverless job started")
    job = {
        "id": job_id,
        "status": "running",
        "createdAt": _format_timestamp(now),
        "startedAt": _format_timestamp(now),
        "finishedAt": None,
        "error": None,
        "configHash": context["configHash"],
        "outputDir": context["outputDir"],
        "runId": job_id,
        "cancelRequested": False,
        "result": None,
    }
    try:
        pipeline_context = PipelineContext(
            run_id=job_id,
            log=lambda level, message: app.run_store.append_log(job_id, level, message),
            is_cancelled=lambda: False,
        )
        result = app.run_pipeline(context=context, pipeline_context=pipeline_context)
    except PipelineCancelled as exc:
        app.run_store.mark_canceled(job_id, str(exc))
        job.update({"status": "canceled", "error": str(exc), "finishedAt": _format_timestamp(time.time()), "cancelRequested": True})
    except Exception as exc:
        app.run_store.append_log(job_id, "ERROR", str(exc))
        app.run_store.mark_failed(job_id, str(exc))
        job.update({"status": "failed", "error": str(exc), "finishedAt": _format_timestamp(time.time())})
    else:
        app.run_store.append_log(job_id, "INFO", "Vercel serverless job succeeded")
        app.run_store.mark_succeeded(job_id, result.get("summary", {}))
        job.update({"status": "succeeded", "finishedAt": _format_timestamp(time.time()), "result": result})
    _save_job(job)
    return job


def _vercel_safe_config(config: dict[str, Any]) -> dict[str, Any]:
    safe_config = json.loads(json.dumps(config))
    if os.environ.get("QFL_VERCEL_ENABLE_ML", "").lower() not in {"1", "true", "yes"}:
        safe_config.setdefault("mining", {})["enable_ml_miner"] = False
    return safe_config


def _save_job(job: dict[str, Any]) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(_json_ready(job), ensure_ascii=False), encoding="utf-8")


def _load_job(job_id: str) -> dict[str, Any] | None:
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _list_jobs() -> list[dict[str, Any]]:
    if not JOBS_DIR.is_dir():
        return []
    jobs = [_load_job(path.stem) for path in JOBS_DIR.glob("*.json")]
    return sorted([job for job in jobs if job], key=lambda item: item.get("createdAt") or "", reverse=True)[:50]


def _realtime_snapshot(config: dict[str, Any], force_refresh: bool = False) -> dict[str, Any]:
    if not force_refresh:
        cached = _load_realtime_cache()
        if cached:
            return cached
    try:
        payload = _fetch_okx_realtime_payload(config)
    except Exception as exc:
        payload = {
            "status": "error",
            "error": str(exc),
            "startedAt": None,
            "stoppedAt": None,
            "messageCount": 0,
            "subscribedArgs": [],
            "orderBooks": [],
            "trades": [],
            "liquidations": [],
            "events": [_event("ERROR", str(exc))],
        }
    REALTIME_CACHE_PATH.write_text(json.dumps(_json_ready(payload), ensure_ascii=False), encoding="utf-8")
    return payload


def _fetch_okx_realtime_payload(config: dict[str, Any]) -> dict[str, Any]:
    request = DataRequest.from_config(config.get("data", {}))
    micro_config = config.get("microstructure", {})
    provider = OKXMicrostructureSnapshotProvider(
        timeout=float(micro_config.get("timeout", config.get("data", {}).get("timeout", 8.0))),
        depth_size=int(micro_config.get("depth_size", 20)),
        trade_limit=int(micro_config.get("trade_limit", 40)),
    )
    order_books: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for instrument in request.universe:
        if instrument.asset_class != AssetClass.CRYPTO:
            continue
        inst_id = okx_spot_inst_id(instrument)
        book_payload = provider._default_fetch_json(provider.BOOKS_PATH, {"instId": inst_id, "sz": str(provider.depth_size)})
        trades_payload = provider._default_fetch_json(provider.TRADES_PATH, {"instId": inst_id, "limit": str(provider.trade_limit)})
        book = (book_payload.get("data") or [{}])[0]
        order_books.append(_book_row(inst_id, book))
        trades.extend(_trade_row(inst_id, item) for item in (trades_payload.get("data") or []))
    return {
        "status": "running",
        "error": None,
        "startedAt": _format_timestamp(time.time()),
        "stoppedAt": None,
        "messageCount": len(order_books) + len(trades),
        "subscribedArgs": [],
        "orderBooks": order_books,
        "trades": sorted(trades, key=lambda row: row.get("timestamp") or "", reverse=True)[:80],
        "liquidations": [],
        "events": [_event("INFO", "Vercel mode uses OKX REST polling snapshots instead of a long-lived WebSocket.")],
    }


def _load_realtime_cache() -> dict[str, Any] | None:
    if not REALTIME_CACHE_PATH.is_file():
        return None
    if time.time() - REALTIME_CACHE_PATH.stat().st_mtime > REALTIME_CACHE_SECONDS:
        return None
    return json.loads(REALTIME_CACHE_PATH.read_text(encoding="utf-8"))


def _stopped_realtime_snapshot() -> dict[str, Any]:
    payload = {
        "status": "stopped",
        "error": None,
        "startedAt": None,
        "stoppedAt": _format_timestamp(time.time()),
        "messageCount": 0,
        "subscribedArgs": [],
        "orderBooks": [],
        "trades": [],
        "liquidations": [],
        "events": [_event("INFO", "Realtime polling stopped in this browser session.")],
    }
    REALTIME_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _allowed_cors_origins() -> set[str]:
    return {item.strip().rstrip("/") for item in os.environ.get("QUANT_FACTOR_ADMIN_CORS_ORIGINS", "").split(",") if item.strip()}


def _event(level: str, message: str) -> dict[str, Any]:
    return {"timestamp": _format_timestamp(time.time()), "level": level.upper(), "message": message}


def _format_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
