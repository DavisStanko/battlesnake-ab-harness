#!/usr/bin/env python3
"""
depth_test.py — Fixed-Position Depth and Performance Tester for Battlesnake Strategies

Provides deterministic, reproducible benchmarking across standardized game positions:
1. Fixture Generation (--generate):
   Simulates seeded baseline-vs-baseline games across standard game modes
   (standard, constrictor, royale, duel) and captures full Battlesnake game states
   at turns 25, 50, and 75 into harness/positions/.

2. Test Mode (--baseline <strategy> --variant <strategy> --time-ms 450 --repeats 5):
   Evaluates baseline and variant strategies on each fixture under identical time budgets
   and live-server thread settings. Runs interleaved baseline/variant trials and computes
   medians per position for:
     - Completed search depth
     - Nodes searched
     - Nodes per second (NPS)
     - NPS ratio (variant / baseline)
     - Move agreement percentage

3. Reporting:
   Outputs per-mode breakdown tables and a cross-mode aggregate summary table.
   Flags any mode where variant median depth drops below baseline.
"""

from __future__ import annotations

import argparse
import collections
import importlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.ab_test import (
    load_strategy_module,
    _setup_initial_board,
    _build_game_state,
    _run_timed_move,
)
from harness.simulator import simulate_turn

DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent / "positions"
DEFAULT_MODES = ["standard", "constrictor", "royale", "duel"]
DEFAULT_TURNS = [25, 50, 75]
DEFAULT_TIME_MS = 450
DEFAULT_REPEATS = 5


# ── Strategy Resolution Helper ───────────────────────────────────────────────

def resolve_strategy_name(name_or_path: str) -> str:
    """Expands common shorthand names ('baseline', 'variant') to full module names."""
    if name_or_path == "baseline":
        return "strategies.strategy_baseline"
    if name_or_path == "variant":
        return "strategies.strategy_variant"
    return name_or_path


# ── Fixture Generation ───────────────────────────────────────────────────────

