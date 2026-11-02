"""
test_baseline_cache_equality.py — Tests verifying identical move and evaluation outputs
with baseline per-turn cache enabled vs disabled across standardized game fixtures.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import unittest

from strategies import strategy_baseline

POSITIONS_DIR = Path(__file__).resolve().parent.parent / "positions"


class TestBaselineCacheEquality(unittest.TestCase):
    """
    Verifies that enabling the shared per-turn CState / board cache produces
    strictly identical move decisions and leaf evaluation scores across all snakes
    in standard fixture positions.
    """

    def setUp(self) -> None:
        self.fixtures = sorted(POSITIONS_DIR.glob("*.json"))
        self.assertTrue(len(self.fixtures) > 0, "No fixture files found in harness/positions/")
        self._orig_budget = getattr(strategy_baseline, "TIME_BUDGET_SEC", 0.45)
        strategy_baseline.TIME_BUDGET_SEC = 0.03

    def tearDown(self) -> None:
        strategy_baseline.TIME_BUDGET_SEC = self._orig_budget

    def test_cache_equality_across_fixtures(self) -> None:
        positions_tested = 0
        snakes_tested = 0

        for fixture_path in self.fixtures:
            with open(fixture_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            ruleset_name = str(data.get("game", {}).get("ruleset", {}).get("name", "standard")).lower()
            wrap = ruleset_name in ("wrapped", "wrapped-constrictor", "spicy-meteors")
            constrictor = ruleset_name in ("constrictor", "wrapped-constrictor")

            board_snakes = data.get("board", {}).get("snakes", [])
            if not board_snakes:
                continue

            positions_tested += 1

            # 1. Evaluate all snakes WITHOUT turn cache
            strategy_baseline.clear_snake_contexts()
            nocache_moves = []
            nocache_evals = []
            nocache_cstate_bytes = []
            for sn in board_snakes:
                gs = copy.deepcopy(data)
                gs["you"] = copy.deepcopy(sn)
                m = strategy_baseline.choose_move(gs, wrap=wrap, constrictor=constrictor)
                cs = strategy_baseline._py_state_to_c(gs, wrap=wrap, constrictor=constrictor)
                ev = strategy_baseline.eval_state(cs)
                nocache_moves.append(m)
                nocache_evals.append(ev)
                nocache_cstate_bytes.append(bytes(cs))

            # 2. Evaluate all snakes WITH shared turn cache (simulating one turn)
            strategy_baseline.clear_snake_contexts()
            turn_cache = {}
            cache_moves = []
            cache_evals = []
            cache_cstate_bytes = []
            for sn in board_snakes:
                gs = copy.deepcopy(data)
                gs["you"] = copy.deepcopy(sn)
                gs["_turn_cache"] = turn_cache
                m = strategy_baseline.choose_move(gs, wrap=wrap, constrictor=constrictor, turn_cache=turn_cache)
                cs = strategy_baseline._py_state_to_c(gs, wrap=wrap, constrictor=constrictor, turn_cache=turn_cache)
                ev = strategy_baseline.eval_state(cs)
                cache_moves.append(m)
                cache_evals.append(ev)
                cache_cstate_bytes.append(bytes(cs))

            # 3. Assert strict equivalence
            for idx, sn in enumerate(board_snakes):
                sn_id = sn.get("id", f"snake_{idx}")
                self.assertEqual(
                    cache_moves[idx],
                    nocache_moves[idx],
                    f"Move mismatch in fixture {fixture_path.name} for snake {sn_id}: "
                    f"cache={cache_moves[idx]} vs nocache={nocache_moves[idx]}",
                )
                self.assertEqual(
                    cache_evals[idx],
                    nocache_evals[idx],
                    f"Eval mismatch in fixture {fixture_path.name} for snake {sn_id}: "
                    f"cache={cache_evals[idx]} vs nocache={nocache_evals[idx]}",
                )
                self.assertEqual(
                    cache_cstate_bytes[idx],
                    nocache_cstate_bytes[idx],
                    f"CState byte mismatch in fixture {fixture_path.name} for snake {sn_id}",
                )
                snakes_tested += 1

        self.assertTrue(positions_tested >= 10, f"Expected >= 10 positions tested, got {positions_tested}")
        self.assertTrue(snakes_tested >= 30, f"Expected >= 30 snakes tested, got {snakes_tested}")


if __name__ == "__main__":
    unittest.main()
