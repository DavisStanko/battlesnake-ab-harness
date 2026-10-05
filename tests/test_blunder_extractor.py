"""
test_blunder_extractor.py — Unit Tests for blunder_extractor.py
"""

import ast
import copy
import json
import os
import sys
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import blunder_extractor
from blunder_extractor import (
    BlunderDetection,
    extract_frame_board,
    find_blunders,
    generate_unit_test_code,
    get_move_between,
    normalize_replay_data,
    parse_coord,
)


class TestBlunderExtractor(unittest.TestCase):
    def test_parse_coord(self):
        self.assertEqual(parse_coord({"x": 5, "y": 3}), (5, 3))
        self.assertEqual(parse_coord((2, 7)), (2, 7))
        self.assertEqual(parse_coord([4, 1]), (4, 1))

    def test_get_move_between(self):
        # Standard
        self.assertEqual(get_move_between((5, 5), (5, 6), 11, 11), "up")
        self.assertEqual(get_move_between((5, 5), (5, 4), 11, 11), "down")
        self.assertEqual(get_move_between((5, 5), (4, 5), 11, 11), "left")
        self.assertEqual(get_move_between((5, 5), (6, 5), 11, 11), "right")

        # Wrap
        self.assertEqual(get_move_between((0, 5), (10, 5), 11, 11, wrap=True), "left")
        self.assertEqual(get_move_between((10, 5), (0, 5), 11, 11, wrap=True), "right")
        self.assertEqual(get_move_between((5, 0), (5, 10), 11, 11, wrap=True), "down")
        self.assertEqual(get_move_between((5, 10), (5, 0), 11, 11, wrap=True), "up")

    def test_normalize_replay_data_frames(self):
        raw = {
            "game": {
                "id": "game-123",
                "ruleset": {"name": "standard"},
                "width": 11,
                "height": 11,
            },
            "frames": [
                {"turn": 0, "snakes": []},
                {"turn": 1, "snakes": []},
            ],
        }
        meta, frames = normalize_replay_data(raw)
        self.assertEqual(meta["id"], "game-123")
        self.assertEqual(len(frames), 2)

    def test_detect_avoidable_wall_collision(self):
        # Snake at (0, 1), moving left into wall (x=-1) when 'up' and 'down' were open
        frame0 = {
            "turn": 5,
            "width": 11,
            "height": 11,
            "food": [{"x": 5, "y": 5}],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 90,
                    "body": [{"x": 0, "y": 1}, {"x": 1, "y": 1}, {"x": 2, "y": 1}],
                }
            ],
        }
        # Frame 1: player is eliminated
        frame1 = {
            "turn": 6,
            "width": 11,
            "height": 11,
            "food": [{"x": 5, "y": 5}],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 0,
                    "body": [{"x": -1, "y": 1}, {"x": 0, "y": 1}, {"x": 1, "y": 1}],
                    "eliminated": True,
                    "death": {"cause": "wall-collision", "turn": 6},
                }
            ],
        }
        metadata = {"id": "test_wall", "ruleset": {"name": "standard"}, "width": 11, "height": 11}
        blunders = find_blunders(metadata, [frame0, frame1])
        self.assertEqual(len(blunders), 1)
        b = blunders[0]
        self.assertEqual(b.turn, 5)
        self.assertEqual(b.snake_id, "player")
        self.assertEqual(b.blunder_move, "left")
        self.assertEqual(b.blunder_cause, "wall-collision")
        self.assertIn("up", b.safe_moves)
        self.assertIn("down", b.safe_moves)
        self.assertNotIn("left", b.safe_moves)
        self.assertNotIn("right", b.safe_moves)  # neck reversal

    def test_detect_avoidable_opponent_body_collision(self):
        # Player at (2, 2) moving right into opponent body segment at (3, 2), while 'up' was open
        frame0 = {
            "turn": 10,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 80,
                    "body": [{"x": 2, "y": 2}, {"x": 2, "y": 1}, {"x": 2, "y": 0}],
                },
                {
                    "id": "opp",
                    "health": 90,
                    "body": [{"x": 4, "y": 2}, {"x": 3, "y": 2}, {"x": 3, "y": 3}],
                },
            ],
        }
        frame1 = {
            "turn": 11,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 0,
                    "body": [{"x": 3, "y": 2}, {"x": 2, "y": 2}, {"x": 2, "y": 1}],
                    "eliminated": True,
                    "death": {"cause": "snake-collision", "turn": 11},
                },
                {
                    "id": "opp",
                    "health": 89,
                    "body": [{"x": 5, "y": 2}, {"x": 4, "y": 2}, {"x": 3, "y": 2}],
                },
            ],
        }
        metadata = {"id": "test_opp_body", "ruleset": {"name": "standard"}, "width": 11, "height": 11}
        blunders = find_blunders(metadata, [frame0, frame1])
        self.assertEqual(len(blunders), 1)
        b = blunders[0]
        self.assertEqual(b.turn, 10)
        self.assertEqual(b.blunder_move, "right")
        self.assertEqual(b.blunder_cause, "snake-collision")
        self.assertIn("up", b.safe_moves)
        self.assertIn("left", b.safe_moves)

    def test_detect_avoidable_self_collision(self):
        # Snake at (5, 5), neck at (5, 4), body curls around (4, 5). Moving left into (4, 5)
        frame0 = {
            "turn": 20,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 70,
                    "body": [
                        {"x": 5, "y": 5},
                        {"x": 5, "y": 4},
                        {"x": 4, "y": 4},
                        {"x": 4, "y": 5},
                        {"x": 4, "y": 6},
                    ],
                }
            ],
        }
        frame1 = {
            "turn": 21,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 0,
                    "body": [{"x": 4, "y": 5}, {"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 4, "y": 4}, {"x": 4, "y": 5}],
                    "eliminated": True,
                    "death": {"cause": "snake-self-collision", "turn": 21},
                }
            ],
        }
        metadata = {"id": "test_self_body", "ruleset": {"name": "standard"}, "width": 11, "height": 11}
        blunders = find_blunders(metadata, [frame0, frame1])
        self.assertEqual(len(blunders), 1)
        b = blunders[0]
        self.assertEqual(b.blunder_move, "left")
        self.assertEqual(b.blunder_cause, "snake-self-collision")
        self.assertIn("up", b.safe_moves)
        self.assertIn("right", b.safe_moves)

    def test_unavoidable_death_is_not_blunder(self):
        # Snake trapped with 0 free moves (surrounded on all sides)
        frame0 = {
            "turn": 30,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 10,
                    "body": [
                        {"x": 0, "y": 0},
                        {"x": 1, "y": 0},
                        {"x": 1, "y": 1},
                        {"x": 0, "y": 1},
                    ],
                }
            ],
        }
        # Move up into (0, 1) or right into (1, 0): both collide
        frame1 = {
            "turn": 31,
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
            "snakes": [
                {
                    "id": "player",
                    "health": 0,
                    "body": [{"x": 0, "y": 1}, {"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}],
                    "eliminated": True,
                }
            ],
        }
        metadata = {"id": "test_unavoidable", "ruleset": {"name": "standard"}, "width": 11, "height": 11}
        blunders = find_blunders(metadata, [frame0, frame1])
        # Since safe_moves is empty, it was not an avoidable blunder
        self.assertEqual(len(blunders), 0)

    def test_generate_unit_test_code_syntax(self):
        blunder = BlunderDetection(
            turn=42,
            snake_id="my_snake",
            blunder_move="down",
            blunder_cause="wall-collision",
            safe_moves=["left", "right"],
            state_before={
                "width": 11,
                "height": 11,
                "turn": 42,
                "player": {
                    "id": "my_snake",
                    "health": 85,
                    "body": [(5, 0), (5, 1), (5, 2)],
                },
                "opponents": [
                    {
                        "id": "opp1",
                        "health": 90,
                        "body": [(8, 8), (8, 9), (8, 10)],
                    }
                ],
                "food": [(5, 5)],
                "hazards": [],
            },
            width=11,
            height=11,
            wrap=False,
            constrictor=False,
            game_id="game_abc_123",
        )

        code = generate_unit_test_code(blunder)
        self.assertIn("def test_blunder_game_game_abc_123_turn_42_wall_collision(self):", code)
        self.assertIn('self.assertIn(move, (\'left\', \'right\')', code)
        self.assertIn('self.assertNotEqual(\n                move,\n                "down"', code)

        # Verify generated code is 100% syntactically valid Python
        wrapped_code = f"class GeneratedTest:\n{code}"
        parsed = ast.parse(wrapped_code)
        self.assertIsNotNone(parsed)


if __name__ == "__main__":
    unittest.main()