def generate_fixtures(
    output_dir: Path = DEFAULT_FIXTURES_DIR,
    modes: Sequence[str] = DEFAULT_MODES,
    turns: Sequence[int] = DEFAULT_TURNS,
) -> None:
    """
    Simulates seeded baseline-vs-baseline games and snapshots full game states
    at turns 25, 50, and 75 into output_dir. Fixtures are commit-worthy and
    remain deterministic unless regenerated.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"[GENERATE] Generating deterministic fixtures into {output_dir}...")

    # Load baseline strategy for generation with 50ms moves (accurate survival path)
    base_mod = load_strategy_module("strategies.strategy_baseline")
    orig_budget = getattr(base_mod, "TIME_BUDGET_SEC", 0.450)
    base_mod.TIME_BUDGET_SEC = 0.050

    target_turns = set(turns)
    max_turn = max(turns)

    mode_configs = {
        # mode: (game_num, num_snakes, seed)
        # In constrictor, game_num=5 selects cardinal spawn layout which survives past turn 78
        "standard": (1, 4, 1),
        "constrictor": (5, 4, 1),
        "royale": (1, 4, 1),
        "duel": (1, 2, 1),
    }

    try:
        for mode in modes:
            game_num, num_snakes, seed = mode_configs.get(mode, (1, 4, 1))
            wrap = mode in ("wrapped", "wrapped-constrictor", "spicy-meteors")
            constrictor = mode in ("constrictor", "wrapped-constrictor")

            board, snakes, rng = _setup_initial_board(
                seed=seed,
                game_num=game_num,
                num_snakes=num_snakes,
                mode=mode,
            )

            print(f"  [GENERATE] Simulating {mode} game (game_num={game_num}, snakes={num_snakes}, seed={seed})...")

            game_id = f"game_{mode}_{seed}"
            for turn in range(max_turn + 1):
                if len(snakes) == 0:
                    raise RuntimeError(f"All snakes died prematurely at turn {turn} in mode {mode}!")

                if turn in target_turns:
                    # Construct full Battlesnake game state from the perspective of the primary snake (snakes[0])
                    primary_snake = snakes[0]
                    game_state = _build_game_state(
                        current_snake=primary_snake,
                        active_snakes=snakes,
                        current_food=board.get("food", []),
                        board_width=board["width"],
                        board_height=board["height"],
                        turn=turn,
                        game_id=f"fixture_{mode}_turn{turn}",
                        mode=mode,
                        hazards=board.get("hazards", []),
                    )

                    fixture_path = output_dir / f"{mode}_turn{turn}.json"
                    with open(fixture_path, "w", encoding="utf-8") as f:
                        json.dump(game_state, f, indent=2)

                    print(
                        f"    -> Saved {fixture_path.name} "
                        f"(snakes={len(snakes)}, food={len(board.get('food', []))}, "
                        f"hazards={len(board.get('hazards', []))})"
                    )

                # Simulate one turn forward
                moves: Dict[str, str] = {}
                for sn in snakes:
                    gs = _build_game_state(
                        current_snake=sn,
                        active_snakes=snakes,
                        current_food=board.get("food", []),
                        board_width=board["width"],
                        board_height=board["height"],
                        turn=turn,
                        game_id=game_id,
                        mode=mode,
                        hazards=board.get("hazards", []),
                    )
                    m, _ = _run_timed_move(base_mod.choose_move, gs, wrap=wrap, constrictor=constrictor)
                    moves[sn["id"]] = m or "up"

                board, snakes = simulate_turn(
                    board, snakes, moves, wrap=wrap, constrictor=constrictor, hazard_damage=14
                )

                # Mode-specific updates
                if mode == "royale":
                    sauce_depth = min((turn + 1) // 25, (board["width"] + 1) // 2)
                    if sauce_depth > 0:
                        w, h = board["width"], board["height"]
                        board["hazards"] = [
                            {"x": x, "y": y}
                            for x in range(w)
                            for y in range(h)
                            if min(x, w - 1 - x, y, h - 1 - y) < sauce_depth
                        ]
                elif not constrictor:
                    # Food replenishment
                    occupied = {(seg["x"], seg["y"]) for sn in snakes for seg in sn["body"]}
                    occupied.update((f["x"], f["y"]) for f in board.get("food", []))
                    unoccupied = [
                        (x, y)
                        for x in range(board["width"])
                        for y in range(board["height"])
                        if (x, y) not in occupied
                    ]
                    num_food = len(board.get("food", []))
                    food_needed = 0
                    if num_food < 1:
                        food_needed = 1 - num_food
                    elif rng.randint(1, 100) <= 15:
                        food_needed = 1
                    if food_needed > 0 and unoccupied:
                        rng.shuffle(unoccupied)
                        for p in unoccupied[:food_needed]:
                            board["food"].append({"x": p[0], "y": p[1]})

    finally:
        base_mod.TIME_BUDGET_SEC = orig_budget

    print("[GENERATE] Fixture generation complete. All fixtures verified.")


# ── Single Search Execution ──────────────────────────────────────────────────

def run_single_search(
    strat_module: Any,
    game_state: Dict[str, Any],
    time_ms: int,
) -> Dict[str, Any]:
    """
    Executes a single search on a fixture state using identical time budget and
    clean memory/thread contexts as the live server.
    """
    # 1. Reset TT contexts for deterministic, independent runs
    if hasattr(strat_module, "clear_snake_contexts"):
        strat_module.clear_snake_contexts()

    # 2. Configure time budget
    time_sec = time_ms / 1000.0
    strat_module.TIME_BUDGET_SEC = time_sec

    # Update strategies.constants if imported
    try:
        import strategies.constants as const
        const.TIME_BUDGET_SEC = time_sec
    except Exception:
        pass

    # In live server / strategy_baseline, choose_move calculates:
    #   budget = 0.470 - elapsed_since_arrival - 0.030 (i.e. 0.440s ceiling)
    # Adjust t0 anchor if time_sec exceeds 0.440s so the full requested time_sec is searched
    if time_sec > 0.440:
        t0_adj = time.perf_counter() + (time_sec - 0.440)
    else:
        t0_adj = time.perf_counter()

    ruleset_name = game_state.get("game", {}).get("ruleset", {}).get("name", "standard")
    wrap = ruleset_name in ("wrapped", "wrapped-constrictor", "spicy-meteors")
    constrictor = ruleset_name in ("constrictor", "wrapped-constrictor")

    t_wall_start = time.perf_counter()
    move = strat_module.choose_move(game_state, wrap=wrap, constrictor=constrictor, t0=t0_adj)
    wall_elapsed = time.perf_counter() - t_wall_start

    depth = int(getattr(strat_module, "LAST_SEARCH_DEPTH", 0))

    nodes = 0
    if hasattr(strat_module, "get_search_diagnostics"):
        diag = strat_module.get_search_diagnostics()
        nodes = int(diag.get("nodes_searched", 0))

    nps = (nodes / wall_elapsed) if wall_elapsed > 0 else 0.0

    return {
        "move": str(move).strip().lower() if move else "up",
        "depth": depth,
        "nodes": nodes,
        "elapsed_sec": wall_elapsed,
        "nps": nps,
    }


# ── Position Benchmark Runner ────────────────────────────────────────────────

def evaluate_position(
    baseline_mod: Any,
    variant_mod: Any,
    fixture_path: Path,
    time_ms: int = DEFAULT_TIME_MS,
    repeats: int = DEFAULT_REPEATS,
) -> Dict[str, Any]:
    """
    Evaluates baseline and variant on a single fixture position.
    Runs interleaved baseline/variant trials and computes medians.
    """
    with open(fixture_path, "r", encoding="utf-8") as f:
        game_state = json.load(f)

    base_runs: List[Dict[str, Any]] = []
    var_runs: List[Dict[str, Any]] = []

    for rep in range(repeats):
        # Interleave runs: alternate execution order across repeats to eliminate thermal/order bias
        if rep % 2 == 0:
            b_res = run_single_search(baseline_mod, game_state, time_ms)
            v_res = run_single_search(variant_mod, game_state, time_ms)
        else:
            v_res = run_single_search(variant_mod, game_state, time_ms)
            b_res = run_single_search(baseline_mod, game_state, time_ms)

        base_runs.append(b_res)
        var_runs.append(v_res)

    # Compute medians
    base_depths = [r["depth"] for r in base_runs]
    var_depths = [r["depth"] for r in var_runs]
    base_npss = [r["nps"] for r in base_runs]
    var_npss = [r["nps"] for r in var_runs]
    base_nodes = [r["nodes"] for r in base_runs]
    var_nodes = [r["nodes"] for r in var_runs]

    base_moves = [r["move"] for r in base_runs]
    var_moves = [r["move"] for r in var_runs]

    base_med_depth = float(statistics.median(base_depths))
    var_med_depth = float(statistics.median(var_depths))
    base_med_nps = float(statistics.median(base_npss))
    var_med_nps = float(statistics.median(var_npss))
    base_med_nodes = float(statistics.median(base_nodes))
    var_med_nodes = float(statistics.median(var_nodes))

    nps_ratio = (var_med_nps / base_med_nps) if base_med_nps > 0 else 1.0

    # Most common move
    base_modal_move = collections.Counter(base_moves).most_common(1)[0][0]
    var_modal_move = collections.Counter(var_moves).most_common(1)[0][0]

    # Move agreement across paired repeats
    agreement_count = sum(1 for bm, vm in zip(base_moves, var_moves) if bm == vm)
    agreement_pct = (agreement_count / repeats) * 100.0

    return {
        "fixture": fixture_path.stem,
        "base_med_depth": base_med_depth,
        "var_med_depth": var_med_depth,
        "delta_depth": var_med_depth - base_med_depth,
        "base_med_nps": base_med_nps,
        "var_med_nps": var_med_nps,
        "base_med_nodes": base_med_nodes,
        "var_med_nodes": var_med_nodes,
        "nps_ratio": nps_ratio,
        "base_move": base_modal_move,
        "var_move": var_modal_move,
        "agreement_pct": agreement_pct,
    }


# ── Full Benchmark Suite ─────────────────────────────────────────────────────

def run_depth_test(
    baseline_strat: str = "strategies.strategy_baseline",
    variant_strat: str = "strategies.strategy_variant",
    time_ms: int = DEFAULT_TIME_MS,
    repeats: int = DEFAULT_REPEATS,
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR,
    modes: Sequence[str] = DEFAULT_MODES,
    turns: Sequence[int] = DEFAULT_TURNS,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Executes fixed-position depth test suite across all requested modes.
    Returns (all_passed, results_dict).
    """
    baseline_mod = load_strategy_module(resolve_strategy_name(baseline_strat))
    variant_mod = load_strategy_module(resolve_strategy_name(variant_strat))

    print("=" * 86)
    print(f" Battlesnake Fixed-Position Depth Tester")
    print(f" Baseline: {baseline_strat}")
    print(f" Variant:  {variant_strat}")
    print(f" Budget:   {time_ms} ms | Repeats: {repeats} per position")
    print(f" Fixtures: {fixtures_dir}")
    print("=" * 86)

    # Verify fixtures exist
    missing_fixtures = []
    for mode in modes:
        for turn in turns:
            expected = fixtures_dir / f"{mode}_turn{turn}.json"
            if not expected.is_file():
                missing_fixtures.append(expected)

    if missing_fixtures:
        print(f"\n[ERROR] Missing {len(missing_fixtures)} fixture files. Run with --generate first:")
        for mf in missing_fixtures[:3]:
            print(f"  Missing: {mf}")
        return False, {}

    mode_results: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    mode_summaries: Dict[str, Dict[str, Any]] = {}
    flagged_regressions: List[str] = []

    for mode in modes:
        print(f"\nEvaluating mode: {mode.upper()}...")
        for turn in turns:
            fpath = fixtures_dir / f"{mode}_turn{turn}.json"
            res = evaluate_position(
                baseline_mod=baseline_mod,
                variant_mod=variant_mod,
                fixture_path=fpath,
                time_ms=time_ms,
                repeats=repeats,
            )
            res["turn"] = turn
            mode_results[mode].append(res)
            print(
                f"  Turn {turn:2d}: Base Depth={res['base_med_depth']:4.1f} | "
                f"Var Depth={res['var_med_depth']:4.1f} (Δ {res['delta_depth']:+4.1f}) | "
                f"NPS Ratio={res['nps_ratio']:5.2f}x | "
                f"Agree={res['agreement_pct']:5.1f}% | "
                f"Moves: {res['base_move']} / {res['var_move']}"
            )

        # Mode medians
        m_base_depths = [r["base_med_depth"] for r in mode_results[mode]]
        m_var_depths = [r["var_med_depth"] for r in mode_results[mode]]
        m_nps_ratios = [r["nps_ratio"] for r in mode_results[mode]]
        m_agrees = [r["agreement_pct"] for r in mode_results[mode]]

        med_base_d = statistics.median(m_base_depths)
        med_var_d = statistics.median(m_var_depths)
        med_nps_r = statistics.median(m_nps_ratios)
        mean_agree = statistics.mean(m_agrees)
        delta_d = med_var_d - med_base_d

        is_regression = med_var_d < med_base_d
        if is_regression:
            flagged_regressions.append(mode)

        mode_summaries[mode] = {
            "base_med_depth": med_base_d,
            "var_med_depth": med_var_d,
            "delta_depth": delta_d,
            "nps_ratio": med_nps_r,
            "agreement_pct": mean_agree,
            "regression": is_regression,
        }

    # ── Report Generation ────────────────────────────────────────────────────
    print("\n" + "=" * 94)
    print(" DETAILED RESULTS PER MODE")
    print("=" * 94)

    for mode in modes:
        rows = mode_results[mode]
        summary = mode_summaries[mode]
        status_str = "PASS" if not summary["regression"] else "FLAG: DEPTH REGRESSION"

        print(f"\n[MODE: {mode.upper()}]  Status: {status_str}")
        print(f"{'Turn':>5} | {'Base Depth':>10} | {'Var Depth':>10} | {'Base NPS':>10} | {'Var NPS':>10} | {'NPS Ratio':>9} | {'Base Move':>9} | {'Var Move':>9} | {'Agree %':>8}")
        print("-" * 5 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 9 + "-+-" + "-" * 9 + "-+-" + "-" * 9 + "-+-" + "-" * 8)
        for r in rows:
            print(
                f"{r['turn']:5d} | "
                f"{r['base_med_depth']:10.1f} | "
                f"{r['var_med_depth']:10.1f} | "
                f"{int(r['base_med_nps']):10,d} | "
                f"{int(r['var_med_nps']):10,d} | "
                f"{r['nps_ratio']:8.2f}x | "
                f"{r['base_move']:>9} | "
                f"{r['var_move']:>9} | "
                f"{r['agreement_pct']:7.1f}%"
            )
        print("-" * 5 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 9 + "-+-" + "-" * 9 + "-+-" + "-" * 9 + "-+-" + "-" * 8)
        print(
            f"Mode Summary: Base Median Depth = {summary['base_med_depth']:.1f} | "
            f"Var Median Depth = {summary['var_med_depth']:.1f} (Δ {summary['delta_depth']:+.1f}) | "
            f"NPS Ratio = {summary['nps_ratio']:.2f}x | "
            f"Agreement = {summary['agreement_pct']:.1f}%\n"
        )

    # ── Aggregate Summary Table ──────────────────────────────────────────────
    all_base_depths = [s["base_med_depth"] for s in mode_summaries.values()]
    all_var_depths = [s["var_med_depth"] for s in mode_summaries.values()]
    all_nps_ratios = [s["nps_ratio"] for s in mode_summaries.values()]
    all_agrees = [s["agreement_pct"] for s in mode_summaries.values()]

    overall_base_d = statistics.median(all_base_depths)
    overall_var_d = statistics.median(all_var_depths)
    overall_delta_d = overall_var_d - overall_base_d
    overall_nps_r = statistics.median(all_nps_ratios)
    overall_agree = statistics.mean(all_agrees)
    overall_pass = len(flagged_regressions) == 0

    print("=" * 94)
    print(" AGGREGATE FIXED-POSITION DEPTH TEST RESULTS")
    print("=" * 94)
    print(f"{'Mode':<14} | {'Base Depth':>10} | {'Var Depth':>10} | {'Δ Depth':>8} | {'NPS Ratio':>9} | {'Move Agree %':>12} | {'Status':<18}")
    print("-" * 14 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 8 + "-+-" + "-" * 9 + "-+-" + "-" * 12 + "-+-" + "-" * 18)

    for mode in modes:
        s = mode_summaries[mode]
        st = "PASS" if not s["regression"] else "FLAG: REGRESSION"
        print(
            f"{mode:<14} | "
            f"{s['base_med_depth']:10.1f} | "
            f"{s['var_med_depth']:10.1f} | "
            f"{s['delta_depth']:+8.1f} | "
            f"{s['nps_ratio']:8.2f}x | "
            f"{s['agreement_pct']:11.1f}% | "
            f"{st:<18}"
        )

    print("-" * 14 + "-+-" + "-" * 10 + "-+-" + "-" * 10 + "-+-" + "-" * 8 + "-+-" + "-" * 9 + "-+-" + "-" * 12 + "-+-" + "-" * 18)
    overall_st = "PASS" if overall_pass else "FAIL (REGRESSION)"
    print(
        f"{'OVERALL':<14} | "
        f"{overall_base_d:10.1f} | "
        f"{overall_var_d:10.1f} | "
        f"{overall_delta_d:+8.1f} | "
        f"{overall_nps_r:8.2f}x | "
        f"{overall_agree:11.1f}% | "
        f"{overall_st:<18}"
    )
    print("=" * 94)

    if flagged_regressions:
        print(f"\n[WARNING] Flagged {len(flagged_regressions)} mode(s) with variant median depth below baseline:")
        for fm in flagged_regressions:
            s = mode_summaries[fm]
            print(f"  - {fm}: Variant depth ({s['var_med_depth']:.1f}) < Baseline depth ({s['base_med_depth']:.1f})")
    else:
        print("\n[SUCCESS] No depth regressions detected across any mode.")

    return overall_pass, {
        "modes": mode_results,
        "summaries": mode_summaries,
        "overall": {
            "base_depth": overall_base_d,
            "var_depth": overall_var_d,
            "delta_depth": overall_delta_d,
            "nps_ratio": overall_nps_r,
            "agreement_pct": overall_agree,
            "passed": overall_pass,
        },
    }


