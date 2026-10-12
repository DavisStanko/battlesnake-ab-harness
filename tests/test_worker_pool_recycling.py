#!/usr/bin/env python3
"""
test_worker_pool_recycling.py — Unit tests for persistent worker pool recycling in harness/ab_test.py.
"""

import multiprocessing as mp
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root and harness directory are on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import harness.ab_test as ab_test
from harness.ab_test import (
    STRATEGY_BASELINE,
    STRATEGY_VARIANT,
    _run_single_mode_simulation,
    create_worker_pool,
)
from harness.ab_dashboard import DashboardClient


class TestWorkerPoolRecycling(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.results_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_create_worker_pool(self):
        pool = create_worker_pool(threads=2)
        try:
            self.assertIsInstance(pool, mp.pool.Pool)
            self.assertEqual(getattr(pool, "_state", None), mp.pool.RUN)
        finally:
            pool.close()
            pool.join()

    def test_run_single_mode_simulation_with_passed_pool_preserves_pool(self):
        dash_mock = MagicMock(spec=DashboardClient)
        dash_mock.check_skip.return_value = False

        shared_pool = create_worker_pool(threads=2)
        try:
            # Mock simulate_single_game to return instantly
            def mock_game(game_num, seed, mode="duel"):
                return {
                    "game": game_num,
                    "seed": seed,
                    "winner": STRATEGY_VARIANT,
                    "tied_snakes": [],
                    "turns": 20,
                    "duration_ms": 10.0,
                    "base_avg_depth": 5.0,
                    "var_avg_depth": 5.0,
                }

            with patch("harness.ab_test.simulate_single_game", side_effect=mock_game):
                res1 = _run_single_mode_simulation(
                    mode="duel",
                    mode_name="Duel 1v1",
                    num_snakes=2,
                    target_share=50.0,
                    high_stop=60.0,
                    low_stop=40.0,
                    min_games=5,
                    max_games=5,
                    check_interval=1,
                    dash_client=dash_mock,
                    desc="Test Pool Reuse",
                    is_bug_fix=False,
                    keep_threshold=51.5,
                    threads=2,
                    results_dir=self.results_dir,
                    pool=shared_pool,
                )

                # Verify shared_pool is STILL open and running!
                self.assertEqual(getattr(shared_pool, "_state", None), mp.pool.RUN)
                self.assertEqual(res1["total_played"], 5)

                # Now run a second mode using the EXACT SAME shared_pool!
                res2 = _run_single_mode_simulation(
                    mode="standard",
                    mode_name="Standard 4P",
                    num_snakes=4,
                    target_share=25.0,
                    high_stop=35.0,
                    low_stop=18.0,
                    min_games=5,
                    max_games=5,
                    check_interval=1,
                    dash_client=dash_mock,
                    desc="Test Pool Reuse 2",
                    is_bug_fix=False,
                    keep_threshold=25.5,
                    threads=2,
                    results_dir=self.results_dir,
                    pool=shared_pool,
                )

                self.assertEqual(getattr(shared_pool, "_state", None), mp.pool.RUN)
                self.assertEqual(res2["total_played"], 5)
        finally:
            shared_pool.close()
            shared_pool.join()

    def test_run_single_mode_simulation_without_pool_creates_and_cleans_local_pool(self):
        dash_mock = MagicMock(spec=DashboardClient)
        dash_mock.check_skip.return_value = False

        def mock_game(game_num, seed, mode="duel"):
            return {
                "game": game_num,
                "seed": seed,
                "winner": STRATEGY_BASELINE,
                "tied_snakes": [],
                "turns": 15,
                "duration_ms": 10.0,
                "base_avg_depth": 5.0,
                "var_avg_depth": 5.0,
            }

        with patch("harness.ab_test.simulate_single_game", side_effect=mock_game):
            # Calling with pool=None creates a local pool and cleans it up
            res = _run_single_mode_simulation(
                mode="duel",
                mode_name="Duel 1v1",
                num_snakes=2,
                target_share=50.0,
                high_stop=60.0,
                low_stop=40.0,
                min_games=3,
                max_games=3,
                check_interval=1,
                dash_client=dash_mock,
                desc="Test Local Pool",
                is_bug_fix=False,
                keep_threshold=51.5,
                threads=2,
                results_dir=self.results_dir,
                pool=None,
            )
            self.assertEqual(res["total_played"], 3)


if __name__ == "__main__":
    unittest.main()
