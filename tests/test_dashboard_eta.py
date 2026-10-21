"""
test_dashboard_eta.py — Unit Tests for Multi-Mode Persistent ETA Calculation in ab_dashboard.py
"""

import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

from harness.ab_dashboard import (
    append_mode_timing,
    load_mode_timings,
    get_mode_medians,
    calculate_eta,
    DashboardState,
    start_dashboard_server,
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


class TestDashboardServerRendering(unittest.TestCase):
    def test_dashboard_server_renders_eta_on_ephemeral_port(self):
        """
        Starts a temporary dashboard server on an ephemeral port,
        initializes synthetic state, queries /api/state and HTML /,
        and verifies the 1500s ETA is accurately served and rendered.
        """
        temp_dir = tempfile.TemporaryDirectory()
        try:
            timings_file = Path(temp_dir.name) / "synthetic_timings.json"
            synthetic_timings = [
                {"mode": "standard", "run_type": "self", "games": 100, "elapsed_seconds": 1000.0},
                {"mode": "constrictor", "run_type": "self", "games": 200, "elapsed_seconds": 500.0},
            ]
            with open(timings_file, "w", encoding="utf-8") as f:
                json.dump(synthetic_timings, f)

            state = DashboardState(
                strategy_baseline="baseline",
                strategy_variant="variant",
                target_games=100,
                game_mode="standard",
            )
            state.matrix_progress = [
                {"mode": "standard", "status": "RUNNING", "games": 0, "max_games": 100},
                {"mode": "constrictor", "status": "PENDING", "games": 0, "max_games": 200},
            ]

            # Start server on ephemeral port (preferred_port=0 lets OS choose free port)
            server, port = start_dashboard_server(state, preferred_port=0)
            try:
                # Patch timings file for this server instance
                import harness.ab_dashboard as abd
                orig_timings_file = abd.MODE_TIMINGS_FILE
                abd.MODE_TIMINGS_FILE = timings_file

                try:
                    # 1. Test /api/state JSON
                    with urllib.request.urlopen(f"http://localhost:{port}/api/state", timeout=2.0) as resp:
                        self.assertEqual(resp.status, 200)
                        api_data = json.loads(resp.read().decode("utf-8"))

                    self.assertIn("eta_seconds", api_data)
                    self.assertAlmostEqual(api_data["eta_seconds"], 1500.0, places=1)
                    self.assertEqual(len(api_data["eta_breakdown"]), 2)

                    # 2. Test / HTML
                    with urllib.request.urlopen(f"http://localhost:{port}/", timeout=2.0) as resp:
                        self.assertEqual(resp.status, 200)
                        html_content = resp.read().decode("utf-8")

                    self.assertIn("runsSubtext", html_content)
                    self.assertIn("data.eta_seconds", html_content)
                    self.assertIn("Total Run ETA", html_content)
                finally:
                    abd.MODE_TIMINGS_FILE = orig_timings_file
            finally:
                server.shutdown()
                server.server_close()
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
