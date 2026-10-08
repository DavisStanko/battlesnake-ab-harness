import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ab_test import (
    load_checkpoint,
    save_checkpoint,
    clear_checkpoint,
    get_checkpoint_path,
    parse_args,
    _run_single_mode_simulation,
    run_ab_test_engine,
    CHECKPOINT_FILE_NAME,
    STRATEGY_BASELINE,
    STRATEGY_VARIANT,
)


class TestAbCheckpointResume(unittest.TestCase):
    """
    Unit tests for A/B testing harness checkpoint persistence and resumption logic.
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="ab_checkpoint_test_"))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_checkpoint_io_roundtrip(self):
        """Verify save_checkpoint, load_checkpoint, and clear_checkpoint."""
        test_data = {
            "desc": "Test checkpoint persistence",
            "modes": "constrictor,royale",
            "completed_modes": [
                {"mode": "constrictor", "total_played": 200, "decision": "PARITY"}
            ],
            "current_mode_state": None,
        }

        # Initially no checkpoint
        self.assertIsNone(load_checkpoint(self.temp_dir))

        # Save checkpoint
        save_checkpoint(self.temp_dir, test_data)
        loaded = load_checkpoint(self.temp_dir)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["desc"], test_data["desc"])
        self.assertEqual(len(loaded["completed_modes"]), 1)
        self.assertEqual(loaded["completed_modes"][0]["mode"], "constrictor")

        # Clear checkpoint
        clear_checkpoint(self.temp_dir)
        self.assertIsNone(load_checkpoint(self.temp_dir))

    def test_checkpoint_load_corrupted_json(self):
        """Corrupted checkpoint files should gracefully return None without raising."""
        cp_path = get_checkpoint_path(self.temp_dir)
        cp_path.write_text("invalid { json garbage", encoding="utf-8")
        self.assertIsNone(load_checkpoint(self.temp_dir))

    def test_cli_parse_resume_flag(self):
        """Verify --resume CLI flag parsing."""
        with patch("sys.argv", ["ab_test.py", "--desc", "Test experiment"]):
            args = parse_args()
            self.assertFalse(args.resume)

        with patch("sys.argv", ["ab_test.py", "--desc", "Test experiment", "--resume"]):
            args = parse_args()
            self.assertTrue(args.resume)

    def test_intra_mode_resume_remaining_tasks(self):
        """
        Verify that _run_single_mode_simulation filters out already completed
        game indices when given resume_mode_state.
        """
        # Simulate prior mode state where 3 games finished
        prior_state = {
            "mode": "constrictor",
            "total_played": 3,
            "results": {
                STRATEGY_BASELINE: {"wins": 2, "losses": 1, "draws": 0},
                STRATEGY_VARIANT: {"wins": 1, "losses": 2, "draws": 0},
            },
            "snakes_stats": {
                STRATEGY_BASELINE: {"wins": 2, "losses": 1, "draws": 0},
                STRATEGY_VARIANT: {"wins": 1, "losses": 2, "draws": 0},
            },
            "rows": [
                {"game": 1, "seed": 42001, "winner": STRATEGY_BASELINE, "turns": 20, "duration_ms": 100},
                {"game": 2, "seed": 42002, "winner": STRATEGY_VARIANT, "turns": 25, "duration_ms": 110},
                {"game": 4, "seed": 42004, "winner": STRATEGY_BASELINE, "turns": 18, "duration_ms": 90},
            ],
            "total_var_depth": 15.0,
            "total_base_depth": 15.0,
        }

        mock_dash = MagicMock()
        mock_dash.check_skip.return_value = True  # Immediately stop after init to inspect task generation

        with patch("ab_test.mp.Pool") as mock_pool_cls:
            mock_pool = MagicMock()
            mock_pool.__enter__.return_value = mock_pool
            mock_pool_cls.return_value = mock_pool

            # Intercept pool.imap_unordered to inspect task_args
            captured_tasks = []
            fake_iter = MagicMock()
            fake_iter.next.side_effect = TimeoutError()
            def fake_imap(func, args):
                captured_tasks.extend(args)
                return fake_iter
            mock_pool.imap_unordered.side_effect = fake_imap

            _run_single_mode_simulation(
                mode="constrictor",
                mode_name="Constrictor 4P",
                num_snakes=4,
                target_share=25.0,
                high_stop=40.0,
                low_stop=10.0,
                min_games=10,
                max_games=5,
                check_interval=5,
                threads=1,
                dash_client=mock_dash,
                desc="Test Intra-Mode Resume",
                is_bug_fix=False,
                keep_threshold=25.5,
                results_dir=self.temp_dir,
                resume_mode_state=prior_state,
            )

            # Games 1, 2, 4 were already done. Remaining out of 5 should be 3 and 5.
            remaining_game_nums = [t[0] for t in captured_tasks]
            self.assertEqual(remaining_game_nums, [3, 5])

    @patch("ab_test.ensure_dashboard_running")
    @patch("ab_test.run_binary_smoke_test")
    @patch("ab_test._run_single_mode_simulation")
    @patch("ab_test.print_matrix_summary")
    def test_matrix_resume_skips_completed_mode(
        self, mock_summary, mock_sim, mock_smoke, mock_dash_fn
    ):
        """
        Verify that run_ab_test_engine with --resume skips simulation for modes
        already marked completed in the checkpoint, aggregates their results, and
        clears the checkpoint upon suite completion.
        """
        mock_dash = MagicMock()
        mock_dash_fn.return_value = mock_dash
        mock_smoke.return_value = (True, "")

        constrictor_res = {
            "mode": "constrictor",
            "name": "Constrictor 4P",
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
            "duration_sec": 10.0,
            "csv_path": "fake.csv",
        }

        royale_res = {
            "mode": "royale",
            "name": "Royale 4P",
            "num_snakes": 4,
            "target_share": 25.0,
            "total_played": 200,
            "var_win_share": 26.0,
            "var_avg_depth": 5.0,
            "base_avg_depth": 5.0,
            "z_score": 0.5,
            "se_pct": 3.0,
            "results": {
                STRATEGY_BASELINE: {"wins": 148, "losses": 52, "draws": 0},
                STRATEGY_VARIANT: {"wins": 52, "losses": 148, "draws": 0},
            },
            "snakes_stats": {},
            "stop_type": "MAXIMUM_REACHED",
            "stop_reason": "Parity confirmed",
            "passed": True,
            "decision": "PARITY",
            "duration_sec": 10.0,
            "csv_path": "fake2.csv",
        }
        mock_sim.return_value = royale_res

        results_dir = self.temp_dir / "ab_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        save_checkpoint(results_dir, {
            "desc": "Test Skip",
            "modes": "constrictor,royale",
            "completed_modes": [constrictor_res],
            "current_mode_state": None,
        })

        with patch("ab_test.PROJECT_ROOT", self.temp_dir):
            run_ab_test_engine(
                desc="Test Skip",
                modes="constrictor,royale",
                skip_smoke=True,
                resume=True,
            )

        # Simulation must only be called for royale, skipping constrictor
        self.assertEqual(mock_sim.call_count, 1)
        call_kwargs = mock_sim.call_args[1]
        self.assertEqual(call_kwargs["mode"], "royale")

        # Active checkpoint file must be cleared after completion
        self.assertIsNone(load_checkpoint(results_dir))


if __name__ == "__main__":
    unittest.main()
