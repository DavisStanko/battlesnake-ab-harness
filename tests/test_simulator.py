import os
import sys
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simulator import (
    simulate_turn,
    ELIMINATED_BY_COLLISION,
    ELIMINATED_BY_HEAD_COLLISION,
    ELIMINATED_BY_OUT_OF_BOUNDS,
    ELIMINATED_BY_OUT_OF_HEALTH,
    ELIMINATED_BY_SELF_COLLISION,
    ELIMINATED_BY_HAZARD,
    NOT_ELIMINATED,
)


class TestBattlesnakeSimulator(unittest.TestCase):
    """
    Comprehensive unit tests for the Battlesnake forward game simulator.
    Covers rules 1 to 10 and all explicit edge cases.
    """

    def setUp(self):
        self.default_board = {
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
        }

    # ═════════════════════════════════════════════════════════════════════════
    # EXPLICIT REQUIRED EDGE CASE TESTS (Named tests for each requirement)
    # ═════════════════════════════════════════════════════════════════════════

    def test_head_to_head_different_lengths_shorter_dies(self):
        """
        Edge Case 1:
        Two snakes move head-to-head into the same cell, different lengths.
        The shorter one dies; the longer one survives.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Snake 1: length 4, moves right into (5, 5)
        # Snake 2: length 3, moves left into (5, 5)
        snakes = [
            {
                "id": "longer",
                "health": 100,
                "body": [{"x": 4, "y": 5}, {"x": 3, "y": 5}, {"x": 2, "y": 5}, {"x": 1, "y": 5}],
            },
            {
                "id": "shorter",
                "health": 100,
                "body": [{"x": 6, "y": 5}, {"x": 7, "y": 5}, {"x": 8, "y": 5}],
            },
        ]
        moves = {"longer": "right", "shorter": "left"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # Expected: only "longer" survives
        self.assertEqual(len(new_snakes), 1)
        self.assertEqual(new_snakes[0]["id"], "longer")
        self.assertEqual(new_snakes[0]["body"][0], {"x": 5, "y": 5})
        self.assertEqual(new_snakes[0]["health"], 99)
        self.assertEqual(new_snakes[0]["length"], 4)

        # Expected: "shorter" in eliminated_snakes with head-collision cause
        self.assertEqual(len(new_board["eliminated_snakes"]), 1)
        elim = new_board["eliminated_snakes"][0]
        self.assertEqual(elim["id"], "shorter")
        self.assertEqual(elim["eliminated_cause"], ELIMINATED_BY_HEAD_COLLISION)
        self.assertEqual(elim["eliminated_by"], "longer")

    def test_head_to_head_same_length_both_die(self):
        """
        Edge Case 2:
        Two snakes move head-to-head into the same cell, same length.
        Both snakes die.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Snake A: length 3, moves right into (5, 5)
        # Snake B: length 3, moves left into (5, 5)
        snakes = [
            {
                "id": "snake_a",
                "health": 100,
                "body": [{"x": 4, "y": 5}, {"x": 3, "y": 5}, {"x": 2, "y": 5}],
            },
            {
                "id": "snake_b",
                "health": 100,
                "body": [{"x": 6, "y": 5}, {"x": 7, "y": 5}, {"x": 8, "y": 5}],
            },
        ]
        moves = {"snake_a": "right", "snake_b": "left"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # Expected: 0 alive snakes
        self.assertEqual(len(new_snakes), 0)
        self.assertEqual(len(new_board["snakes"]), 0)

        # Expected: both eliminated by head-collision
        self.assertEqual(len(new_board["eliminated_snakes"]), 2)
        elim_causes = {s["id"]: s["eliminated_cause"] for s in new_board["eliminated_snakes"]}
        self.assertEqual(elim_causes["snake_a"], ELIMINATED_BY_HEAD_COLLISION)
        self.assertEqual(elim_causes["snake_b"], ELIMINATED_BY_HEAD_COLLISION)

    def test_tail_cell_occupied_when_eating_food(self):
        """
        Edge Case 3:
        A snake eats food and grows, on the same turn another snake's head would
        have hit the old tail cell (this cell is now still occupied, since the
        snake grew and did not vacate it).
        """
        # Snake 1 body: head=(5,6), neck=(5,5), tail=(5,4).
        # Food is at (5,7). Snake 1 moves up to (5,7) and eats food.
        # Because Snake 1 ate food, its tail at (5,4) is NOT removed.
        # Snake 2 body: head=(5,3), (5,2), (5,1). Moves up into (5,4).
        # Snake 2's head hits Snake 1's tail cell at (5,4) and is eliminated.
        board = {
            "width": 11,
            "height": 11,
            "food": [{"x": 5, "y": 7}],
            "hazards": [],
        }
        snakes = [
            {
                "id": "feeder",
                "health": 50,
                "body": [{"x": 5, "y": 6}, {"x": 5, "y": 5}, {"x": 5, "y": 4}],
            },
            {
                "id": "tail_chaser",
                "health": 100,
                "body": [{"x": 5, "y": 3}, {"x": 5, "y": 2}, {"x": 5, "y": 1}],
            },
        ]
        moves = {"feeder": "up", "tail_chaser": "up"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # "feeder" survived, length grew to 4, health reset to 100
        self.assertEqual(len(new_snakes), 1)
        feeder = new_snakes[0]
        self.assertEqual(feeder["id"], "feeder")
        self.assertEqual(feeder["length"], 4)
        self.assertEqual(feeder["health"], 100)
        self.assertEqual(
            feeder["body"],
            [{"x": 5, "y": 7}, {"x": 5, "y": 6}, {"x": 5, "y": 5}, {"x": 5, "y": 4}],
        )

        # "tail_chaser" collided with feeder's body at (5,4) and was eliminated
        self.assertEqual(len(new_board["eliminated_snakes"]), 1)
        elim = new_board["eliminated_snakes"][0]
        self.assertEqual(elim["id"], "tail_chaser")
        self.assertEqual(elim["eliminated_cause"], ELIMINATED_BY_COLLISION)
        self.assertEqual(elim["eliminated_by"], "feeder")

    def test_tail_cell_vacated_when_not_eating_food(self):
        """
        Edge Case 4:
        A snake moves into a cell that used to be another snake's tail, where
        that other snake is moving away (not eating) — this should be a legal,
        safe move.
        """
        # Snake 1 body: head=(5,6), neck=(5,5), tail=(5,4).
        # No food. Snake 1 moves up to (5,7).
        # Because Snake 1 did not eat, its tail cell at (5,4) is vacated.
        # Snake 2 body: head=(5,3), (5,2), (5,1). Moves up into (5,4).
        # Snake 2 enters the vacated cell safely!
        board = {
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [],
        }
        snakes = [
            {
                "id": "runner",
                "health": 100,
                "body": [{"x": 5, "y": 6}, {"x": 5, "y": 5}, {"x": 5, "y": 4}],
            },
            {
                "id": "chaser",
                "health": 100,
                "body": [{"x": 5, "y": 3}, {"x": 5, "y": 2}, {"x": 5, "y": 1}],
            },
        ]
        moves = {"runner": "up", "chaser": "up"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # Both snakes survive safely!
        self.assertEqual(len(new_snakes), 2)
        runner = next(s for s in new_snakes if s["id"] == "runner")
        chaser = next(s for s in new_snakes if s["id"] == "chaser")

        self.assertEqual(
            runner["body"],
            [{"x": 5, "y": 7}, {"x": 5, "y": 6}, {"x": 5, "y": 5}],
        )
        self.assertEqual(
            chaser["body"],
            [{"x": 5, "y": 4}, {"x": 5, "y": 3}, {"x": 5, "y": 2}],
        )
        self.assertEqual(len(new_board["eliminated_snakes"]), 0)

    def test_snake_moves_out_of_bounds(self):
        """
        Edge Case 5:
        A snake moves out of bounds.
        Confirm it is eliminated with cause 'wall-collision'.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Head is at (0, 5), moves left into (-1, 5)
        snakes = [
            {
                "id": "wall_crasher",
                "health": 100,
                "body": [{"x": 0, "y": 5}, {"x": 1, "y": 5}, {"x": 2, "y": 5}],
            }
        ]
        moves = {"wall_crasher": "left"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        self.assertEqual(len(new_snakes), 0)
        self.assertEqual(len(new_board["eliminated_snakes"]), 1)
        elim = new_board["eliminated_snakes"][0]
        self.assertEqual(elim["id"], "wall_crasher")
        self.assertEqual(elim["eliminated_cause"], ELIMINATED_BY_OUT_OF_BOUNDS)

    def test_snake_moves_into_own_neck_self_collision(self):
        """
        Edge Case 6:
        A snake moves into its own neck (illegal move / immediate self-collision).
        Confirm it is eliminated with cause 'snake-self-collision'.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Head is at (5, 5), neck is at (5, 4).
        # Snake moves "down" directly into its own neck at (5, 4).
        snakes = [
            {
                "id": "neck_reverser",
                "health": 100,
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 5, "y": 3}],
            }
        ]
        moves = {"neck_reverser": "down"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        self.assertEqual(len(new_snakes), 0)
        self.assertEqual(len(new_board["eliminated_snakes"]), 1)
        elim = new_board["eliminated_snakes"][0]
        self.assertEqual(elim["id"], "neck_reverser")
        self.assertEqual(elim["eliminated_cause"], ELIMINATED_BY_SELF_COLLISION)
        self.assertEqual(elim["eliminated_by"], "neck_reverser")

    def test_health_reaches_exactly_zero_normal_decay_starvation(self):
        """
        Edge Case 7:
        A snake's health reaches exactly 0 from normal decay (no hazard) —
        confirm it is eliminated with cause 'out-of-health'.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Health starts at exactly 1. Loses 1 health from normal turn decay -> reaches 0.
        snakes = [
            {
                "id": "starving_snake",
                "health": 1,
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 5, "y": 3}],
            }
        ]
        moves = {"starving_snake": "up"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        self.assertEqual(len(new_snakes), 0)
        self.assertEqual(len(new_board["eliminated_snakes"]), 1)
        elim = new_board["eliminated_snakes"][0]
        self.assertEqual(elim["id"], "starving_snake")
        self.assertEqual(elim["health"], 0)
        self.assertEqual(elim["eliminated_cause"], ELIMINATED_BY_OUT_OF_HEALTH)

    def test_hazard_and_food_overlap_resets_health_to_100(self):
        """
        Edge Case 8:
        A snake lands on a hazard and food on the same turn (overlapping cell) —
        confirm health resets to 100 correctly (food overrides hazard damage,
        per official rules).
        """
        # Hazard and food both at (5, 6). Snake starts at (5, 5) with health 50.
        board = {
            "width": 11,
            "height": 11,
            "food": [{"x": 5, "y": 6}],
            "hazards": [{"x": 5, "y": 6}],
        }
        snakes = [
            {
                "id": "hazard_eater",
                "health": 50,
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 5, "y": 3}],
            }
        ]
        moves = {"hazard_eater": "up"}

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        self.assertEqual(len(new_snakes), 1)
        s = new_snakes[0]
        self.assertEqual(s["id"], "hazard_eater")
        # Health must be 100 (food resets health and protects from hazard)
        self.assertEqual(s["health"], 100)
        self.assertEqual(s["length"], 4)
        # Food is removed
        self.assertEqual(new_board["food"], [])
        # Hazard is retained
        self.assertEqual(new_board["hazards"], [{"x": 5, "y": 6}])
        self.assertEqual(len(new_board["eliminated_snakes"]), 0)

    def test_three_snakes_two_collide_head_to_head_third_unaffected(self):
        """
        Edge Case 9:
        Three or more snakes, where two collide head-to-head and a third is
        unaffected — confirm only the correct two are eliminated.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Snake 1 (len 3) moves right to (5, 5)
        # Snake 2 (len 3) moves left to (5, 5)
        # Snake 3 (len 3) moves down to (1, 0)
        snakes = [
            {
                "id": "colliding_1",
                "health": 100,
                "body": [{"x": 4, "y": 5}, {"x": 3, "y": 5}, {"x": 2, "y": 5}],
            },
            {
                "id": "colliding_2",
                "health": 100,
                "body": [{"x": 6, "y": 5}, {"x": 7, "y": 5}, {"x": 8, "y": 5}],
            },
            {
                "id": "unaffected_3",
                "health": 80,
                "body": [{"x": 1, "y": 1}, {"x": 1, "y": 2}, {"x": 1, "y": 3}],
            },
        ]
        moves = {
            "colliding_1": "right",
            "colliding_2": "left",
            "unaffected_3": "down",
        }

        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # Only "unaffected_3" survives
        self.assertEqual(len(new_snakes), 1)
        survivor = new_snakes[0]
        self.assertEqual(survivor["id"], "unaffected_3")
        self.assertEqual(survivor["health"], 79)
        self.assertEqual(survivor["body"][0], {"x": 1, "y": 0})

        # Exactly the two colliding snakes are eliminated
        self.assertEqual(len(new_board["eliminated_snakes"]), 2)
        elim_ids = {s["id"] for s in new_board["eliminated_snakes"]}
        self.assertEqual(elim_ids, {"colliding_1", "colliding_2"})

    # ═════════════════════════════════════════════════════════════════════════
    # ADDITIONAL COMPREHENSIVE TESTS (Rules 1-10 & Engine Parity)
    # ═════════════════════════════════════════════════════════════════════════

    def test_simultaneous_eliminations_rule_10(self):
        """
        Rule 10 verification:
        Snake A moves into Snake B's body, while Snake B moves out of bounds.
        Even though Snake B dies out of bounds, its body before elimination
        still eliminates Snake A in the same turn.
        """
        board = {"width": 11, "height": 11, "food": [], "hazards": []}
        # Snake B: at top edge, moves up out of bounds (y=10 -> y=11)
        # Snake A: moves into Snake B's body at (5, 9)
        snakes = [
            {
                "id": "snake_a",
                "health": 100,
                "body": [{"x": 4, "y": 9}, {"x": 3, "y": 9}, {"x": 2, "y": 9}],
            },
            {
                "id": "snake_b",
                "health": 100,
                "body": [{"x": 5, "y": 10}, {"x": 5, "y": 9}, {"x": 5, "y": 8}],
            },
        ]
        moves = {"snake_a": "right", "snake_b": "up"}
        new_board, new_snakes = simulate_turn(board, snakes, moves)

        # Both snakes are eliminated simultaneously!
        self.assertEqual(len(new_snakes), 0)
        self.assertEqual(len(new_board["eliminated_snakes"]), 2)
        causes = {s["id"]: s["eliminated_cause"] for s in new_board["eliminated_snakes"]}
        self.assertEqual(causes["snake_b"], ELIMINATED_BY_OUT_OF_BOUNDS)
        self.assertEqual(causes["snake_a"], ELIMINATED_BY_COLLISION)

    def test_hazard_damage_without_food(self):
        """Snake landing in hazard without food takes 1 (decay) + 15 (hazard) = 16 damage."""
        board = {
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [{"x": 5, "y": 6}],
        }
        snakes = [
            {
                "id": "s1",
                "health": 100,
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 5, "y": 3}],
            }
        ]
        _, new_snakes = simulate_turn(board, snakes, {"s1": "up"})
        self.assertEqual(new_snakes[0]["health"], 84)

    def test_custom_hazard_damage_parameter(self):
        """Custom hazard damage parameter applies correctly."""
        board = {
            "width": 11,
            "height": 11,
            "food": [],
            "hazards": [{"x": 5, "y": 6}],
        }
        snakes = [
            {
                "id": "s1",
                "health": 100,
                "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}, {"x": 5, "y": 3}],
            }
        ]
        _, new_snakes = simulate_turn(board, snakes, {"s1": "up"}, hazard_damage=30)
        # 100 - 1 - 30 = 69
        self.assertEqual(new_snakes[0]["health"], 69)

    def test_tuple_coordinates_and_list_moves(self):
        """Verify support for tuple coordinates and list of moves."""
        board = {"width": 11, "height": 11, "food": [(5, 6)], "hazards": []}
        snakes = [
            {"id": "s1", "health": 80, "body": [(5, 5), (5, 4), (5, 3)]},
            {"id": "s2", "health": 80, "body": [(1, 1), (1, 2), (1, 3)]},
        ]
        moves = ["up", "down"]
        new_board, new_snakes = simulate_turn(board, snakes, moves)

        self.assertEqual(len(new_snakes), 2)
        # Preserves tuple format
        self.assertEqual(new_snakes[0]["body"][0], (5, 6))
        self.assertEqual(new_snakes[0]["health"], 100)
        self.assertEqual(new_snakes[0]["length"], 4)
        self.assertEqual(new_snakes[1]["body"][0], (1, 0))
        self.assertEqual(new_snakes[1]["health"], 79)


    def test_movement_all_four_directions(self):
        """Verify movement offsets for up, down, left, right."""
        offsets = {
            "up": {"x": 5, "y": 6},
            "down": {"x": 5, "y": 4},
            "left": {"x": 4, "y": 5},
            "right": {"x": 6, "y": 5},
        }
        for direction, expected_head in offsets.items():
            snakes = [{"id": "s", "health": 100, "body": [{"x": 5, "y": 5}, {"x": 5, "y": 5}]}]
            _, new_snakes = simulate_turn(self.default_board, snakes, {"s": direction})
            self.assertEqual(new_snakes[0]["body"][0], expected_head)

    def test_eat_food_on_turn_with_one_health_left(self):
        """Snake starting with health 1 loses 1 health to 0, eats food -> resets to 100 and survives."""
        board = {"width": 11, "height": 11, "food": [{"x": 5, "y": 6}], "hazards": []}
        snakes = [{"id": "s", "health": 1, "body": [{"x": 5, "y": 5}, {"x": 5, "y": 4}]}]
        _, new_snakes = simulate_turn(board, snakes, {"s": "up"})
        self.assertEqual(len(new_snakes), 1)
        self.assertEqual(new_snakes[0]["health"], 100)

    def test_multiple_snakes_eat_same_food(self):
        """When multiple snakes eat the same food, both grow and have health reset to 100."""
        board = {"width": 11, "height": 11, "food": [{"x": 5, "y": 5}], "hazards": []}
        snakes = [
            {"id": "s1", "health": 50, "body": [{"x": 4, "y": 5}, {"x": 3, "y": 5}, {"x": 2, "y": 5}, {"x": 1, "y": 5}]},
            {"id": "s2", "health": 50, "body": [{"x": 6, "y": 5}, {"x": 7, "y": 5}, {"x": 8, "y": 5}]},
        ]
        # s1 (len 4 -> 5) moves right to (5,5); s2 (len 3 -> 4) moves left to (5,5)
        # Head-to-head at (5,5): s1 (len 5) survives, s2 (len 4) eliminated
        new_board, new_snakes = simulate_turn(board, snakes, {"s1": "right", "s2": "left"})
        self.assertEqual(new_board["food"], [])
        self.assertEqual(len(new_snakes), 1)
        self.assertEqual(new_snakes[0]["id"], "s1")
        self.assertEqual(new_snakes[0]["health"], 100)
        self.assertEqual(new_snakes[0]["length"], 5)
        self.assertEqual(new_board["eliminated_snakes"][0]["id"], "s2")


if __name__ == "__main__":
    unittest.main()
