from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import api.index as vercel_api


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VercelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name).resolve()
        vercel_api.RUNTIME_ROOT = root
        vercel_api.CONFIG_PATH = root / "config.json"
        vercel_api.JOBS_DIR = root / "jobs"
        vercel_api.REALTIME_CACHE_PATH = root / "realtime.json"
        os.environ["QUANT_FACTOR_ADMIN_CORS_ORIGINS"] = "https://ajan-0.github.io"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), vercel_api.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.environ.pop("QUANT_FACTOR_ADMIN_CORS_ORIGINS", None)
        self.tempdir.cleanup()

    def test_health_endpoint_reports_vercel_mode(self) -> None:
        payload = self.get_json("/api/health")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "vercel")

    def test_config_endpoint_uses_tmp_runtime_root(self) -> None:
        payload = self.get_json("/api/config")

        self.assertEqual(payload["configPath"], "vercel:/tmp/config.json")
        self.assertEqual(payload["config"]["output_dir"], "runs/vercel-latest")
        self.assertFalse(payload["config"]["mining"]["enable_ml_miner"])
        self.assertIn("data", payload["config"])

    def test_options_preflight_sends_cors_headers(self) -> None:
        request = Request(
            f"{self.base_url}/api/config",
            headers={
                "Origin": "https://ajan-0.github.io",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type",
            },
            method="OPTIONS",
        )

        with urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://ajan-0.github.io")

    def get_json(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


class VercelStaticAssetTests(unittest.TestCase):
    def test_public_assets_match_admin_static_assets(self) -> None:
        for name in ("index.html", "app.css", "app.js", "runtime-config.js"):
            with self.subTest(name=name):
                public_text = (PROJECT_ROOT / "public" / name).read_text(encoding="utf-8")
                static_text = (PROJECT_ROOT / "quant_factor_lab" / "admin" / "static" / name).read_text(encoding="utf-8")
                self.assertEqual(public_text, static_text)


if __name__ == "__main__":
    unittest.main()
