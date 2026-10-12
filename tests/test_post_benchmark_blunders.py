#!/usr/bin/env python3
"""
test_post_benchmark_blunders.py — Unit tests for automated post-benchmark blunder extraction pipeline.
"""

import copy
import csv
import json
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
    _load_mode_rows,
    extract_post_benchmark_blunders,
    print_blunder_summary,
    replay_game_for_blunders,
    run_post_benchmark_blunder_pipeline,
)
from harness.blunder_extractor import BlunderDetection


class TestPostBenchmarkBlunders(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.results_dir = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_mode_rows_from_in_memory_rows(self):
        mode_res = {
            "mode": "duel",
            "rows": [
                {"game": 1, "seed": 42001, "winner": STRATEGY_VARIANT, "turns": 45},
                {"game": 2, "seed": 42002, "winner": STRATEGY_BASELINE, "turns": 12},
            ],
        }
        loaded = _load_mode_rows(mode_res)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["game"], 1)

    def test_load_mode_rows_from_csv(self):
        csv_file = self.results_dir / "test_duel.csv"
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["game", "seed", "winner", "turns", "duration_ms"])
            writer.writeheader()
            writer.writerow({"game": 1, "seed": 42001, "winner": STRATEGY_BASELINE, "turns": 15, "duration_ms": 100})
            writer.writerow({"game": 2, "seed": 42002, "winner": STRATEGY_VARIANT, "turns": 80, "duration_ms": 200})

        mode_res = {
            "mode": "duel",
            "csv_path": str(csv_file),
        }
        loaded = _load_mode_rows(mode_res)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(int(loaded[0]["game"]), 1)
        self.assertEqual(loaded[0]["winner"], STRATEGY_BASELINE)

    def test_load_mode_rows_empty_fallback(self):
        mode_res = {"mode": "duel", "csv_path": "/nonexistent/path.csv"}
        loaded = _load_mode_rows(mode_res)
        self.assertEqual(loaded, [])

    def test_extract_post_benchmark_blunders_zero_losses(self):
        matrix_results = [
            {
                "mode": "duel",
                "name": "Duel 1v1",
                "total_played": 10,
                "rows": [
                    {"game": i, "seed": 42000 + i, "winner": STRATEGY_VARIANT, "turns": 50}
                    for i in range(1, 11)
                ],
            }
        ]
        total_analyzed, blunders, test_file = extract_post_benchmark_blunders(
            matrix_results,
            max_losses=5,
            results_dir=self.results_dir,
        )
        self.assertEqual(total_analyzed, 0)
        self.assertEqual(blunders, [])
        self.assertIsNone(test_file)

    def test_extract_post_benchmark_blunders_picks_shortest_losses(self):
        # Create matrix results with losses of varying lengths
        # Game 1: 15 turns (loss)
        # Game 2: 120 turns (loss)
        # Game 3: 8 turns (loss)
        # Game 4: 90 turns (loss)
        # Game 5: 25 turns (loss)
        # Game 6: 200 turns (loss)
        matrix_results = [
            {
                "mode": "standard",
                "name": "Standard 4P",
                "total_played": 6,
                "rows": [
                    {"game": 1, "seed": 101, "winner": STRATEGY_BASELINE, "turns": 15},
                    {"game": 2, "seed": 102, "winner": STRATEGY_BASELINE, "turns": 120},
                    {"game": 3, "seed": 103, "winner": STRATEGY_BASELINE, "turns": 8},
                    {"game": 4, "seed": 104, "winner": STRATEGY_BASELINE, "turns": 90},
                    {"game": 5, "seed": 105, "winner": STRATEGY_BASELINE, "turns": 25},
                    {"game": 6, "seed": 106, "winner": STRATEGY_BASELINE, "turns": 200},
                ],
            }
        ]

        replayed_seeds = []

        def mock_replay(game_num, seed, mode, max_turns=1000, target_snake=STRATEGY_VARIANT):
            replayed_seeds.append(seed)
            # Simulate a blunder for the shortest game (seed 103)
            if seed == 103:
                mock_b = BlunderDetection(
                    turn=7,
                    snake_id=target_snake,
                    blunder_move="left",
                    blunder_cause="wall-collision",
                    safe_moves=["up", "down"],
                    state_before={
                        "player": {"body": [(0, 1), (1, 1), (2, 1)], "health": 100},
                        "opponents": [],
                        "food": [],
                        "hazards": [],
                    },
                    width=11,
                    height=11,
                    wrap=False,
                    constrictor=False,
                    game_id=f"game_{game_num}_{seed}",
                )
                return [], [mock_b]
            return [], []

        with patch("harness.ab_test.replay_game_for_blunders", side_effect=mock_replay):
            total_analyzed, blunders, test_file = extract_post_benchmark_blunders(
                matrix_results,
                max_losses=3,
                results_dir=self.results_dir,
            )

        self.assertEqual(total_analyzed, 3)
        # Shortest 3 games: seed 103 (8 turns), seed 101 (15 turns), seed 105 (25 turns)
        self.assertEqual(replayed_seeds, [103, 101, 105])
        self.assertEqual(len(blunders), 1)
        self.assertEqual(blunders[0]["seed"], 103)
        self.assertEqual(blunders[0]["blunder_turn"], 7)
        self.assertEqual(blunders[0]["blunder_cause"], "wall-collision")
        self.assertIsNotNone(test_file)
        self.assertTrue(test_file.exists())

        # Check generated test file content
        content = test_file.read_text(encoding="utf-8")
        self.assertIn("class TestExtractedBlunders", content)
        self.assertIn("test_blunder_standard_g3_t7_wall_collision", content)
        self.assertIn("wall-collision", content)

    def test_print_blunder_summary_formatting(self):
        # Test 0 losses
        with patch("builtins.print") as mock_print:
            print_blunder_summary(0, [])
            mock_print.assert_any_call("  [✓] Zero variant losses recorded across tested modes; no blunders to analyze.")

        # Test 0 blunders detected
        with patch("builtins.print") as mock_print:
            print_blunder_summary(5, [])
            mock_print.assert_any_call("  [✓] No avoidable 1-step fatal blunders detected.")

        # Test with detected blunders
        fake_blunders = [
            {
                "game": 3,
                "seed": 42003,
                "mode_name": "Duel 1v1",
                "loss_turns": 14,
                "blunder_turn": 13,
                "blunder_move": "left",
                "blunder_cause": "snake-collision",
                "safe_moves": ["up", "right"],
            }
        ]
        with patch("builtins.print") as mock_print:
            print_blunder_summary(1, fake_blunders, output_test_file=self.results_dir / "blunder_tests.py")
            # Verify blunder info was printed
            mock_print.assert_any_call("  [⚠️] Detected 1 avoidable fatal blunder(s):")

    def test_run_post_benchmark_blunder_pipeline_end_to_end(self):
        matrix_results = [
            {
                "mode": "duel",
                "name": "Duel 1v1",
                "total_played": 2,
                "rows": [
                    {"game": 1, "seed": 42001, "winner": STRATEGY_BASELINE, "turns": 20},
                    {"game": 2, "seed": 42002, "winner": STRATEGY_VARIANT, "turns": 60},
                ],
            }
        ]

        def mock_replay(game_num, seed, mode, max_turns=1000, target_snake=STRATEGY_VARIANT):
            return [], []

        with patch("harness.ab_test.replay_game_for_blunders", side_effect=mock_replay):
            # Should run without error
            run_post_benchmark_blunder_pipeline(
                matrix_results=matrix_results,
                max_losses=5,
                results_dir=self.results_dir,
            )

    def test_replay_game_for_blunders_real_simulation(self):
        frames, blunders = replay_game_for_blunders(
            game_num=1,
            seed=42001,
            mode="duel",
            max_turns=5,
            target_snake=STRATEGY_VARIANT,
        )
        self.assertGreater(len(frames), 0)
        self.assertIsInstance(blunders, list)


if __name__ == "__main__":
    unittest.main()
