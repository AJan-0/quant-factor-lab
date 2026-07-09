from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from quant_factor_lab.raw_store import RawDataVersionStore, dataframe_content_hash, raw_snapshot_frame


class RawDataVersionStoreTests(unittest.TestCase):
    def test_records_frame_with_content_hash_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = pd.DataFrame(
                [
                    {"timestamp": "2026-07-01", "symbol": "ETH-USD", "close": 3000.0},
                    {"timestamp": "2026-07-02", "symbol": "ETH-USD", "close": 3100.0},
                ]
            )
            store = RawDataVersionStore(Path(tmpdir) / "raw.sqlite3")

            snapshot = store.record_frame(
                run_id="run-1",
                dataset="market_data",
                provider="okx",
                frame=frame,
                artifact_path=Path(tmpdir) / "market_data.csv",
                frequency="1d",
            )

            self.assertEqual(snapshot.rows, 2)
            self.assertEqual(snapshot.symbols, ["ETH-USD"])
            self.assertEqual(snapshot.content_hash, dataframe_content_hash(frame))

            saved = store.list_snapshots(run_id="run-1")
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].snapshot_id, snapshot.snapshot_id)

            manifest = raw_snapshot_frame(saved)
            self.assertEqual(manifest.iloc[0]["dataset"], "market_data")
            self.assertEqual(manifest.iloc[0]["symbols"], "ETH-USD")


if __name__ == "__main__":
    unittest.main()
