#!/usr/bin/env python3
"""
test_pool_gate_enforcement.py — Tests enforcing the pool evaluation gate for PARITY / IMPROVEMENT.

Verifies:
1. A PARITY experiment whose matrix returns PARITY produces no final/accepted state
   and no completed history record until pool data exists (and fails loudly if pool cannot run).
2. A matrix REVERT rejects immediately without pool.
3. A Rule 3 BUG FIX still completes without pool.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import ab_test
from ab_test import run_ab_test_engine, STRATEGY_BASELINE, STRATEGY_VARIANT


class TestPoolGateEnforcement(unittest.TestCase):
    def setUp(self):
        self.temp_dir_obj = tempfile.TemporaryDirectory()
        self.temp_dir = Path(self.temp_dir_obj.name)
        (self.temp_dir / "ab_results").mkdir(parents=True, exist_ok=True)
        (self.temp_dir / "harness").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir_obj.cleanup()

    @patch("ab_test.ensure_dashboard_running")
    @patch("tests.test_strategy.run_variant_unit_tests", return_value=(True, "All passed"))
    @patch("harness.depth_test.run_depth_test", return_value=(True, {"overall": {"passed": True}}))
    @patch("ab_test.print_matrix_summary")
    @patch("ab_test.append_results_ledger")
    def test_parity_matrix_parity_fails_loudly_if_pool_cannot_run(
        self, mock_ledger, mock_summary, mock_depth, mock_unit, mock_dash_fn
    ):
        """A PARITY target whose matrix returns PARITY must fail loudly without pool config, writing no completed history."""
        mock_dash = MagicMock()
        mock_dash_fn.return_value = mock_dash

        parity_result = {
            "mode": "standard",
            "name": "Standard 4P",
            "num_snakes": 4,
            "target_share": 25.0,
            "total_played": 200,
            "var_win_share": 25.0,
            "var_avg_depth": 5.0,
            "base_avg_depth": 5.0,
            "z_score": 0.0,
            "se_pct": 3.0,
            "results": {
                STRATEGY_BASELINE: {"wins": 150, "losses": 50, "draws": 0},
                STRATEGY_VARIANT: {"wins": 50, "losses": 150, "draws": 0},
            },
            "snakes_stats": {},
            "stop_type": "MAXIMUM_REACHED",
            "stop_reason": "Parity confirmed",
            "passed": True,
            "decision": "PARITY",
            "duration_sec": 1.0,
            "csv_path": "fake.csv",
        }

        # Point PROJECT_ROOT to empty temp dir with NO pool_snakes.json
        with patch("ab_test.PROJECT_ROOT", self.temp_dir):
            with patch("ab_test._run_single_mode_simulation", return_value=parity_result):
                with self.assertRaises(RuntimeError) as ctx:
                    run_ab_test_engine(
                        desc="Refactor test",
                        modes="standard",
                        target="PARITY",
                        skip_smoke=True,
                    )
                self.assertIn("External pool gate required", str(ctx.exception))

        # Check history file: must NOT contain a MATRIX_COMPLETE record
        history_file = self.temp_dir / "ab_results" / "ab_history.json"
        if history_file.exists():
            records = json.loads(history_file.read_text())
            for r in records:
                self.assertNotEqual(r.get("stop_type"), "MATRIX_COMPLETE")

    @patch("ab_test.ensure_dashboard_running")
    @patch("tests.test_strategy.run_variant_unit_tests", return_value=(True, "All passed"))
    @patch("harness.depth_test.run_depth_test", return_value=(True, {"overall": {"passed": True}}))
    @patch("ab_test.print_matrix_summary")
    @patch("ab_test.append_results_ledger")
    @patch("ab_test.run_pool_stage")
    def test_parity_matrix_parity_proceeds_to_pool_and_records_pool_data(
        self, mock_pool_stage, mock_ledger, mock_summary, mock_depth, mock_unit, mock_dash_fn
    ):
        """A PARITY target whose matrix returns PARITY proceeds to pool, producing final history only with pool data."""
        mock_dash = MagicMock()
        mock_dash_fn.return_value = mock_dash

        parity_result = {
            "mode": "standard",
            "name": "Standard 4P",
            "num_snakes": 4,
            "target_share": 25.0,
            "total_played": 200,
            "var_win_share": 25.0,
            "var_avg_depth": 5.0,
            "base_avg_depth": 5.0,
            "z_score": 0.0,
            "se_pct": 3.0,
            "results": {
                STRATEGY_BASELINE: {"wins": 150, "losses": 50, "draws": 0},
                STRATEGY_VARIANT: {"wins": 50, "losses": 150, "draws": 0},
            },
            "snakes_stats": {},
            "stop_type": "MAXIMUM_REACHED",
            "stop_reason": "Parity confirmed",
            "passed": True,
            "decision": "PARITY",
            "duration_sec": 1.0,
            "csv_path": "fake.csv",
        }

        mock_pool_stage.return_value = {
            "success": True,
            "decision": "PARITY",
            "stop_type": "POOL_COMPLETE",
            "stop_reason": "Passed pool gate and replication",
            "pool_data": [{"mode": "standard", "games": 50, "delta": 0.0, "z_score": 0.0, "decision": "PARITY"}],
            "all_games": [{"game_num": 1}],
            "opponents": [],
            "replicated": True,
        }

        with patch("ab_test.PROJECT_ROOT", self.temp_dir):
            with patch("ab_test._run_single_mode_simulation", return_value=parity_result):
                run_ab_test_engine(
                    desc="Refactor test",
                    modes="standard",
                    target="PARITY",
                    skip_smoke=True,
                )

        mock_pool_stage.assert_called_once()

        history_file = self.temp_dir / "ab_results" / "ab_history.json"
        self.assertTrue(history_file.exists())
        records = json.loads(history_file.read_text())
        self.assertEqual(len(records), 1)
        self.assertIsNotNone(records[0].get("pool_data"))
        self.assertEqual(records[0]["stop_type"], "POOL_COMPLETE")
        self.assertEqual(records[0]["recommendation"], "PARITY")

    @patch("ab_test.ensure_dashboard_running")
    @patch("tests.test_strategy.run_variant_unit_tests", return_value=(True, "All passed"))
    @patch("harness.depth_test.run_depth_test", return_value=(True, {"overall": {"passed": True}}))
    @patch("ab_test.print_matrix_summary")
    @patch("ab_test.append_results_ledger")
    @patch("ab_test.run_pool_stage")
    def test_matrix_revert_rejects_without_pool(
        self, mock_pool_stage, mock_ledger, mock_summary, mock_depth, mock_unit, mock_dash_fn
    ):
        """A matrix REVERT rejects immediately without calling pool."""
        mock_dash = MagicMock()
        mock_dash_fn.return_value = mock_dash

        revert_result = {
            "mode": "standard",
            "name": "Standard 4P",
            "num_snakes": 4,
            "target_share": 25.0,
            "total_played": 200,
            "var_win_share": 18.0,
            "var_avg_depth": 5.0,
            "base_avg_depth": 5.0,
            "z_score": -2.3,
            "se_pct": 3.0,
            "results": {
                STRATEGY_BASELINE: {"wins": 164, "losses": 36, "draws": 0},
                STRATEGY_VARIANT: {"wins": 36, "losses": 164, "draws": 0},
            },
            "snakes_stats": {},
            "stop_type": "EARLY_STOP_LOW",
            "stop_reason": "Regression detected",
            "passed": False,
            "decision": "REVERT",
            "duration_sec": 1.0,
            "csv_path": "fake.csv",
        }

        with patch("ab_test.PROJECT_ROOT", self.temp_dir):
            with patch("ab_test._run_single_mode_simulation", return_value=revert_result):
                run_ab_test_engine(
                    desc="Regressed candidate",
                    modes="standard",
                    target="PARITY",
                    skip_smoke=True,
                )

        mock_pool_stage.assert_not_called()

        history_file = self.temp_dir / "ab_results" / "ab_history.json"
        self.assertTrue(history_file.exists())
        records = json.loads(history_file.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["recommendation"], "REVERT")
        self.assertEqual(records[0]["stop_type"], "MATRIX_FAILED")

    @patch("ab_test.ensure_dashboard_running")
    @patch("tests.test_strategy.run_variant_unit_tests", return_value=(True, "All passed"))
    @patch("harness.depth_test.run_depth_test", return_value=(True, {"overall": {"passed": True}}))
    @patch("ab_test.print_matrix_summary")
    @patch("ab_test.append_results_ledger")
    @patch("ab_test.run_pool_stage")
    def test_rule3_bug_fix_completes_without_pool(
        self, mock_pool_stage, mock_ledger, mock_summary, mock_depth, mock_unit, mock_dash_fn
    ):
        """A Rule 3 BUG FIX target completes at MATRIX_COMPLETE without calling pool."""
        mock_dash = MagicMock()
        mock_dash_fn.return_value = mock_dash

        parity_result = {
            "mode": "standard",
            "name": "Standard 4P",
            "num_snakes": 4,
            "target_share": 25.0,
            "total_played": 200,
            "var_win_share": 25.0,
            "var_avg_depth": 5.0,
            "base_avg_depth": 5.0,
            "z_score": 0.0,
            "se_pct": 3.0,
            "results": {
                STRATEGY_BASELINE: {"wins": 150, "losses": 50, "draws": 0},
                STRATEGY_VARIANT: {"wins": 50, "losses": 150, "draws": 0},
            },
            "snakes_stats": {},
            "stop_type": "MAXIMUM_REACHED",
            "stop_reason": "Parity confirmed",
            "passed": True,
            "decision": "PARITY",
            "duration_sec": 1.0,
            "csv_path": "fake.csv",
        }

        with patch("ab_test.PROJECT_ROOT", self.temp_dir):
            with patch("ab_test._run_single_mode_simulation", return_value=parity_result):
                run_ab_test_engine(
                    desc="fix: avoid illegal corner trap",
                    modes="standard",
                    target="BUG FIX",
                    skip_smoke=True,
                )

        mock_pool_stage.assert_not_called()

        history_file = self.temp_dir / "ab_results" / "ab_history.json"
        self.assertTrue(history_file.exists())
        records = json.loads(history_file.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["stop_type"], "MATRIX_COMPLETE")
        self.assertEqual(records[0]["recommendation"], "PARITY")


if __name__ == "__main__":
    unittest.main()