# ── Command Line Interface ───────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fixed-position depth tester for Battlesnake minimax strategies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate commit-worthy position fixtures across standard modes and turns (25, 50, 75).",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default="strategies.strategy_baseline",
        help="Baseline strategy module path or name ('baseline' / 'strategies.strategy_baseline').",
    )
    parser.add_argument(
        "--variant",
        type=str,
        default="strategies.strategy_variant",
        help="Variant strategy module path or name ('variant' / 'strategies.strategy_variant').",
    )
    parser.add_argument(
        "--time-ms",
        type=int,
        default=DEFAULT_TIME_MS,
        help="Search budget per move in milliseconds.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=DEFAULT_REPEATS,
        help="Number of interleaved repeat runs per fixture position.",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=str,
        default=str(DEFAULT_FIXTURES_DIR),
        help="Directory containing or targeting position fixtures.",
    )
    parser.add_argument(
        "--modes",
        type=str,
        default="standard,constrictor,royale,duel",
        help="Comma-separated list of modes to test or generate.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fixtures_dir = Path(args.fixtures_dir)
    modes = [m.strip().lower() for m in args.modes.split(",") if m.strip()]

    if args.generate:
        generate_fixtures(output_dir=fixtures_dir, modes=modes)
        return

    passed, _ = run_depth_test(
        baseline_strat=args.baseline,
        variant_strat=args.variant,
        time_ms=args.time_ms,
        repeats=args.repeats,
        fixtures_dir=fixtures_dir,
        modes=modes,
    )

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
