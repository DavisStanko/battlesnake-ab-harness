"""
test_example_snakes.py — Unit tests for reference example strategies:
- example_snakes.survival
- example_snakes.variant_template
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from example_snakes import survival, variant_template


class TestExampleSnakes(unittest.TestCase):
    def test_interfaces(self):
        for mod in (survival, variant_template):
            self.assertTrue(hasattr(mod, "INFO"))
            self.assertIsInstance(mod.INFO, dict)
            self.assertIn("apiversion", mod.INFO)
            self.assertTrue(callable(getattr(mod, "choose_move", None)))
            self.assertTrue(callable(getattr(mod, "move", None)))

    def test_out_of_bounds_avoidance(self):
        # Snake in corner (0, 0)
        game_state = {
            "board": {
                "width": 11,
                "height": 11,
                "snakes": [
                    {
                        "id": "me",
                        "health": 90,
                        "body": [{"x": 0, "y": 0}, {"x": 0, "y": 1}, {"x": 0, "y": 2}],
                    }
                ],
                "food": [],
                "hazards": [],
            },
            "you": {
                "id": "me",
                "health": 90,
                "head": {"x": 0, "y": 0},
                "body": [{"x": 0, "y": 0}, {"x": 0, "y": 1}, {"x": 0, "y": 2}],
            },
        }
        # In corner (0, 0), "down" (0, -1) and "left" (-1, 0) are out of bounds.
        # "up" (0, 1) hits body.
        # Only "right" (1, 0) is safe.
        move = survival.choose_move(game_state, wrap=False, constrictor=False)
        self.assertEqual(move, "right")

    def test_tail_vacating_when_health_lt_100(self):
        # 3-segment snake chasing its own tail in 2x2 loop:
        # head: (1, 1), neck: (1, 0), tail: (0, 0), segment: (0, 1)
        # body: [(1, 1), (0, 1), (0, 0)] -> tail is at (0, 0)
        # If moving "left" to (0, 1): hits neck.
        # If moving "down" to (1, 0): open.
        # If moving from (1, 0) into tail (0, 0): safe if health < 100.
        game_state = {
            "board": {
                "width": 11,
                "height": 11,
                "snakes": [
                    {
                        "id": "me",
                        "health": 80,
                        "body": [{"x": 5, "y": 5}, {"x": 5, "y": 6}, {"x": 6, "y": 6}, {"x": 6, "y": 5}],
                    }
                ],
                "food": [],
            },
            "you": {
                "id": "me",
                "health": 80,
                "head": {"x": 5, "y": 5},
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 6}, {"x": 6, "y": 6}, {"x": 6, "y": 5}],
            },
        }
        # Tail is at (6, 5). Moving "right" steps onto tail (6, 5).
        # Since health is 80 (< 100), tail vacates and right should be allowed.
        # Let's test multiple times to confirm "right" is among valid choices.
        choices = {survival.choose_move(game_state, wrap=False) for _ in range(50)}
        self.assertIn("right", choices)

    def test_tail_stays_when_health_100(self):
        # When health == 100 (snake just ate), tail does NOT vacate.
        # Surround head at (5, 5) with neck at (5, 6), segment at (5, 4), wall/segment at (4, 5).
        # And tail at (6, 5).
        game_state = {
            "board": {
                "width": 11,
                "height": 11,
                "snakes": [
                    {
                        "id": "me",
                        "health": 100,
                        "body": [
                            {"x": 5, "y": 5},  # head
                            {"x": 5, "y": 6},  # up
                            {"x": 4, "y": 5},  # left
                            {"x": 5, "y": 4},  # down
                            {"x": 6, "y": 5},  # right (tail)
                        ],
                    }
                ],
                "food": [],
            },
            "you": {
                "id": "me",
                "health": 100,
                "head": {"x": 5, "y": 5},
                "body": [
                    {"x": 5, "y": 5},
                    {"x": 5, "y": 6},
                    {"x": 4, "y": 5},
                    {"x": 5, "y": 4},
                    {"x": 6, "y": 5},
                ],
            },
        }
        # All directions hit body, and tail at (6, 5) stays because health == 100.
        # Surviving moves is empty -> fallback is "up".
        move = survival.choose_move(game_state, wrap=False)
        self.assertEqual(move, "up")

    def test_wrap_mode(self):
        # Head at (0, 5). Moving "left" in wrap wraps to (10, 5).
        game_state = {
            "board": {
                "width": 11,
                "height": 11,
                "snakes": [
                    {
                        "id": "me",
                        "health": 90,
                        "body": [
                            {"x": 0, "y": 5},
                            {"x": 0, "y": 6},
                            {"x": 0, "y": 4},
                            {"x": 1, "y": 5},
                            {"x": 2, "y": 5},
                        ],
                    }
                ],
                "food": [],
            },
            "you": {
                "id": "me",
                "health": 90,
                "head": {"x": 0, "y": 5},
                "body": [
                    {"x": 0, "y": 5},
                    {"x": 0, "y": 6},
                    {"x": 0, "y": 4},
                    {"x": 1, "y": 5},
                    {"x": 2, "y": 5},
                ],
            },
        }
        # "up", "down", "right" are blocked by body.
        # "left" in wrap goes to (10, 5) which is unoccupied!
        move = survival.choose_move(game_state, wrap=True)
        self.assertEqual(move, "left")

    def test_variant_template_matches_survival(self):
        # Confirm variant_template produces same behavior on unambiguous board
        game_state = {
            "board": {
                "width": 11,
                "height": 11,
                "snakes": [
                    {
                        "id": "me",
                        "health": 90,
                        "body": [{"x": 0, "y": 0}, {"x": 0, "y": 1}, {"x": 0, "y": 2}],
                    }
                ],
                "food": [],
            },
            "you": {
                "id": "me",
                "health": 90,
                "head": {"x": 0, "y": 0},
                "body": [{"x": 0, "y": 0}, {"x": 0, "y": 1}, {"x": 0, "y": 2}],
            },
        }
        move = variant_template.choose_move(game_state, wrap=False)
        self.assertEqual(move, "right")


if __name__ == "__main__":
    unittest.main()
