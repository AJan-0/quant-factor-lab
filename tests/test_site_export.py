from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quant_factor_lab.site_export import export_static_site


def site_export_config() -> dict:
    return {
        "data": {
            "provider": "synthetic",
            "start": "2022-01-01",
            "end": "2022-03-01",
            "frequency": "1d",
            "universe": [
                {"symbol": "BTC-USD", "asset_class": "crypto"},
                {"symbol": "ETH-USD", "asset_class": "crypto"},
            ],
            "adjustments": [],
        },
        "mining": {"enable_operator_miner": True, "operator_windows": [3, 5]},
        "evaluation": {"forward_horizons": [1], "min_observations": 20},
        "backtest": {"horizon": 1, "top_n": 1, "bottom_n": 1},
        "output_dir": "runs/static-test",
    }


class StaticSiteExportTests(unittest.TestCase):
    def test_export_static_site_writes_pages_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(site_export_config()), encoding="utf-8")
            output_dir = root / "runs" / "static-test"
            output_dir.mkdir(parents=True)
            (output_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "rows": 4,
                        "symbols": ["BTC-USD", "ETH-USD"],
                        "output_dir": str(output_dir),
                        "outputs": {"market_data": str(output_dir / "market_data.csv")},
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "market_data.csv").write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,asset_class,frequency",
                        "2022-01-01,BTC-USD,10,12,9,11,100,crypto,1d",
                        "2022-01-02,BTC-USD,11,13,10,12,110,crypto,1d",
                        "2022-01-01,ETH-USD,20,22,18,21,200,crypto,1d",
                        "2022-01-02,ETH-USD,21,24,20,23,210,crypto,1d",
                    ]
                ),
                encoding="utf-8",
            )

            manifest = export_static_site(config_path=config_path, site_dir=root / "site", root=root)

            site_dir = root / "site"
            self.assertEqual(manifest["mode"], "static")
            self.assertTrue((site_dir / "index.html").is_file())
            self.assertTrue((site_dir / ".nojekyll").is_file())
            self.assertIn("QFL_STATIC_SITE = true", (site_dir / "runtime-config.js").read_text(encoding="utf-8"))
            self.assertTrue((site_dir / "api" / "summary.json").is_file())
            self.assertTrue((site_dir / "api" / "market.json").is_file())
            self.assertTrue((site_dir / "artifacts" / "market_data.csv").is_file())

            market_payload = json.loads((site_dir / "api" / "market.json").read_text(encoding="utf-8"))
            self.assertEqual(market_payload["symbols"], ["BTC-USD", "ETH-USD"])
            self.assertEqual(len(market_payload["rows"]), 4)

            summary_text = (site_dir / "api" / "summary.json").read_text(encoding="utf-8")
            self.assertNotIn(str(root), summary_text)
            self.assertIn("./runs/static-test", summary_text.replace("\\", "/"))

    def test_export_static_site_can_point_to_remote_api(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(site_export_config()), encoding="utf-8")

            manifest = export_static_site(
                config_path=config_path,
                site_dir=root / "site",
                root=root,
                api_base_url="https://api.example.com/qfl/",
            )

            runtime_config = (root / "site" / "runtime-config.js").read_text(encoding="utf-8")
            self.assertEqual(manifest["apiBaseUrl"], "https://api.example.com/qfl/")
            self.assertIn("QFL_STATIC_SITE = false", runtime_config)
            self.assertIn('"https://api.example.com/qfl/"', runtime_config)

    def test_export_static_site_refuses_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(site_export_config()), encoding="utf-8")

            with self.assertRaises(ValueError):
                export_static_site(config_path=config_path, site_dir=root, root=root)


if __name__ == "__main__":
    unittest.main()
