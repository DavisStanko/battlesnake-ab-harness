import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from depth_test import is_parity_target, run_depth_test


class TestDepthGate(unittest.TestCase):
    """
    Unit tests for Stage 2 fixed-position depth & throughput benchmark gating.
    Verifies that parity modes enforce automated failure on throughput/depth regression,
    while improvement modes allow matrix self-play to judge positional accuracy.
    """

    def test_is_parity_target(self):
        # Explicit parity / non-regression targets
        self.assertTrue(is_parity_target("PARITY"))
        self.assertTrue(is_parity_target("BUG FIX"))
        self.assertTrue(is_parity_target("HARNESS VERIFICATION"))
        self.assertTrue(is_parity_target("refactor c_engine"))
        self.assertTrue(is_parity_target("fix tail chase blunder"))

        # Improvement targets
        self.assertFalse(is_parity_target("IMPROVEMENT"))
        self.assertFalse(is_parity_target("Heuristic Weight Tuning"))
        self.assertFalse(is_parity_target(None))
        self.assertFalse(is_parity_target(""))

    def test_ab_test_target_choices(self):
        """ab_test.py accepts valid target choices and rejects invalid values."""
        from io import StringIO
        import harness.ab_test as ab_test

        # Valid targets: PARITY and IMPROVEMENT
        with patch.object(sys, "argv", ["ab_test.py", "--desc", "test", "--target", "PARITY"]):
            args = ab_test.parse_args()
            self.assertEqual(args.target, "PARITY")

        with patch.object(sys, "argv", ["ab_test.py", "--desc", "test", "--target", "IMPROVEMENT"]):
            args = ab_test.parse_args()
            self.assertEqual(args.target, "IMPROVEMENT")

        # Invalid target raises SystemExit code 2
        with patch("sys.stderr", new_callable=StringIO):
            with patch.object(sys, "argv", ["ab_test.py", "--desc", "test", "--target", "INVALID_TARGET"]):
                with self.assertRaises(SystemExit) as cm:
                    ab_test.parse_args()
                self.assertEqual(cm.exception.code, 2)

    def test_parity_mode_fails_on_overall_nps_drop(self):
        """
        In parity mode, overall NPS ratio < 0.95x must fail Stage 2 automatically.
        """
        mock_res = {
            "mode": "standard",
            "turn": 20,
            "phase": "Early",
            "title": "Standard Early",
            "base_med_depth": 5.0,
            "var_med_depth": 5.0,
            "delta_depth": 0.0,
            "base_med_nps": 100000,
            "var_med_nps": 85000,
            "nps_ratio": 0.85,
            "agreement_pct": 100.0,
            "base_move": "up",
            "var_move": "up",
        }

        with patch("depth_test._evaluate_position_worker", return_value=mock_res):
            passed, results = run_depth_test(
                modes=["standard"],
                turns=[20],
                repeats=1,
                workers=1,
                enable_dashboard=False,
                target="PARITY",
            )
            self.assertFalse(passed, "Stage 2 must fail in PARITY mode when NPS drops to 0.85x")
            flagged = results.get("flagged_regressions", [])
            self.assertTrue(any("NPS ratio 0.85x < 0.90x" in f or "throughput" in f for f in flagged))

    def test_parity_mode_fails_on_depth_drop(self):
        """
        In parity mode, median depth drop must fail Stage 2 automatically.
        """
        mock_res = {
            "mode": "duel",
            "turn": 20,
            "phase": "Early",
            "title": "Duel Early",
            "base_med_depth": 6.0,
            "var_med_depth": 5.0,
            "delta_depth": -1.0,
            "base_med_nps": 100000,
            "var_med_nps": 100000,
            "nps_ratio": 1.00,
            "agreement_pct": 100.0,
            "base_move": "up",
            "var_move": "up",
        }

        with patch("depth_test._evaluate_position_worker", return_value=mock_res):
            passed, results = run_depth_test(
                modes=["duel"],
                turns=[20],
                repeats=1,
                workers=1,
                enable_dashboard=False,
                target="PARITY",
            )
            self.assertFalse(passed, "Stage 2 must fail in PARITY mode when depth drops")
            flagged = results.get("flagged_regressions", [])
            self.assertTrue(any("depth 5.0 < 6.0" in f for f in flagged))

    def test_improvement_mode_does_not_fail_on_nps_drop(self):
        """
        In improvement mode, throughput drop (e.g. 0.90x) does NOT cause Stage 2 failure,
        allowing Stage 3 self-play matrix to judge if positional accuracy wins games.
        """
        mock_res = {
            "mode": "standard",
            "turn": 20,
            "phase": "Early",
            "title": "Standard Early",
            "base_med_depth": 5.0,
            "var_med_depth": 5.0,
            "delta_depth": 0.0,
            "base_med_nps": 100000,
            "var_med_nps": 88000,
            "nps_ratio": 0.88,
            "agreement_pct": 100.0,
            "base_move": "up",
            "var_move": "up",
        }

        with patch("depth_test._evaluate_position_worker", return_value=mock_res):
            passed, results = run_depth_test(
                modes=["standard"],
                turns=[20],
                repeats=1,
                workers=1,
                enable_dashboard=False,
                target="IMPROVEMENT",
            )
            self.assertTrue(passed, "Stage 2 must pass in IMPROVEMENT mode despite 0.88x NPS")
            flagged = results.get("flagged_regressions", [])
            self.assertEqual(len(flagged), 0)

    @patch("tests.test_strategy.run_variant_unit_tests", return_value=(True, "All passed"))
    @patch("ab_test.ensure_dashboard_running")
    @patch("ab_test.PROJECT_ROOT")
    @patch("ab_test._run_single_mode_simulation")
    @patch("harness.depth_test.run_depth_test")
    @patch("ab_test.save_run_history")
    def test_ab_engine_halts_on_depth_failure(
        self, mock_save_history, mock_run_depth, mock_sim_mode, mock_root, mock_dash, mock_unit_tests
    ):
        """
        Verifies that when Stage 2 depth/throughput fails, ab_test aborts with REVERT
        and does NOT simulate any matrix games.
        """
        from ab_test import run_ab_test_engine
        import tempfile
        import shutil

        temp_dir = Path(tempfile.mkdtemp(prefix="ab_test_gate_"))
        mock_root.__truediv__.side_effect = lambda path: temp_dir / path

        try:
            mock_run_depth.return_value = (
                False,
                {
                    "overall": {"passed": False},
                    "flagged_regressions": ["standard (NPS ratio 0.85x < 0.90x)"],
                },
            )
            mock_save_history.return_value = temp_dir / "history.json"

            dash_client_mock = MagicMock()

            run_ab_test_engine(
                desc="Refactor [TARGET: PARITY]",
                modes="standard",
                skip_smoke=True,
                target="PARITY",
            )

            # Ensure depth_test was called with target="PARITY"
            mock_run_depth.assert_called_once()
            _, kwargs = mock_run_depth.call_args
            self.assertEqual(kwargs.get("target"), "PARITY")

            # Matrix simulation MUST NOT be called because depth failed
            mock_sim_mode.assert_not_called()

            # save_run_history MUST record REVERT
            mock_save_history.assert_called_once()
            save_args, save_kwargs = mock_save_history.call_args
            self.assertEqual(save_args[4], "REVERT")  # stop_type
            self.assertEqual(save_args[6], "REVERT")  # recommendation
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
