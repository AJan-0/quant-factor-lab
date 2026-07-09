from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quant_factor_lab.admin.server import AdminJobManager
from quant_factor_lab.admin.server import create_admin_server
from quant_factor_lab.run_store import RunStore


def admin_test_config() -> dict:
    return {
        "data": {
            "provider": "synthetic",
            "start": "2022-01-01",
            "end": "2022-04-01",
            "frequency": "1d",
            "seed": 11,
            "universe": [
                {"symbol": "BTC-USD", "asset_class": "crypto"},
                {"symbol": "ETH-USD", "asset_class": "crypto"},
                {"symbol": "AAPL", "asset_class": "equity"},
            ],
            "adjustments": [],
        },
        "mining": {"enable_operator_miner": True, "operator_windows": [3, 5]},
        "evaluation": {"forward_horizons": [1], "min_observations": 20},
        "backtest": {"horizon": 1, "top_n": 1, "bottom_n": 1},
        "output_dir": "runs/admin-test",
    }


class AdminServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(admin_test_config()), encoding="utf-8")
        self.server = create_admin_server(self.config_path, host="127.0.0.1", port=0, root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tempdir.cleanup()

    def test_config_endpoint_reads_and_saves_adjustments(self) -> None:
        payload = self.get_json("/api/config")
        config = payload["config"]
        config["data"]["adjustments"] = [
            {
                "symbol": "BTC-USD",
                "field": "close",
                "operation": "multiply",
                "value": 1.02,
                "start": "2022-02-01",
                "end": "2022-02-10",
            }
        ]

        saved = self.request_json("PUT", "/api/config", {"config": config})

        self.assertEqual(saved["config"]["data"]["adjustments"][0]["value"], 1.02)
        self.assertIn("config.json", saved["configPath"])

    def test_summary_reports_empty_state_before_first_run(self) -> None:
        payload = self.get_json("/api/summary")

        self.assertFalse(payload["hasRun"])
        self.assertEqual(payload["topFactors"], [])
        self.assertEqual(payload["factorSignals"], [])
        self.assertIsNone(payload["sourceHealth"])
        self.assertEqual(payload["sourceStatusRows"], [])
        self.assertEqual(payload["derivativesSnapshot"], [])
        self.assertEqual(payload["derivativesHistory"], [])
        self.assertEqual(payload["microstructureSnapshot"], [])
        self.assertEqual(payload["onchainMetrics"], [])
        self.assertIsNone(payload["onchainHealth"])
        self.assertEqual(payload["rawDataManifest"], [])
        self.assertEqual(payload["decisionCards"], [])
        self.assertEqual(payload["technicalIndicators"], [])
        self.assertEqual(payload["indicatorStates"], [])
        self.assertEqual(payload["walkForward"], [])
        self.assertEqual(payload["equityCurve"], [])

    def test_market_endpoint_returns_symbol_series(self) -> None:
        market_dir = self.root / "runs" / "admin-test"
        market_dir.mkdir(parents=True)
        (market_dir / "market_data.csv").write_text(
            "\n".join(
                [
                    "timestamp,symbol,open,high,low,close,volume,asset_class,frequency",
                    "2022-01-01,BTC-USD,10,12,9,11,100,crypto,1d",
                    "2022-01-02,BTC-USD,11,14,10,13,120,crypto,1d",
                    "2022-01-01,ETH-USD,20,22,19,21,200,crypto,1d",
                ]
            ),
            encoding="utf-8",
        )

        payload = self.get_json("/api/market?symbol=BTC-USD&limit=20")

        self.assertEqual(payload["selectedSymbol"], "BTC-USD")
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["rows"][-1]["close"], 13)

    def test_config_endpoint_rejects_invalid_adjustments(self) -> None:
        config = admin_test_config()
        config["data"]["adjustments"] = [{"symbol": "BTC-USD", "field": "bad", "operation": "multiply", "value": 1}]

        with self.assertRaises(HTTPError) as error:
            self.request_json("PUT", "/api/config", {"config": config})

        self.assertEqual(error.exception.code, 400)

    def test_security_headers_are_sent(self) -> None:
        with urlopen(f"{self.base_url}/api/health", timeout=10) as response:
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_realtime_endpoint_reports_status(self) -> None:
        payload = self.get_json("/api/realtime")

        self.assertEqual(payload["status"], "stopped")
        self.assertIn("orderBooks", payload)
        self.assertIn("liquidations", payload)

    def test_token_protects_admin_api(self) -> None:
        server = create_admin_server(self.config_path, host="127.0.0.1", port=0, root=self.root, admin_token="secret")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{base_url}/api/config", timeout=10)
            self.assertEqual(error.exception.code, 401)

            request = Request(f"{base_url}/api/config", headers={"Authorization": "Bearer secret"})
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertIn("config", payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_cors_preflight_allows_configured_pages_origin(self) -> None:
        origin = "https://ajan-0.github.io"
        server = create_admin_server(
            self.config_path,
            host="127.0.0.1",
            port=0,
            root=self.root,
            admin_token="secret",
            cors_origins=[origin],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            request = Request(
                f"{base_url}/api/config",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "content-type, authorization",
                },
                method="OPTIONS",
            )

            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
                self.assertIn("PUT", response.headers["Access-Control-Allow-Methods"])
                self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])
        finally:
            server.shutdown()
            server.server_close()

    def test_cors_response_headers_are_sent_for_allowed_origin(self) -> None:
        origin = "https://ajan-0.github.io"
        server = create_admin_server(
            self.config_path,
            host="127.0.0.1",
            port=0,
            root=self.root,
            cors_origins=[origin],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            request = Request(f"{base_url}/api/health", headers={"Origin": origin})

            with urlopen(request, timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
                self.assertEqual(response.headers["Vary"], "Origin")
        finally:
            server.shutdown()
            server.server_close()

    def test_cors_preflight_rejects_unconfigured_origin(self) -> None:
        server = create_admin_server(
            self.config_path,
            host="127.0.0.1",
            port=0,
            root=self.root,
            cors_origins=["https://ajan-0.github.io"],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            request = Request(
                f"{base_url}/api/config",
                headers={
                    "Origin": "https://example.invalid",
                    "Access-Control-Request-Method": "PUT",
                },
                method="OPTIONS",
            )

            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=10)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_rate_limit_rejects_excess_requests(self) -> None:
        server = create_admin_server(self.config_path, host="127.0.0.1", port=0, root=self.root, rate_limit=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        base_url = f"http://{host}:{port}"
        try:
            with urlopen(f"{base_url}/api/health", timeout=10) as response:
                self.assertEqual(response.status, 200)
            with self.assertRaises(HTTPError) as error:
                urlopen(f"{base_url}/api/health", timeout=10)
            self.assertEqual(error.exception.code, 429)
        finally:
            server.shutdown()
            server.server_close()

    def test_job_endpoint_tracks_pipeline_run(self) -> None:
        started = self.request_json("POST", "/api/jobs", {"config": admin_test_config()})
        job_id = started["job"]["id"]
        job = self.wait_for_job(job_id)

        self.assertEqual(job["status"], "succeeded")
        self.assertTrue(job["result"]["snapshot"]["hasRun"])
        self.assertEqual(job["result"]["snapshot"]["dataQuality"]["summary"]["status"], "PASS")
        self.assertEqual(job["result"]["snapshot"]["sourceHealth"]["summary"]["status"], "SIMULATED")
        self.assertGreater(len(job["result"]["snapshot"]["sourceStatusRows"]), 0)
        self.assertGreater(len(job["result"]["snapshot"]["dataQualityRows"]), 0)
        self.assertIn("derivativesSnapshot", job["result"]["snapshot"])
        self.assertIn("derivativesHistory", job["result"]["snapshot"])
        self.assertIn("microstructureSnapshot", job["result"]["snapshot"])
        self.assertIn("onchainMetrics", job["result"]["snapshot"])
        self.assertIn("onchainHealth", job["result"]["snapshot"])
        self.assertIn("rawDataManifest", job["result"]["snapshot"])
        self.assertIn("decisionCards", job["result"]["snapshot"])
        self.assertGreater(len(job["result"]["snapshot"]["technicalIndicators"]), 0)
        self.assertGreater(len(job["result"]["snapshot"]["indicatorStates"]), 0)
        self.assertIn("walkForward", job["result"]["snapshot"])
        self.assertGreater(len(job["result"]["snapshot"]["factorBacktests"]), 0)
        self.assertIn("sharpe", job["result"]["snapshot"]["factorBacktests"][0])
        self.assertGreater(len(job["result"]["snapshot"]["factorSignals"]), 0)
        self.assertIn("signal", job["result"]["snapshot"]["factorSignals"][0])
        self.assertIn("confidence", job["result"]["snapshot"]["factorSignals"][0])
        self.assertEqual(job["runId"], job_id)

        runs = self.get_json("/api/runs?limit=5")["runs"]
        self.assertEqual(runs[0]["id"], job_id)
        self.assertEqual(runs[0]["status"], "succeeded")
        self.assertEqual(runs[0]["summary"]["factor_count"], job["result"]["summary"]["factor_count"])

        run = self.get_json(f"/api/runs/{job_id}")["run"]
        self.assertEqual(run["configHash"], job["configHash"])

        logs = self.get_json(f"/api/jobs/{job_id}/logs")["logs"]
        self.assertGreater(len(logs), 0)
        self.assertIn("Job started", [log["message"] for log in logs])

    def test_failed_job_is_recorded_in_run_history(self) -> None:
        config = admin_test_config()
        config["data"]["provider"] = "csv"
        config["data"]["path"] = str(self.root / "missing.csv")

        started = self.request_json("POST", "/api/jobs", {"config": config})
        job_id = started["job"]["id"]
        job = self.wait_for_job(job_id)

        self.assertEqual(job["status"], "failed")
        self.assertIn("missing.csv", job["error"])

        run = self.get_json(f"/api/runs/{job_id}")["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("missing.csv", run["error"])

    def test_job_manager_supports_cooperative_cancel_and_logs(self) -> None:
        store = RunStore(self.root / "runs" / "job-manager.sqlite3")
        manager = AdminJobManager(store)

        def target(context):
            for index in range(100):
                context.checkpoint(f"step {index}")
                time.sleep(0.01)
            return {"summary": {"factor_count": 1}}

        job = manager.start(target, config_hash="abc", output_dir="runs/cancel-test")
        time.sleep(0.03)
        canceled = manager.cancel(job.id)
        self.assertIsNotNone(canceled)

        for _ in range(50):
            current = manager.get(job.id)
            if current and current.status == "canceled":
                break
            time.sleep(0.05)

        current = manager.get(job.id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.status, "canceled")
        self.assertTrue(current.cancel_requested)
        self.assertTrue(any(log["level"] == "WARN" for log in manager.logs(job.id)))

        saved = store.get_run(job.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, "canceled")

    def wait_for_job(self, job_id: str) -> dict:
        job = self.get_json(f"/api/jobs/{job_id}")["job"]
        for _ in range(40):
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.25)
            job = self.get_json(f"/api/jobs/{job_id}")["job"]
        return job

    def get_json(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def request_json(self, method: str, path: str, payload: dict) -> dict:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
