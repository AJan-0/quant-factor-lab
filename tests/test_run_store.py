from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from quant_factor_lab.run_store import RunStore, hash_config


class RunStoreTests(unittest.TestCase):
    def test_create_update_and_list_run_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(Path(tmpdir) / "runs.sqlite3")
            run_id = "run-1"
            config_hash = hash_config({"data": {"provider": "synthetic"}})

            created = store.create_run(run_id=run_id, config_hash=config_hash, output_dir="runs/demo")
            store.mark_running(run_id)
            store.mark_succeeded(run_id, {"factor_count": 12, "backtest_metrics": {"sharpe": 1.25}})

            saved = store.get_run(run_id)
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(created.id, run_id)
            self.assertEqual(saved.status, "succeeded")
            self.assertEqual(saved.config_hash, config_hash)
            self.assertEqual(saved.output_dir, "runs/demo")
            self.assertEqual(saved.summary["factor_count"], 12)
            self.assertIsNone(saved.error)

            runs = store.list_runs(limit=10)
            self.assertEqual([run.id for run in runs], [run_id])

    def test_failed_run_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(Path(tmpdir) / "runs.sqlite3")
            store.create_run(run_id="run-2", config_hash="abc", output_dir="runs/demo")

            store.mark_running("run-2")
            store.mark_failed("run-2", "pipeline failed")

            saved = store.get_run("run-2")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved.status, "failed")
            self.assertEqual(saved.error, "pipeline failed")
            self.assertIsNone(saved.summary)

    def test_logs_and_canceled_status_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(Path(tmpdir) / "runs.sqlite3")
            store.create_run(run_id="run-3", config_hash="abc", output_dir="runs/demo")

            first = store.append_log("run-3", "info", "started")
            second = store.append_log("run-3", "warn", "cancel requested")
            store.mark_canceled("run-3", "user requested cancellation")

            logs = store.list_logs("run-3")
            self.assertEqual([log.message for log in logs], ["started", "cancel requested"])
            self.assertEqual(logs[0].level, "INFO")
            self.assertEqual(store.list_logs("run-3", after_id=first.id)[0].id, second.id)

            saved = store.get_run("run-3")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved.status, "canceled")
            self.assertEqual(saved.error, "user requested cancellation")

    def test_hash_config_is_stable_for_key_order(self) -> None:
        left = hash_config({"b": 2, "a": {"x": 1}})
        right = hash_config({"a": {"x": 1}, "b": 2})

        self.assertEqual(left, right)
        self.assertEqual(len(left), 64)


