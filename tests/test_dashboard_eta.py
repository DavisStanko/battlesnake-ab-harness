"""
test_dashboard_eta.py — Unit Tests for Multi-Mode Persistent ETA Calculation in ab_dashboard.py
"""

import json
from pathlib import Path
import tempfile
import time
import unittest

from harness.ab_dashboard import (
    append_mode_timing,
    load_mode_timings,
    get_mode_medians,
    calculate_eta,
    DashboardState,
)


class TestDashboardETA(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.timings_file = Path(self.temp_dir.name) / "test_mode_timings.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_synthetic_math_example_from_user_spec(self):
        """
        User Specification Check:
        history says standard = 10s/game, constrictor = 2.5s/game;
        a job with 100 standard and 200 constrictor games left must show ~1500s.
        """
        synthetic_timings = [
            {"mode": "standard", "run_type": "self", "games": 100, "elapsed_seconds": 1000.0},     # 10.0 s/game
            {"mode": "constrictor", "run_type": "self", "games": 200, "elapsed_seconds": 500.0},   # 2.5 s/game
        ]

        matrix_progress = [
            {"mode": "standard", "status": "RUNNING", "games": 0, "max_games": 100},
            {"mode": "constrictor", "status": "PENDING", "games": 0, "max_games": 200},
        ]

        eta_res = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=0,
            current_max_games=100,
            current_mode_elapsed=0.0,
            run_type="self",
            mode_timings=synthetic_timings,
        )

        self.assertIsNotNone(eta_res["total_eta_seconds"])
        self.assertAlmostEqual(eta_res["total_eta_seconds"], 1500.0, places=1)
        self.assertEqual(len(eta_res["breakdown"]), 2)
        self.assertEqual(eta_res["breakdown"][0]["mode"], "standard")
        self.assertAlmostEqual(eta_res["breakdown"][0]["eta_seconds"], 1000.0, places=1)
        self.assertEqual(eta_res["breakdown"][1]["mode"], "constrictor")
        self.assertAlmostEqual(eta_res["breakdown"][1]["eta_seconds"], 500.0, places=1)

    def test_median_used_instead_of_mean(self):
        """Verify that the median of past runs is used, not the mean."""
        # 3 runs of duel: 6s, 10s, 30s. Mean = 15.33s, Median = 10.0s.
        timings = [
            {"mode": "duel", "run_type": "self", "games": 10, "elapsed_seconds": 60.0},   # 6.0 s/game
            {"mode": "duel", "run_type": "self", "games": 10, "elapsed_seconds": 100.0},  # 10.0 s/game
            {"mode": "duel", "run_type": "self", "games": 10, "elapsed_seconds": 300.0},  # 30.0 s/game
        ]

        medians = get_mode_medians(timings, target_run_type="self")
        self.assertAlmostEqual(medians["duel"], 10.0, places=2)

        # 50 games left of duel: 50 * 10 = 500s (if mean were used: 50 * 15.33 = 766.7s)
        eta_res = calculate_eta(
            matrix_progress=[{"mode": "duel", "status": "RUNNING", "games": 0, "max_games": 50}],
            current_mode="duel",
            current_played=0,
            current_max_games=50,
            current_mode_elapsed=0.0,
            run_type="self",
            mode_timings=timings,
        )
        self.assertAlmostEqual(eta_res["total_eta_seconds"], 500.0, places=1)

    def test_blending_stable_vs_unstable_current_mode(self):
        """
        Rule 3:
        If current mode has already run long enough (>= 20 games), blend using live rate.
        Before threshold (< 20 games), use history for everything.
        """
        timings = [
            {"mode": "standard", "run_type": "self", "games": 100, "elapsed_seconds": 1000.0},  # 10s/game history
            {"mode": "constrictor", "run_type": "self", "games": 100, "elapsed_seconds": 250.0}, # 2.5s/game history
        ]

        matrix_progress = [
            {"mode": "standard", "status": "RUNNING", "games": 10, "max_games": 100},
            {"mode": "constrictor", "status": "PENDING", "games": 0, "max_games": 200},
        ]

        # Case A: 10 games done (< 20). Live rate is 15s/game (150s / 10g), but unstable!
        # Should use history for standard (10s/game) for remaining 90 games -> 90 * 10 = 900s
        # Constrictor: 200 * 2.5 = 500s
        # Total = 900 + 500 = 1400s
        eta_unstable = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=10,
            current_max_games=100,
            current_mode_elapsed=150.0,
            run_type="self",
            mode_timings=timings,
            min_stable_games=20,
        )
        self.assertFalse(eta_unstable["is_stable"])
        self.assertEqual(eta_unstable["breakdown"][0]["rate_source"], "history")
        self.assertAlmostEqual(eta_unstable["total_eta_seconds"], 1400.0, places=1)

        # Case B: 25 games done (>= 20). Live rate is 15s/game (375s / 25g), stable!
        # Remaining standard: 75 games * 15s (live rate) = 1125s
        # Constrictor: 200 * 2.5 (history) = 500s
        # Total = 1125 + 500 = 1625s
        matrix_progress[0]["games"] = 25
        eta_stable = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=25,
            current_max_games=100,
            current_mode_elapsed=375.0,
            run_type="self",
            mode_timings=timings,
            min_stable_games=20,
        )
        self.assertTrue(eta_stable["is_stable"])
        self.assertEqual(eta_stable["breakdown"][0]["rate_source"], "live")
        self.assertAlmostEqual(eta_stable["total_eta_seconds"], 1625.0, places=1)

    def test_mode_with_no_history_falls_back_to_current_mode_rate(self):
        """Rule 4: A mode with no history falls back to the current mode's rate."""
        timings = [
            {"mode": "standard", "run_type": "self", "games": 100, "elapsed_seconds": 1000.0},  # 10s/game
        ]

        # royale has NO history. Current mode standard has live rate 12s/game (240s / 20g)
        matrix_progress = [
            {"mode": "standard", "status": "RUNNING", "games": 20, "max_games": 100},
            {"mode": "royale", "status": "PENDING", "games": 0, "max_games": 50},
        ]

        eta_res = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=20,
            current_max_games=100,
            current_mode_elapsed=240.0,
            run_type="self",
            mode_timings=timings,
        )

        # standard: 80 left * 12s = 960s
        # royale: 50 left * 12s (fallback to current mode rate) = 600s
        # Total = 1560s
        self.assertAlmostEqual(eta_res["total_eta_seconds"], 1560.0, places=1)
        self.assertEqual(eta_res["breakdown"][1]["rate_source"], "fallback")
        self.assertAlmostEqual(eta_res["breakdown"][1]["rate_spg"], 12.0, places=1)

    def test_run_with_no_history_at_all_matches_legacy_behavior(self):
        """Rule 4: A run with no history at all must behave exactly as the dashboard does now."""
        # No history at all
        matrix_progress = [
            {"mode": "standard", "status": "RUNNING", "games": 40, "max_games": 200},
            {"mode": "constrictor", "status": "PENDING", "games": 0, "max_games": 200},
        ]

        # 40 games in 400s -> pace = 0.1 games/sec -> 10s/game
        # Legacy behavior only counted current mode: (200 - 40) / 0.1 = 1600s
        eta_res = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=40,
            current_max_games=200,
            current_mode_elapsed=400.0,
            run_type="self",
            mode_timings=[],
        )

        self.assertFalse(eta_res["has_history"])
        self.assertAlmostEqual(eta_res["total_eta_seconds"], 1600.0, places=1)

    def test_append_mode_timing_persistence(self):
        """Rule 1: Verify persistent append to mode_timings.json."""
        self.assertFalse(self.timings_file.exists())
        append_mode_timing("royale", "self", 200, 2400.0, timings_path=self.timings_file)
        self.assertTrue(self.timings_file.exists())

        loaded = load_mode_timings(timings_path=self.timings_file)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["mode"], "royale")
        self.assertEqual(loaded[0]["run_type"], "self")
        self.assertEqual(loaded[0]["games"], 200)
        self.assertAlmostEqual(loaded[0]["elapsed_seconds"], 2400.0, places=1)

        # Deduplicate identical append
        append_mode_timing("royale", "self", 200, 2401.0, timings_path=self.timings_file)
        loaded2 = load_mode_timings(timings_path=self.timings_file)
        self.assertEqual(len(loaded2), 1)

        # Different mode appends
        append_mode_timing("constrictor", "pool", 50, 100.0, timings_path=self.timings_file)
        loaded3 = load_mode_timings(timings_path=self.timings_file)
        self.assertEqual(len(loaded3), 2)
        self.assertEqual(loaded3[1]["mode"], "constrictor")
        self.assertEqual(loaded3[1]["run_type"], "pool")

    def test_pool_mode_eta_calculation(self):
        """
        Verify that pool mode ETA correctly filters historical timings by run_type='pool'
        and uses the 10-game stability threshold for live blending.
        """
        timings = [
            {"mode": "standard", "run_type": "pool", "games": 20, "elapsed_seconds": 20.0},     # 1.0 s/game pool
            {"mode": "constrictor", "run_type": "pool", "games": 20, "elapsed_seconds": 10.0},  # 0.5 s/game pool
            {"mode": "standard", "run_type": "self", "games": 200, "elapsed_seconds": 2000.0},  # 10.0 s/game self
        ]

        matrix_progress = [
            {"mode": "standard", "status": "RUNNING", "games": 0, "max_games": 20},
            {"mode": "constrictor", "status": "PENDING", "games": 0, "max_games": 20},
        ]

        # Prior to 10 games, uses pool history (1.0s/game for standard, 0.5s for constrictor)
        eta_res = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=5,
            current_max_games=20,
            current_mode_elapsed=4.0,  # 0.8s live rate, but not stable yet (<10 games)
            run_type="pool",
            mode_timings=timings,
        )

        self.assertFalse(eta_res["is_stable"])
        # Standard: 15 left * 1.0s = 15.0s; Constrictor: 20 left * 0.5s = 10.0s -> Total = 25.0s
        self.assertAlmostEqual(eta_res["total_eta_seconds"], 25.0, places=1)

        # At >= 10 games, is_stable becomes True and standard blends with live rate (10.0s / 10 games = 1.0s)
        eta_stable = calculate_eta(
            matrix_progress=matrix_progress,
            current_mode="standard",
            current_played=10,
            current_max_games=20,
            current_mode_elapsed=8.0,  # 0.8s live rate
            run_type="pool",
            mode_timings=timings,
        )
        self.assertTrue(eta_stable["is_stable"])
        # Standard: 10 left * 0.8s = 8.0s; Constrictor: 20 left * 0.5s = 10.0s -> Total = 18.0s
        self.assertAlmostEqual(eta_stable["total_eta_seconds"], 18.0, places=1)

    def test_depth_test_eta_calculation(self):
        """
        Verify that depth test ETA computes accurately from remaining fixture positions,
        repetition budget, and live spp blending.
        """
        depth_data = {
            "total_positions": 12,
            "completed_positions": 0,
            "time_ms": 450,
            "repeats": 5,
        }

        # 0 completed positions: uses budget_spp = (5 repeats * 2 snakes * 450ms) / 1000 = 4.5s/position
        # 12 positions * 4.5s = 54.0s
        eta_budget = calculate_eta(
            matrix_progress=[],
            current_mode="depth",
            current_played=0,
            current_max_games=12,
            current_mode_elapsed=0.0,
            run_type="depth",
            depth_data=depth_data,
        )
        self.assertAlmostEqual(eta_budget["total_eta_seconds"], 54.0, places=1)
        self.assertEqual(eta_budget["breakdown"][0]["rate_source"], "budget")

        # After 4 positions completed in 16.0s (4.0s/position live):
        # 8 positions left * 4.0s = 32.0s
        depth_data["completed_positions"] = 4
        eta_live = calculate_eta(
            matrix_progress=[],
            current_mode="depth",
            current_played=4,
            current_max_games=12,
            current_mode_elapsed=16.0,
            run_type="depth",
            depth_data=depth_data,
        )
        self.assertAlmostEqual(eta_live["total_eta_seconds"], 32.0, places=1)
        self.assertEqual(eta_live["breakdown"][0]["rate_source"], "live")
        self.assertTrue(eta_live["is_stable"])

    def test_idle_state_no_timer_and_no_eta(self):
        """
        Verify that when dashboard starts without an active test runner,
        it enters IDLE state with 0s elapsed time (not ticking) and None for ETA.
        """
        state = DashboardState()
        self.assertEqual(state.status, "IDLE")
        d = state.to_dict()
        self.assertEqual(d["status"], "IDLE")
        self.assertFalse(d["runner_alive"])
        self.assertEqual(d["elapsed_seconds"], 0.0)
        self.assertEqual(d["mode_elapsed_seconds"], 0.0)
        self.assertIsNone(d["eta_seconds"])
        self.assertEqual(d["eta_breakdown"], [])


if __name__ == "__main__":
    unittest.main()
