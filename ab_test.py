#!/usr/bin/env python3
"""
ab_test.py — Automated A/B Testing Harness for Battlesnake Strategies

Combines deterministic safety checks, official Battlesnake binary smoke-testing,
and high-speed in-process parallel simulation streaming live to the web dashboard.

Workflow:
1. Deterministic Safety & Binary Verification:
   - Runs full tactical unit test suite (tests/test_strategy.py).
   - Launches strategy servers and executes a 1-game smoke test with the official
     Battlesnake CLI binary to verify Flask routing, JSON contracts, and runtime health.
2. Fast In-Process A/B Simulation:
   - Evaluates moves in-process using simulator.py and multiprocessing across CPU cores.
   - Enforces 500ms Battlesnake move timeout ceiling with exact timing.
   - Streams live match updates, standings, and win rates to http://localhost:8888.
3. Automated Checkpoints & Early Stopping:
   - Checks every 100 games after 200 games.
   - Early stops at >60.0% win share (KEEP) or <40.0% win share (REVERT).
4. Decision Policy:
   - Bug fix (desc contains 'fix'/'bug'/'blunder'): KEEP if win share >= 50.0%.
   - General improvement: KEEP if win share >= 51.5%.

Usage:
    python ab_test.py [--desc "Description of your change"]
"""

import argparse
import csv
from datetime import datetime
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import random
import re
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Ensure harness and project root are on sys.path
HARNESS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = HARNESS_DIR.parent if (HARNESS_DIR.parent / "strategies").exists() else HARNESS_DIR
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import importlib
import importlib.util

from ab_dashboard import DashboardClient, ensure_dashboard_running
from simulator import simulate_turn

if (HARNESS_DIR.parent / "strategies").exists():
    DEFAULT_BASELINE_MODULE: str = "strategies.strategy_baseline"
    DEFAULT_VARIANT_MODULE: str = "strategies.strategy_variant"
else:
    DEFAULT_BASELINE_MODULE: str = "example_snakes.baseline"
    DEFAULT_VARIANT_MODULE: str = "example_snakes.variant"

_strategy_baseline: Any = None
_strategy_variant: Any = None
_baseline_module_name: str = DEFAULT_BASELINE_MODULE
_variant_module_name: str = DEFAULT_VARIANT_MODULE


def load_strategy_module(module_or_path: str) -> Any:
    """
    Dynamically loads a Battlesnake strategy module from a dotted module path
    or a Python file path.
    """
    path_candidate = Path(module_or_path)
    if module_or_path.endswith(".py") or path_candidate.is_file():
        mod_name = path_candidate.stem
        spec = importlib.util.spec_from_file_location(mod_name, str(path_candidate.resolve()))
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load specification for strategy file: {module_or_path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(module_or_path)

    # Ensure required interface is present:
    # 1. INFO: dict
    if not hasattr(mod, "INFO") or not isinstance(getattr(mod, "INFO"), dict):
        setattr(mod, "INFO", {"apiversion": "1", "author": "Battlesnake", "color": "#888888", "head": "default", "tail": "default"})

    # 2. choose_move(game_state, wrap=False, constrictor=False) -> str
    if not hasattr(mod, "choose_move"):
        if hasattr(mod, "move"):
            orig_move = getattr(mod, "move")
            def wrapped_choose_move(game_state: Dict[str, Any], wrap: bool = False, constrictor: bool = False) -> str:
                res = orig_move(game_state)
                if isinstance(res, dict):
                    return res.get("move", "up")
                return str(res)
            setattr(mod, "choose_move", wrapped_choose_move)
        else:
            raise AttributeError(f"Strategy module '{module_or_path}' must define 'choose_move(game_state, wrap, constrictor)' or 'move(game_state)'")

    return mod


def init_strategies(baseline_module: str, variant_module: str) -> None:
    global _strategy_baseline, _strategy_variant, _baseline_module_name, _variant_module_name
    _baseline_module_name = baseline_module
    _variant_module_name = variant_module
    _strategy_baseline = load_strategy_module(baseline_module)
    _strategy_variant = load_strategy_module(variant_module)


def _init_worker(baseline_module: str, variant_module: str) -> None:
    init_strategies(baseline_module, variant_module)


# Pre-initialize defaults so imported helpers work unconditionally
try:
    init_strategies(_baseline_module_name, _variant_module_name)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Project Configuration Constants
# ---------------------------------------------------------------------------
THREADS = 8                 # Fixed simulation worker process count (Updated per user instruction)
MIN_GAMES = 30             # Minimum games before evaluating dynamic statistical early stop (rethought from fixed 50)
MAX_GAMES = 200            # Maximum games to play before hard stop
CHECK_INTERVAL = 1         # Evaluate statistical confidence dynamically on every completed game (>= MIN_GAMES)
Z_CRIT_EARLY = 2.80        # Z-score cutoff for early stop (>99.5% confidence, p < 0.0026)
Z_CRIT_REGRESSION = 1.645  # Z-score cutoff at MAX_GAMES for regression detection (95% confidence, p < 0.05)
Z_CRIT_KEEP = 1.645        # one-sided 95% significance required for KEEP at MAX_GAMES
EARLY_STOP_HIGH = 60.0     # Base nominal upper cutoff in 1v1
EARLY_STOP_LOW = 40.0      # Base nominal lower cutoff in 1v1
EARLY_STOP_HIGH_4P = 35.0  # Base nominal upper cutoff in 4-player
EARLY_STOP_LOW_4P = 18.0   # Base nominal lower cutoff in 4-player
THRESHOLD_IMPROVEMENT = 51.5  # Win share threshold (%) required for general improvements in 1v1
THRESHOLD_BUG_FIX = 50.0      # Win share threshold (%) required for bug fixes in 1v1
THRESHOLD_4P_IMPROVEMENT = 25.5 # Win share threshold (%) required for general improvements in 4p
THRESHOLD_4P_BUG_FIX = 25.0     # Win share threshold (%) required for bug fixes in 4p
MOVE_TIMEOUT_SEC = 0.500   # 500ms official Battlesnake turn time limit


def get_statistical_thresholds(total_played: int, num_snakes: int, z_crit: float = Z_CRIT_EARLY) -> Tuple[float, float]:
    """
    Computes dynamic continuous early-stop thresholds based on binomial standard error at sample size N.
    High Stop: min(100.0, p0 + z_crit * SE)
    Low Stop: max(0.0, p0 - z_crit * SE)
    """
    p0 = 50.0 if num_snakes == 2 else 25.0
    p0_frac = p0 / 100.0
    if total_played <= 0:
        return 100.0, 0.0
    se = math.sqrt(p0_frac * (1.0 - p0_frac) / total_played) * 100.0
    high_stop = min(100.0, p0 + z_crit * se)
    low_stop = max(0.0, p0 - z_crit * se)
    return high_stop, low_stop


def compute_z_score(win_share: float, total_played: int, num_snakes: int) -> Tuple[float, float]:
    """
    Computes standard error and z-score vs parity.
    Returns: (z_score, se_percentage)
    """
    p0 = 50.0 if num_snakes == 2 else 25.0
    p0_frac = p0 / 100.0
    if total_played <= 0:
        return 0.0, 0.0
    se = math.sqrt(p0_frac * (1.0 - p0_frac) / total_played) * 100.0
    z = (win_share - p0) / se if se > 0 else 0.0
    return z, se


def classify_max_games_outcome(
    var_win_share: float,
    z_final: float,
    keep_threshold: float,
    num_snakes: int,
    max_opp_win_share: float = 0.0,
    se: Optional[float] = None,
) -> Tuple[str, bool, str]:
    """
    Evaluates the final outcome when MAX_GAMES are played without early stop.

    Outcomes:
    - KEEP (Green): win share >= keep_threshold AND z_final >= Z_CRIT_KEEP (one-sided p < 0.05).
      In multiplayer (num_snakes > 2), additionally requires var_win_share >= max_opp_win_share (top snake).
    - REVERT (Red): z_final <= -Z_CRIT_REGRESSION (statistically significantly below parity).
    - PARITY (Yellow): all other cases within the parity confidence band.

    Returns:
        (decision, passed, reason)
    """
    if num_snakes == 2:
        is_improvement = (var_win_share >= keep_threshold and z_final >= Z_CRIT_KEEP)
    else:
        is_improvement = (
            var_win_share >= keep_threshold
            and var_win_share >= max_opp_win_share
            and z_final >= Z_CRIT_KEEP
        )

    is_regression = (z_final <= -Z_CRIT_REGRESSION)

    if is_improvement:
        passed = True
        decision = "KEEP"
        stop_reason = (
            f"Variant achieved win share {var_win_share:.1f}% >= target {keep_threshold:.1f}% "
            f"(z={z_final:+.2f} vs parity). Confident improvement."
        )
    elif is_regression:
        passed = False
        decision = "REVERT"
        stop_reason = (
            f"Variant win share {var_win_share:.1f}% is statistically significantly below parity "
            f"(z={z_final:+.2f} <= -{Z_CRIT_REGRESSION:.2f}, p<0.05). Confident regression."
        )
    else:
        passed = True
        decision = "PARITY"
        se_str = f", SE={se:.1f}%" if se is not None else ""
        stop_reason = (
            f"Variant achieved win share {var_win_share:.1f}% within parity confidence band "
            f"(z={z_final:+.2f}{se_str}). Confident no regression."
        )

    return decision, passed, stop_reason


def get_tiered_thresholds(total_played: int, num_snakes: int) -> Tuple[float, float]:
    """
    Backwards-compatible wrapper calling get_statistical_thresholds with z_crit=Z_CRIT_EARLY.
    """
    return get_statistical_thresholds(total_played, num_snakes, z_crit=Z_CRIT_EARLY)

# Smoke Test Ports & Services
PORT_BASELINE = 8000
PORT_VARIANT = 8001
DASHBOARD_PORT = 8888

STRATEGY_BASELINE = "baseline"
STRATEGY_VARIANT = "variant"

MODES_MATRIX = [
    {
        "mode": "constrictor",
        "name": "Constrictor 4P",
        "num_snakes": 4,
        "target_share": 25.0,
        "high_stop": EARLY_STOP_HIGH_4P,
        "low_stop": EARLY_STOP_LOW_4P,
    },
    {
        "mode": "royale",
        "name": "Royale 4P",
        "num_snakes": 4,
        "target_share": 25.0,
        "high_stop": EARLY_STOP_HIGH_4P,
        "low_stop": EARLY_STOP_LOW_4P,
    },
    {
        "mode": "duel",
        "name": "Duel 1v1",
        "num_snakes": 2,
        "target_share": 50.0,
        "high_stop": EARLY_STOP_HIGH,
        "low_stop": EARLY_STOP_LOW,
    },
    {
        "mode": "standard",
        "name": "Standard 4P",
        "num_snakes": 4,
        "target_share": 25.0,
        "high_stop": EARLY_STOP_HIGH_4P,
        "low_stop": EARLY_STOP_LOW_4P,
    },
]

_WINNER_RE = re.compile(r"Game completed after (\d+) turns\. (.+?) was the winner\.")
_DRAW_RE = re.compile(r"Game completed after (\d+) turns\..* tied\.")
_COMPLETED_RE = re.compile(r"Game completed after (\d+) turns\.")
_CONN_ERR_RE = re.compile(r"(connection refused|Error getting snake metadata|dial tcp)", re.I)


# ── In-Process Simulation Engine ─────────────────────────────────────────────

def _run_timed_move(
    move_fn: Any,
    game_state: Dict[str, Any],
    wrap: bool = False,
    constrictor: bool = False,
) -> Tuple[Optional[str], float]:
    """Executes a strategy move function and tracks elapsed time."""
    t0 = time.perf_counter()
    chosen_move = move_fn(game_state, wrap=wrap, constrictor=constrictor)
    elapsed = time.perf_counter() - t0
    return chosen_move, elapsed


def _setup_initial_board(
    seed: int,
    game_num: int = 1,
    width: int = 11,
    height: int = 11,
    num_snakes: int = 2,
    mode: str = "standard",
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], random.Random]:
    """Initializes a standard 11x11 board matching official spawn rules for 2 or 4 snakes with balanced rotation."""
    rng = random.Random(seed)
    mn, md, mx = 1, (width - 1) // 2, width - 2
    corners = [(mn, mn), (mn, mx), (mx, mn), (mx, mx)]
    cardinals = [(mn, md), (md, mn), (md, mx), (mx, md)]

    if num_snakes == 4:
        # Perfectly balanced spawn and seating rotation:
        # Alternate between corners and cardinals every 4 games
        use_corners = ((game_num - 1) // 4) % 2 == 0
        base_spawns = list(corners) if use_corners else list(cardinals)

        # Rotate snake slot assignment cyclically: variant takes slot 0, 1, 2, 3 in turn
        # and receives base_spawns[slot] to visit every physical spawn position uniformly
        var_slot = (game_num - 1) % 4
        snake_defs: List[Optional[Dict[str, str]]] = [None] * 4
        snake_defs[var_slot] = {"id": STRATEGY_VARIANT, "name": STRATEGY_VARIANT}
        baselines = [
            {"id": f"{STRATEGY_BASELINE}_1", "name": f"{STRATEGY_BASELINE}_1"},
            {"id": f"{STRATEGY_BASELINE}_2", "name": f"{STRATEGY_BASELINE}_2"},
            {"id": f"{STRATEGY_BASELINE}_3", "name": f"{STRATEGY_BASELINE}_3"},
        ]
        b_idx = 0
        for slot in range(4):
            if snake_defs[slot] is None:
                snake_defs[slot] = baselines[b_idx]
                b_idx += 1

        snakes = [
            {
                "id": sdef["id"],
                "name": sdef["name"],
                "health": 100,
                "body": [{"x": pt[0], "y": pt[1]}] * 3,
                "head": {"x": pt[0], "y": pt[1]},
                "length": 3,
            }
            for sdef, pt in zip(snake_defs, base_spawns)
        ]
    else:
        # 2-player mode: alternate corners and cardinals every 4 games
        # with mirrored pairwise swaps across opposing positions
        use_corners = ((game_num - 1) // 4) % 2 == 0
        pool = list(corners) if use_corners else list(cardinals)
        step = (game_num - 1) % 4
        pairs = [(pool[0], pool[3]), (pool[3], pool[0]), (pool[1], pool[2]), (pool[2], pool[1])]
        p_var, p_base = pairs[step]

        if (game_num - 1) % 2 == 0:
            s_first, s_second = (STRATEGY_BASELINE, p_base), (STRATEGY_VARIANT, p_var)
        else:
            s_first, s_second = (STRATEGY_VARIANT, p_var), (STRATEGY_BASELINE, p_base)

        snakes = [
            {
                "id": s_first[0],
                "name": s_first[0],
                "health": 100,
                "body": [{"x": s_first[1][0], "y": s_first[1][1]}] * 3,
                "head": {"x": s_first[1][0], "y": s_first[1][1]},
                "length": 3,
            },
            {
                "id": s_second[0],
                "name": s_second[0],
                "health": 100,
                "body": [{"x": s_second[1][0], "y": s_second[1][1]}] * 3,
                "head": {"x": s_second[1][0], "y": s_second[1][1]},
                "length": 3,
            },
        ]

    center = (md, (height - 1) // 2)
    if mode == "constrictor":
        food = []
    else:
        food = [{"x": center[0], "y": center[1]}]
        for sn in snakes:
            hx, hy = sn["head"]["x"], sn["head"]["y"]
            candidates = [(hx - 1, hy - 1), (hx - 1, hy + 1), (hx + 1, hy - 1), (hx + 1, hy + 1)]
            valid = []
            for c in candidates:
                if not (0 <= c[0] < width and 0 <= c[1] < height):
                    continue
                if c == center:
                    continue
                if (c[0] in (0, width - 1)) and (c[1] in (0, height - 1)):
                    continue
                is_away = False
                if c[0] < hx < center[0] or center[0] < hx < c[0]:
                    is_away = True
                elif c[1] < hy < center[1] or center[1] < hy < c[1]:
                    is_away = True
                if is_away and not any(f["x"] == c[0] and f["y"] == c[1] for f in food):
                    valid.append(c)
            if valid:
                chosen = rng.choice(valid)
                food.append({"x": chosen[0], "y": chosen[1]})

    board = {
        "width": width,
        "height": height,
        "food": food,
        "hazards": [],
        "snakes": snakes,
    }
    return board, snakes, rng


def _build_game_state(
    current_snake: Dict[str, Any],
    active_snakes: List[Dict[str, Any]],
    current_food: List[Dict[str, Any]],
    board_width: int,
    board_height: int,
    turn: int,
    game_id: str,
    mode: str = "standard",
    hazards: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Constructs isolated game_state dictionary matching the Battlesnake API."""
    if hazards is None:
        hazards = []
    return {
        "game": {
            "id": game_id,
            "ruleset": {
                "name": mode,
                "version": "v1.2.3",
                "settings": {
                    "foodSpawnChance": 15 if mode != "constrictor" else 0,
                    "minimumFood": 1 if mode != "constrictor" else 0,
                    "hazardDamagePerTurn": 14,
                    "royale": {
                        "shrinkEveryNTurns": 25,
                    },
                },
            },
            "map": "standard",
            "timeout": 500,
            "source": "custom",
        },
        "turn": turn,
        "board": {
            "width": board_width,
            "height": board_height,
            "snakes": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "health": s["health"],
                    "body": [{"x": pt["x"], "y": pt["y"]} for pt in s["body"]],
                    "head": {"x": s["head"]["x"], "y": s["head"]["y"]},
                    "length": s["length"],
                }
                for s in active_snakes
            ],
            "food": [{"x": f["x"], "y": f["y"]} for f in current_food],
            "hazards": [{"x": hz["x"], "y": hz["y"]} for hz in hazards],
        },
        "you": {
            "id": current_snake["id"],
            "name": current_snake["name"],
            "health": current_snake["health"],
            "body": [{"x": pt["x"], "y": pt["y"]} for pt in current_snake["body"]],
            "head": {"x": current_snake["head"]["x"], "y": current_snake["head"]["y"]},
            "length": current_snake["length"],
        },
    }


def simulate_single_game(
    game_num: int,
    seed: int,
    max_turns: int = 1000,
    mode: str = "duel",
) -> Dict[str, Any]:
    """Runs a complete in-process game simulation between baseline and variant."""
    t_game_start = time.perf_counter()
    num_snakes = 4 if mode in ("standard", "ffa", "royale", "constrictor") else 2
    wrap = mode in ("wrapped", "wrapped-constrictor", "spicy-meteors")
    constrictor = mode in ("constrictor", "wrapped-constrictor")

    board, snakes, rng = _setup_initial_board(seed, game_num=game_num, num_snakes=num_snakes, mode=mode)
    turn = 0
    winner = None
    tied_snakes: List[str] = []
    base_depths: List[int] = []
    var_depths: List[int] = []

    while turn < max_turns:
        if len(snakes) == 0:
            winner = "DRAW"
            break
        elif len(snakes) == 1:
            winner = snakes[0]["id"]
            break

        # In Royale mode, concentric hazard ring expands every 25 turns inward
        if mode == "royale":
            sauce_depth = min(turn // 25, (board["width"] + 1) // 2)
            if sauce_depth > 0:
                w, h = board["width"], board["height"]
                board["hazards"] = [
                    {"x": x, "y": y}
                    for x in range(w)
                    for y in range(h)
                    if min(x, w - 1 - x, y, h - 1 - y) < sauce_depth
                ]
            else:
                board["hazards"] = []

        moves: Dict[str, str] = {}
        last_alive_snakes = [s["id"] for s in snakes]

        # Rotate evaluation and board snake ordering turn-by-turn to prevent structural seating bias
        turn_offset = turn % len(snakes)
        ordered_snakes = snakes[turn_offset:] + snakes[:turn_offset]

        for sn in ordered_snakes:
            s_id = sn["id"]
            strat_module = _strategy_variant if s_id == STRATEGY_VARIANT else _strategy_baseline
            if strat_module is None:
                init_strategies(_baseline_module_name, _variant_module_name)
                strat_module = _strategy_variant if s_id == STRATEGY_VARIANT else _strategy_baseline
            move_fn = strat_module.choose_move
            game_state = _build_game_state(
                current_snake=sn,
                active_snakes=ordered_snakes,
                current_food=board["food"],
                board_width=board["width"],
                board_height=board["height"],
                turn=turn,
                game_id=f"game_{game_num}_{seed}",
                mode=mode,
                hazards=board.get("hazards", []),
            )

            chosen_move, elapsed = _run_timed_move(move_fn, game_state, wrap=wrap, constrictor=constrictor)
            moves[s_id] = chosen_move if (chosen_move in ("up", "down", "left", "right")) else "up"
            d = getattr(strat_module, "LAST_SEARCH_DEPTH", 0)
            if d > 0:
                if s_id == STRATEGY_VARIANT:
                    var_depths.append(d)
                else:
                    base_depths.append(d)

        board, snakes = simulate_turn(board, snakes, moves, wrap=wrap, constrictor=constrictor, hazard_damage=14)
        turn += 1

        if len(snakes) == 1:
            winner = snakes[0]["id"]
            if winner.startswith(f"{STRATEGY_BASELINE}_"):
                winner = STRATEGY_BASELINE
            break
        elif len(snakes) == 0:
            winner = "DRAW"
            tied_snakes = list(last_alive_snakes)
            break

        # Early exit optimization: In multiplayer games (> 2 snakes), if the variant snake is eliminated
        # while 2 or more baseline snakes remain alive, the variant has definitively lost.
        # Stop simulation early to eliminate redundant minimax self-play compute between identical baselines.
        if num_snakes > 2:
            var_alive = any(s["id"] == STRATEGY_VARIANT for s in snakes)
            if not var_alive:
                winner = STRATEGY_BASELINE
                break

        # Food spawn (only if not constrictor)
        if not constrictor:
            occupied = {(seg["x"], seg["y"]) for sn in snakes for seg in sn["body"]}
            occupied.update((f["x"], f["y"]) for f in board["food"])
            unoccupied = [
                (x, y)
                for x in range(board["width"])
                for y in range(board["height"])
                if (x, y) not in occupied
            ]
            num_food = len(board["food"])
            food_needed = 0
            if num_food < 1:
                food_needed = 1 - num_food
            elif rng.randint(1, 100) <= 15:
                food_needed = 1

            if food_needed > 0 and unoccupied:
                rng.shuffle(unoccupied)
                for p in unoccupied[:food_needed]:
                    board["food"].append({"x": p[0], "y": p[1]})
        else:
            board["food"] = []

    if winner is None:
        winner = "DRAW"
        tied_snakes = [s["id"] for s in snakes]

    duration_ms = (time.perf_counter() - t_game_start) * 1000.0
    return {
        "game": game_num,
        "seed": seed,
        "winner": winner,
        "tied_snakes": tied_snakes,
        "turns": turn,
        "duration_ms": round(duration_ms, 2),
        "base_avg_depth": round(sum(base_depths) / len(base_depths), 2) if base_depths else 0.0,
        "var_avg_depth": round(sum(var_depths) / len(var_depths), 2) if var_depths else 0.0,
    }


def _worker_wrapper(args: Tuple) -> Dict[str, Any]:
    if len(args) == 3:
        game_num, seed, mode = args
    else:
        game_num, seed = args
        mode = "standard"
    return simulate_single_game(game_num, seed, mode=mode)


# ── Battlesnake Binary Smoke Test ───────────────────────────────────────────

def find_battlesnake_cli() -> str:
    cli = os.environ.get("BATTLESNAKE_CLI", str(Path.home() / "go" / "bin" / "battlesnake"))
    if not Path(cli).exists():
        cli_in_path = subprocess.run(["which", "battlesnake"], capture_output=True, text=True).stdout.strip()
        if cli_in_path:
            return cli_in_path
    return cli


def find_python_interpreter() -> str:
    venv_python = Path(__file__).parent / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def launch_server(python_bin: str, strategy: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "STRATEGY_NAME": strategy, "PORT": str(port)}
    return subprocess.Popen(
        [python_bin, "main.py"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_server(url: str, timeout: float = 10.0, interval: float = 0.25) -> bool:
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except Exception:
            time.sleep(interval)
    return False


def kill_servers(*procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
    time.sleep(0.3)
    for proc in procs:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def run_binary_smoke_test(
    cli_path: str,
    python_bin: str,
    port_base: int,
    port_var: int,
    mode: str = "standard",
) -> Tuple[bool, str]:
    """Runs a 1-game smoke test with the official Battlesnake CLI to verify full HTTP stack."""
    url_base = f"http://127.0.0.1:{port_base}"
    url_var = f"http://127.0.0.1:{port_var}"

    if not Path(cli_path).exists():
        return False, f"Battlesnake CLI binary not found at '{cli_path}'"

    proc_base = proc_var = None
    try:
        proc_base = launch_server(python_bin, STRATEGY_BASELINE, port_base)
        proc_var = launch_server(python_bin, STRATEGY_VARIANT, port_var)

        if not wait_for_server(url_base, timeout=8.0):
            return False, f"Baseline server failed to respond at {url_base}"
        if not wait_for_server(url_var, timeout=8.0):
            return False, f"Variant server failed to respond at {url_var}"

        cmd = [
            cli_path, "play",
            "-W", "7", "-H", "7",
            "-u", url_base, "-n", STRATEGY_BASELINE,
            "-u", url_var, "-n", STRATEGY_VARIANT,
            "-r", "12345",
        ]
        if mode != "standard":
            cmd.extend(["-g", mode])

        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = res.stdout + res.stderr
        if res.returncode != 0 or _CONN_ERR_RE.search(output):
            return False, f"CLI smoke test failed: {output.strip()}"
        return True, "OK"
    except Exception as e:
        return False, f"CLI smoke test exception: {e}"
    finally:
        kill_servers(proc_base, proc_var)


# ── History & Reporting ──────────────────────────────────────────────────────

def export_history_csv(history: list, csv_path: Path) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date & Time", "Tested Change", "Baseline", "Draws", "Variant", "Decision"])
        for run in history:
            timestamp = run.get("timestamp", "-")
            desc = run.get("experiment_desc") or run.get("description") or "Unspecified Experiment"
            total = run.get("total_games", 0)

            base = run.get("baseline", {})
            b_wins = base.get("wins", 0) if base else 0
            b_rate = base.get("rate") if base else None
            if b_rate is None and total > 0:
                b_rate = round((b_wins / total) * 100.0, 1)
            b_str = f"{b_rate:.1f}% ({b_wins}W)" if b_rate is not None else "-"

            var = run.get("variant", {})
            v_wins = var.get("wins", 0) if var else 0
            v_rate = var.get("rate") if var else None
            if v_rate is None and total > 0:
                v_rate = round((v_wins / total) * 100.0, 1)
            v_str = f"{v_rate:.1f}% ({v_wins}W)" if v_rate is not None else "-"

            d_count = None
            if "draws" in run and isinstance(run["draws"], (int, float)):
                d_count = run["draws"]
            elif base and isinstance(base.get("draws"), (int, float)):
                d_count = base["draws"]
            elif "draws" in run and isinstance(run["draws"], dict) and isinstance(run["draws"].get("count"), (int, float)):
                d_count = run["draws"]["count"]

            if d_count is not None:
                d_rate = round((d_count / total * 100.0), 1) if total > 0 else 0.0
                d_cnt_str = f"{d_count:.2f}".rstrip('0').rstrip('.') if isinstance(d_count, float) else f"{d_count}"
                draw_str = f"{d_rate:.1f}% ({d_cnt_str}D)"
            else:
                draw_str = "-"

            rec = run.get("recommendation", "PENDING")
            writer.writerow([timestamp, desc, b_str, draw_str, v_str, rec])


def save_run_history(
    strategy_baseline: str,
    strategy_variant: str,
    total_games: int,
    results: dict,
    stop_type: str,
    stop_reason: str,
    recommendation: str,
    experiment_desc: str = "Unspecified Experiment",
    matrix_results: Optional[List[dict]] = None,
    game_mode: str = "standard",
) -> Path:
    out_dir = PROJECT_ROOT / "ab_results"
    out_dir.mkdir(exist_ok=True)
    history_file = out_dir / "ab_history.json"
    history_csv_file = out_dir / "ab_history.csv"

    w_base = round(results[strategy_baseline]["wins"], 2) if isinstance(results[strategy_baseline]["wins"], float) else results[strategy_baseline]["wins"]
    l_base = round(results[strategy_baseline]["losses"], 2) if isinstance(results[strategy_baseline]["losses"], float) else results[strategy_baseline]["losses"]
    d_base = round(results[strategy_baseline]["draws"], 2) if isinstance(results[strategy_baseline]["draws"], float) else results[strategy_baseline]["draws"]
    rate_base = round((w_base / total_games * 100.0) if total_games > 0 else 0.0, 1)

    w_var = round(results[strategy_variant]["wins"], 2) if isinstance(results[strategy_variant]["wins"], float) else results[strategy_variant]["wins"]
    l_var = round(results[strategy_variant]["losses"], 2) if isinstance(results[strategy_variant]["losses"], float) else results[strategy_variant]["losses"]
    d_var = round(results[strategy_variant]["draws"], 2) if isinstance(results[strategy_variant]["draws"], float) else results[strategy_variant]["draws"]
    rate_var = round((w_var / total_games * 100.0) if total_games > 0 else 0.0, 1)

    history = []
    if history_file.exists():
        try:
            with history_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    history = data
        except Exception:
            history = []

    run_entry = {
        "run_id": len(history) + 1,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "experiment_desc": experiment_desc,
        "strategy_baseline": strategy_baseline,
        "strategy_variant": strategy_variant,
        "game_mode": game_mode,
        "total_games": total_games,
        "baseline": {"wins": w_base, "losses": l_base, "draws": d_base, "rate": rate_base},
        "variant": {"wins": w_var, "losses": l_var, "draws": d_var, "rate": rate_var},
        "recommendation": recommendation,
        "stop_type": stop_type,
        "stop_reason": stop_reason,
    }
    if matrix_results is not None:
        sanitized_matrix = []
        for mr in matrix_results:
            mr_copy = dict(mr)
            if "results" in mr_copy and isinstance(mr_copy["results"], dict):
                mr_copy["results"] = {
                    s: {
                        k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in s_res.items()
                    } if isinstance(s_res, dict) else s_res
                    for s, s_res in mr_copy["results"].items()
                }
            sanitized_matrix.append(mr_copy)
        run_entry["matrix_results"] = sanitized_matrix

    history.append(run_entry)
    history = history[-100:]

    with history_file.open("w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, default=str)

    export_history_csv(history, history_csv_file)
    return history_file


def print_summary(
    total_games: int,
    results: dict,
    stop_type: str,
    stop_reason: str,
    history_path: Path,
    experiment_desc: str = "Unspecified Experiment",
    recommendation: str = None,
    keep_threshold: float = THRESHOLD_IMPROVEMENT,
    total_wall_sec: float = 0.0,
    game_mode: str = "standard",
    num_snakes: int = 2,
    snakes_stats: Optional[Dict[str, dict]] = None,
) -> None:
    w_base = results[STRATEGY_BASELINE]["wins"]
    l_base = results[STRATEGY_BASELINE]["losses"]
    d_base = results[STRATEGY_BASELINE]["draws"]
    rate_base = (w_base / total_games * 100.0) if total_games > 0 else 0.0

    w_var = results[STRATEGY_VARIANT]["wins"]
    l_var = results[STRATEGY_VARIANT]["losses"]
    d_var = results[STRATEGY_VARIANT]["draws"]
    rate_var = (w_var / total_games * 100.0) if total_games > 0 else 0.0

    draw_share = 0.25 if num_snakes == 4 else 0.5
    var_win_share = ((w_var + draw_share * d_var) / total_games * 100.0) if total_games > 0 else (25.0 if num_snakes == 4 else 50.0)

    if recommendation is None:
        z_score, _ = compute_z_score(var_win_share, total_games, num_snakes)
        max_opp = 0.0
        if num_snakes == 4 and snakes_stats:
            max_opp = max(
                ((s["wins"] + 0.25 * s["draws"]) / total_games * 100.0)
                for s_id, s in snakes_stats.items()
                if s_id != STRATEGY_VARIANT
            ) if total_games > 0 else 25.0
        recommendation, _, _ = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z_score,
            keep_threshold=keep_threshold,
            num_snakes=num_snakes,
            max_opp_win_share=max_opp,
        )

    throughput = (total_games / total_wall_sec) if total_wall_sec > 0 else 0.0

    sep = "=" * 76
    sub_sep = "-" * 76

    print()
    print(sep)
    print("                        A/B TEST SUMMARY")
    print(sep)
    print(f"Tested Change:          {experiment_desc}")
    print(f"Game Mode:              {game_mode.upper()} ({num_snakes} Snakes)")
    print(f"Total Games Played:     {total_games}")
    print(f"Total Elapsed Time:     {total_wall_sec:.2f}s ({throughput:.3f} games/sec)")
    print(f"Stop Decision:          {stop_type}")
    print(f"Stop Reason:            {stop_reason}")
    print(f"Required Win Share:     >={keep_threshold:.1f}%")
    print(f"Final Win Share:        {var_win_share:.1f}% (Variant: {rate_var:.1f}%, Baseline: {rate_base:.1f}%)")
    print()
    if num_snakes == 4 and snakes_stats:
        if len(snakes_stats) == 2:
            print("4-Player Standings Performance (Variant vs 3-Snake Baseline Team):")
            print(sub_sep)
            print(f"{'Entity':<24} {'Wins':>8} {'Losses':>8} {'Draws':>8} {'Win Rate':>12} {'Win Share':>12} {'Target':>15}")
            print(sub_sep)
            for s_id, s_data in sorted(snakes_stats.items(), key=lambda x: x[1]["wins"], reverse=True):
                sw = s_data["wins"]
                sl = s_data["losses"]
                sd = s_data["draws"]
                srt = (sw / total_games * 100.0) if total_games > 0 else 0.0
                is_var = (s_id == STRATEGY_VARIANT)
                sws = ((sw + (0.25 if is_var else 0.75) * sd) / total_games * 100.0) if total_games > 0 else (25.0 if is_var else 75.0)
                target_str = "25.0% (1 Snake)" if is_var else "75.0% (3 Snakes)"
                sname = f"{s_id.upper()} (VARIANT)" if is_var else f"{s_id.upper()} (3 BASELINES)"
                print(f"{sname:<24} {sw:>8} {sl:>8} {sd:>8} {srt:>11.1f}% {sws:>11.1f}% {target_str:>15}")
            print(sub_sep)
        else:
            print("4-Player Standings Performance:")
            print(sub_sep)
            print(f"{'Snake':<18} {'Wins':>8} {'Losses':>8} {'Draws':>8} {'Win Rate':>12} {'Win Share':>12}")
            print(sub_sep)
            for s_id, s_data in sorted(snakes_stats.items(), key=lambda x: x[1]["wins"] + 0.25 * x[1]["draws"], reverse=True):
                sw = s_data["wins"]
                sl = s_data["losses"]
                sd = s_data["draws"]
                srt = (sw / total_games * 100.0) if total_games > 0 else 0.0
                sws = ((sw + 0.25 * sd) / total_games * 100.0) if total_games > 0 else 25.0
                sname = s_id.upper().replace("_", " ")
                print(f"{sname:<18} {sw:>8} {sl:>8} {sd:>8} {srt:>11.1f}% {sws:>11.1f}%")
            print(sub_sep)
    else:
        print("Strategy Performance:")
        print(sub_sep)
        print(f"{'Strategy':<15} {'Wins':>8} {'Losses':>8} {'Draws':>8} {'Win Rate':>12}")
        print(sub_sep)
        print(f"{STRATEGY_BASELINE:<15} {w_base:>8} {l_base:>8} {d_base:>8} {rate_base:>11.1f}%")
        print(f"{STRATEGY_VARIANT:<15} {w_var:>8} {l_var:>8} {d_var:>8} {rate_var:>11.1f}%")
        print(sub_sep)
    print()
    print(f"Final Recommendation:   {recommendation} (Win Share: {var_win_share:.1f}% vs Target: {keep_threshold:.1f}%)")
    print(f"History File:           {history_path}")
    print(sep)
    print()


def print_matrix_summary(
    matrix_results: List[Dict[str, Any]],
    overall_recommendation: str,
    overall_stop_reason: str,
    total_games: int,
    total_wall_sec: float,
    experiment_desc: str,
    history_path: Path,
) -> None:
    sep = "=" * 76
    sub_sep = "-" * 76
    throughput = (total_games / total_wall_sec) if total_wall_sec > 0 else 0.0

    print()
    print(sep)
    print("                MULTI-MODE A/B REGRESSION MATRIX SCORECARD")
    print(sep)
    print(f"Tested Change:          {experiment_desc}")
    print(f"Total Games Played:     {total_games} across {len(matrix_results)} ranked modes")
    print(f"Total Elapsed Time:     {total_wall_sec:.2f}s ({throughput:.3f} games/sec)")
    print(f"Matrix Status:          {overall_stop_reason}")
    print()
    print(f"{'Mode':<16} {'Snakes':>6} {'Games':>7} {'Win Share':>10} {'Target':>8} {'Depth (V/B)':>13} {'Result':>12}")
    print(sub_sep)
    for m in matrix_results:
        if m.get("stop_type") == "USER_SKIPPED":
            res_badge = "⏭️ SKIPPED"
        elif m.get("decision") == "KEEP":
            res_badge = "✅ KEEP"
        elif m.get("decision") == "PARITY":
            res_badge = "🟡 PARITY"
        else:
            res_badge = "❌ REVERT"
        name = m.get("name", m.get("mode", "").upper())
        snakes = m.get("num_snakes", 2)
        games = m.get("total_played", 0)
        ws = m.get("var_win_share", 0.0)
        target = m.get("target_share", 50.0 if snakes == 2 else 25.0)
        vd = m.get("var_avg_depth", 0.0)
        bd = m.get("base_avg_depth", 0.0)
        depth_str = f"{vd:.1f}/{bd:.1f}" if (vd > 0 or bd > 0) else "N/A"
        print(f"{name:<16} {snakes:>6} {games:>7} {ws:>9.1f}% {target:>7.1f}% {depth_str:>13} {res_badge:>12}")
    print(sub_sep)
    print()
    if overall_recommendation == "KEEP":
        matrix_status_note = "Passed regression criteria with confirmed improvement"
    elif overall_recommendation == "PARITY":
        matrix_status_note = "Confirmed parity across all tested modes (no regression detected)"
    elif overall_recommendation == "INCOMPLETE":
        matrix_status_note = "All tested modes skipped"
    else:
        matrix_status_note = "Regression confirmed in one or more modes"
    print(f"Final Matrix Decision:  {overall_recommendation} ({matrix_status_note})")
    print(f"History File:           {history_path}")
    print(sep)
    print()


def _run_single_mode_simulation(
    mode: str,
    mode_name: str,
    num_snakes: int,
    target_share: float,
    high_stop: float,
    low_stop: float,
    min_games: int,
    max_games: int,
    check_interval: int,
    dash_client: DashboardClient,
    desc: str,
    is_bug_fix: bool,
    keep_threshold: float,
    threads: int = THREADS,
    matrix_progress: Optional[List[dict]] = None,
    results_dir: Optional[Path] = None,
    base_seed: int = 42_000,
) -> Dict[str, Any]:
    if results_dir is None:
        results_dir = PROJECT_ROOT / "ab_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = results_dir / f"ab_test_{mode}_{timestamp_str}.csv"

    print(f"  --> Running {mode_name} ({num_snakes} snakes, up to {max_games} games)...")

    dash_client.init_test(
        strategy_baseline=STRATEGY_BASELINE,
        strategy_variant=STRATEGY_VARIANT,
        min_games=min_games,
        max_games=max_games,
        check_interval=check_interval,
        early_stop_high=high_stop,
        early_stop_low=low_stop,
        threads=threads,
        experiment_desc=desc,
        game_mode=mode,
        num_snakes=num_snakes,
        target_win_share=target_share,
        matrix_progress=matrix_progress,
    )

    task_args = [(i, base_seed + i, mode) for i in range(1, max_games + 1)]

    snakes_stats = {
        STRATEGY_VARIANT: {"wins": 0, "losses": 0, "draws": 0},
        STRATEGY_BASELINE: {"wins": 0, "losses": 0, "draws": 0},
    }

    results = {
        STRATEGY_BASELINE: {"wins": 0, "losses": 0, "draws": 0},
        STRATEGY_VARIANT: {"wins": 0, "losses": 0, "draws": 0},
    }
    rows: List[Dict[str, Any]] = []

    stop_type = "MAXIMUM_REACHED"
    stop_reason = f"Reached maximum configured game limit ({max_games} games)."
    total_played = 0
    total_var_depth = 0.0
    total_base_depth = 0.0
    var_d_avg = 0.0
    base_d_avg = 0.0
    t_start = time.perf_counter()

    with mp.Pool(
        processes=threads,
        initializer=_init_worker,
        initargs=(_baseline_module_name, _variant_module_name),
    ) as pool:
        iterator = pool.imap_unordered(_worker_wrapper, task_args)
        last_skip_check = time.time()
        try:
            while True:
                try:
                    res = iterator.next(timeout=0.25)
                except (TimeoutError, mp.TimeoutError):
                    if dash_client.check_skip():
                        stop_type = "USER_SKIPPED"
                        stop_reason = f"Mode '{mode_name}' skipped by user via dashboard."
                        print(f"\n[⏭️] Mode '{mode_name}' skipped by user via dashboard.\n")
                        pool.terminate()
                        break
                    continue
                except StopIteration:
                    break

                now = time.time()
                if (total_played % 5 == 0) or (now - last_skip_check > 0.1):
                    last_skip_check = now
                    if dash_client.check_skip():
                        stop_type = "USER_SKIPPED"
                        stop_reason = f"Mode '{mode_name}' skipped by user via dashboard."
                        print(f"\n[⏭️] Mode '{mode_name}' skipped by user via dashboard.\n")
                        pool.terminate()
                        break

                total_played += 1
                winner = res["winner"]
                turns = res["turns"]
                g_num = res["game"]
                dur_ms = res["duration_ms"]
                rows.append(res)

                draw_share = 0.25 if num_snakes == 4 else 0.5

                if winner == "DRAW":
                    tied_snakes = res.get("tied_snakes", [])
                    if not tied_snakes:
                        tied_snakes = list(snakes_stats.keys())
                    k = len(tied_snakes)
                    draw_units = num_snakes / k

                    if STRATEGY_VARIANT in tied_snakes:
                        results[STRATEGY_VARIANT]["draws"] += draw_units
                        snakes_stats[STRATEGY_VARIANT]["draws"] += draw_units
                    else:
                        results[STRATEGY_VARIANT]["losses"] += 1
                        snakes_stats[STRATEGY_VARIANT]["losses"] += 1

                    for s_id in snakes_stats:
                        if s_id != STRATEGY_VARIANT:
                            if s_id in tied_snakes:
                                snakes_stats[s_id]["draws"] += draw_units
                            else:
                                snakes_stats[s_id]["losses"] += 1

                    baseline_tied = [s for s in tied_snakes if s != STRATEGY_VARIANT]
                    if baseline_tied:
                        results[STRATEGY_BASELINE]["draws"] += draw_units * (len(baseline_tied) / (num_snakes - 1 if num_snakes > 1 else 1))
                    else:
                        results[STRATEGY_BASELINE]["losses"] += 1

                    winner_label = f"DRAW ({', '.join(tied_snakes)})" if len(tied_snakes) < num_snakes else "DRAW"
                elif winner == STRATEGY_VARIANT:
                    results[STRATEGY_VARIANT]["wins"] += 1
                    results[STRATEGY_BASELINE]["losses"] += 1
                    if winner in snakes_stats:
                        snakes_stats[winner]["wins"] += 1
                    for s_id in snakes_stats:
                        if s_id != winner:
                            snakes_stats[s_id]["losses"] += 1
                    winner_label = f"Winner: {STRATEGY_VARIANT}"
                else:
                    results[STRATEGY_BASELINE]["wins"] += 1
                    results[STRATEGY_VARIANT]["losses"] += 1
                    if winner in snakes_stats:
                        snakes_stats[winner]["wins"] += 1
                    elif STRATEGY_BASELINE in snakes_stats:
                        snakes_stats[STRATEGY_BASELINE]["wins"] += 1
                    for s_id in snakes_stats:
                        if s_id != winner and s_id != STRATEGY_BASELINE:
                            snakes_stats[s_id]["losses"] += 1
                    winner_label = f"Winner: {STRATEGY_BASELINE}"

                dash_client.update_game(g_num, res["seed"], winner, turns=turns)

                var_w = snakes_stats[STRATEGY_VARIANT]["wins"]
                var_d = snakes_stats[STRATEGY_VARIANT]["draws"]
                var_pts = var_w + draw_share * var_d
                var_win_share = (var_pts / total_played * 100.0) if total_played else target_share

                total_var_depth += res.get("var_avg_depth", 0.0)
                total_base_depth += res.get("base_avg_depth", 0.0)
                var_d_avg = (total_var_depth / total_played) if total_played else 0.0
                base_d_avg = (total_base_depth / total_played) if total_played else 0.0

                if num_snakes == 4:
                    lead_vs_parity = var_win_share - 25.0
                    lead_str = f"{'+' if lead_vs_parity >= 0 else ''}{lead_vs_parity:.1f}% vs 25% Parity"
                    base_w = snakes_stats.get(STRATEGY_BASELINE, {}).get("wins", results[STRATEGY_BASELINE]["wins"])
                    base_d = snakes_stats.get(STRATEGY_BASELINE, {}).get("draws", results[STRATEGY_BASELINE]["draws"])
                    base_pts = base_w + (1.0 - draw_share) * base_d
                    base_win_share = (base_pts / total_played * 100.0) if total_played else 75.0
                    print(
                        f"    Game {total_played:>3}/{max_games} (Sim #{g_num:>3}) -> {winner_label:<20} "
                        f"({turns:>3} turns, {dur_ms:>5.0f}ms) | Var Share: {var_win_share:>5.1f}% ({lead_str}) [Var: {var_win_share:.1f}% vs Base Team: {base_win_share:.1f}%] | Depth: V {var_d_avg:.1f} vs B {base_d_avg:.1f}"
                    )
                else:
                    base_w = results[STRATEGY_BASELINE]["wins"]
                    base_d = results[STRATEGY_BASELINE]["draws"]
                    base_pts = base_w + draw_share * base_d
                    base_win_share = (base_pts / total_played * 100.0) if total_played else 50.0
                    print(
                        f"    Game {total_played:>3}/{max_games} (Sim #{g_num:>3}) -> {winner_label:<20} "
                        f"({turns:>3} turns, {dur_ms:>5.0f}ms) | Win Share: {var_win_share:>5.1f}% (Var: {var_win_share:.1f}% vs Base: {base_win_share:.1f}%) | Depth: V {var_d_avg:.1f} vs B {base_d_avg:.1f}"
                    )

                # Continuous dynamic statistical confidence check
                if total_played >= min_games:
                    curr_high_stop, curr_low_stop = get_statistical_thresholds(total_played, num_snakes)
                    z_curr, se_curr = compute_z_score(var_win_share, total_played, num_snakes)

                    # Periodic checkpoint update (at min_games and every 25 games thereafter)
                    if total_played == min_games or total_played % 25 == 0:
                        print(
                            f"\n      [Confidence Check @ Game {total_played}] Var Share: {var_win_share:.1f}% "
                            f"(z={z_curr:+.2f}, SE={se_curr:.1f}%) | Early Stop Bounds: <{curr_low_stop:.1f}% or >{curr_high_stop:.1f}%\n"
                        )

                    if var_win_share > curr_high_stop:
                        stop_type = "EARLY_STOP_HIGH"
                        stop_reason = (
                            f"Variant win share ({var_win_share:.1f}%) exceeded statistical upper threshold "
                            f"({curr_high_stop:.1f}%, z={z_curr:+.2f} vs parity) at game {total_played}. Confident improvement."
                        )
                        pool.terminate()
                        break
                    elif var_win_share < curr_low_stop:
                        stop_type = "EARLY_STOP_LOW"
                        stop_reason = (
                            f"Variant win share ({var_win_share:.1f}%) fell below statistical lower threshold "
                            f"({curr_low_stop:.1f}%, z={z_curr:+.2f} vs parity) at game {total_played}. Confident regression."
                        )
                        pool.terminate()
                        break
        except KeyboardInterrupt:
            print("\n[!] Interrupted by user. Terminating worker pool...")
            pool.terminate()
            pool.join()
            dash_client.set_finished(
                stop_type="MANUAL_ABORT",
                stop_reason="Simulation interrupted by user",
                recommendation="PENDING",
                csv_path="",
            )
            sys.exit(130)

    duration_sec = time.perf_counter() - t_start

    # Export CSV results
    rows.sort(key=lambda r: r["game"])
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["game", "seed", "winner", "turns", "duration_ms"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    draw_share = 0.25 if num_snakes == 4 else 0.5
    var_w = snakes_stats[STRATEGY_VARIANT]["wins"]
    var_d = snakes_stats[STRATEGY_VARIANT]["draws"]
    var_pts = var_w + draw_share * var_d
    var_win_share = (var_pts / total_played * 100.0) if total_played else target_share
    z_final, se_final = compute_z_score(var_win_share, total_played, num_snakes)

    # 3-Outcome Decision Classification:
    # 1. KEEP: Confident improvement (Early stop high OR win share >= keep_threshold with z >= 1.645 vs parity)
    # 2. REVERT: Confident regression (Early stop low OR statistically significantly below parity z <= -1.645)
    # 3. PARITY: Confident no regression (Played to max games within the 95% parity confidence band)
    if stop_type == "USER_SKIPPED":
        passed = False
        decision = "SKIPPED"
    elif stop_type == "EARLY_STOP_HIGH":
        passed = True
        decision = "KEEP"
    elif stop_type == "EARLY_STOP_LOW":
        passed = False
        decision = "REVERT"
    else:
        # Reached MAX_GAMES (all 200 games played without triggering early cutoff)
        if num_snakes == 4:
            max_opp_win_share = max(
                ((s["wins"] + 0.25 * s["draws"]) / total_played * 100.0)
                for s_id, s in snakes_stats.items()
                if s_id != STRATEGY_VARIANT
            ) if total_played > 0 else 25.0
        else:
            max_opp_win_share = 0.0

        decision, passed, stop_reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z_final,
            keep_threshold=keep_threshold,
            num_snakes=num_snakes,
            max_opp_win_share=max_opp_win_share,
            se=se_final,
        )

    return {
        "mode": mode,
        "name": mode_name,
        "num_snakes": num_snakes,
        "target_share": target_share,
        "total_played": total_played,
        "var_win_share": round(var_win_share, 1),
        "var_avg_depth": round(var_d_avg, 2),
        "base_avg_depth": round(base_d_avg, 2),
        "z_score": round(z_final, 2),
        "se_pct": round(se_final, 2),
        "results": results,
        "snakes_stats": snakes_stats,
        "stop_type": stop_type,
        "stop_reason": stop_reason,
        "passed": passed,
        "decision": decision,
        "duration_sec": duration_sec,
        "csv_path": str(csv_path),
    }


def parse_selected_modes(raw_modes: str) -> List[Dict[str, Any]]:
    """
    Parses user-specified modes string into a validated list of mode specs from MODES_MATRIX.

    Supported inputs:
      - 'all' or empty: all 4 ranked modes (Constrictor 4P, Royale 4P, Duel 1v1, Standard 4P)
      - 'multi': all 3 multiplayer modes (Constrictor 4P, Royale 4P, Standard 4P)
      - Comma-separated or space-separated mode names: e.g. 'constrictor', 'royale', 'standard,royale', 'duel,standard'
      - Aliases: 'ffa' -> 'standard', 'wrapped' -> 'standard', '1v1' -> 'duel', '4p' -> 'standard'

    CAUTION & WARNING:
      Running an isolated subset of modes (e.g. '--modes royale') saves substantial execution
      time during mode-specific hypothesis exploration. However, running isolated mode tests
      risks introducing undetected regressions in omitted modes. Pre-flight unit tests
      ALWAYS execute regardless of selected modes, but passing PRs into production/test
      must either run the full regression matrix ('--modes all') or explicitly justify
      why omitted modes are unaffected.
    """
    if not raw_modes or raw_modes.strip().lower() in ("all", "default"):
        return list(MODES_MATRIX)

    if raw_modes.strip().lower() == "multi":
        return [m for m in MODES_MATRIX if m["mode"] != "duel"]

    tokens = [t.strip().lower() for t in re.split(r"[\s,]+", raw_modes) if t.strip()]
    alias_map = {
        "ffa": "standard",
        "wrapped": "standard",
        "1v1": "duel",
        "4p": "standard",
    }

    selected_modes = []
    seen = set()
    for token in tokens:
        canon = alias_map.get(token, token)
        matching = next((m for m in MODES_MATRIX if m["mode"] == canon), None)
        if matching is None:
            valid_names = ", ".join([m["mode"] for m in MODES_MATRIX] + list(alias_map.keys()) + ["all", "multi"])
            sys.exit(f"[!] Error: Unknown game mode '{token}'. Valid options: {valid_names}")
        if canon not in seen:
            seen.add(canon)
            selected_modes.append(matching)

    if not selected_modes:
        return list(MODES_MATRIX)
    return selected_modes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automated Fast A/B Testing Harness for Battlesnake Strategies.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--desc", "-d", "--description",
        type=str,
        default="Unspecified Experiment",
        help="Short description of the tested variant change",
    )
    parser.add_argument(
        "--modes", "--mode", "-m",
        dest="modes",
        type=str,
        default="all",
        help=(
            "Target game mode(s) to execute. Can be 'all', 'multi', a single mode, or a comma-separated "
            "list of 1-4 modes (e.g. '--modes royale' or '--modes standard,royale'). "
            "WARNING: Testing an isolated subset of modes risks introducing regressions in omitted modes."
        ),
    )
    parser.add_argument(
        "--baseline-module",
        type=str,
        default=DEFAULT_BASELINE_MODULE,
        help=f"Module name or file path for baseline snake strategy (default: {DEFAULT_BASELINE_MODULE})",
    )
    parser.add_argument(
        "--variant-module",
        type=str,
        default=DEFAULT_VARIANT_MODULE,
        help=f"Module name or file path for variant snake strategy (default: {DEFAULT_VARIANT_MODULE})",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the pre-flight official Battlesnake CLI binary smoke test",
    )
    return parser.parse_args()


# ── Main Engine Runner ───────────────────────────────────────────────────────

def run_ab_test_engine(
    desc: str = "Unspecified Experiment",
    modes: str = "all",
    baseline_module: Optional[str] = None,
    variant_module: Optional[str] = None,
    skip_smoke: bool = False,
    threads: int = THREADS,
    min_games: int = MIN_GAMES,
    max_games: int = MAX_GAMES,
    check_interval: int = CHECK_INTERVAL,
    port_base: int = PORT_BASELINE,
    port_var: int = PORT_VARIANT,
    dashboard_port: int = DASHBOARD_PORT,
) -> None:
    if baseline_module is None:
        baseline_module = DEFAULT_BASELINE_MODULE
    if variant_module is None:
        variant_module = DEFAULT_VARIANT_MODULE
    init_strategies(baseline_module, variant_module)
    cli_path = find_battlesnake_cli()
    python_bin = find_python_interpreter()

    is_bug_fix = any(k in desc.lower() for k in ("fix", "bug", "blunder", "issue", "patch", "repair"))

    active_matrix = parse_selected_modes(modes)
    is_partial_matrix = len(active_matrix) < len(MODES_MATRIX)
    mode_names = ", ".join([m["name"] for m in active_matrix])

    print("=" * 76)
    print("         BATTLESNAKE AUTOMATED MULTI-MODE A/B TESTING HARNESS")
    print("=" * 76)
    print(f"  Tested Change:    {desc}")
    print(f"  Baseline Snake:   {baseline_module}")
    print(f"  Variant Snake:    {variant_module}")
    print(f"  Evaluation:       {mode_names if is_partial_matrix else 'Full 4-Mode Ranked Regression Matrix'}")
    if is_partial_matrix:
        print(f"  [⚠️ WARNING]      Running ISOLATED mode test for {len(active_matrix)} mode(s): {mode_names}")
        print(f"                    Danger: Changes may introduce undetected regressions in omitted modes!")
    print(f"  Target Policy:    {'Bug Fix (>= 50% in 1v1, >= 25% in 4p)' if is_bug_fix else 'General Improvement (>= 51.5% in 1v1, >= 25.5% in 4p)'}")
    print(f"  Sample Budget:    {max_games} max games/mode (Min: {min_games}, Checkpoint: Every {check_interval})")
    print(f"  Workers:          {threads} parallel worker processes (In-Process Simulator)")
    print(f"  Dashboard:        http://localhost:{dashboard_port} (Browser Live View)")
    print("=" * 76 + "\n")

    dash_client = ensure_dashboard_running(port=dashboard_port)
    if not dash_client.is_running():
        sys.exit(f"[!] Fatal error: Failed to connect to dashboard at port {dashboard_port}. An A/B test cannot run without showing in the browser.")

    # ── Step 1: Tactical Unit Tests ──
    print("[1/2] Running strategy safety unit tests on variant...")
    test_strategy = None
    try:
        from tests import test_strategy
    except ImportError:
        try:
            import test_strategy
        except ImportError:
            test_strategy = None

    if test_strategy and hasattr(test_strategy, "run_variant_unit_tests"):
        passed, test_msg = test_strategy.run_variant_unit_tests()
        if not passed:
            print(f"\n[❌] UNIT TEST SUITE FAILED FOR STRATEGY VARIANT!")
            print(f"     Details: {test_msg}\n")
            stop_type = "TEST_FAILURE"
            stop_reason = f"Unit test suite failed: {test_msg}"
            recommendation = "TEST_FAILURE"
            results = {
                STRATEGY_BASELINE: {"wins": 0, "losses": 0, "draws": 0},
                STRATEGY_VARIANT: {"wins": 0, "losses": 0, "draws": 0},
            }
            history_path = save_run_history(
                STRATEGY_BASELINE, STRATEGY_VARIANT, 0, results,
                stop_type, stop_reason, recommendation, experiment_desc=desc
            )
            dash_client.set_finished(stop_type, stop_reason, recommendation, str(history_path))
            print_summary(0, results, stop_type, stop_reason, history_path, experiment_desc=desc, recommendation=recommendation)
            return
        print("  [✓] Strategy variant passed all tactical unit tests.\n")
    else:
        print("  [✓] No tactical unit tests registered (standalone harness mode).\n")

    # ── Pre-flight Binary Smoke Test (skippable via --skip-smoke) ──
    if not skip_smoke:
        main_py = PROJECT_ROOT / "main.py"
        if Path(cli_path).exists() and main_py.exists():
            print("  --> Running pre-flight binary smoke test with official Battlesnake CLI...")
            smoke_ok, smoke_err = run_binary_smoke_test(cli_path, python_bin, port_base, port_var)
            if not smoke_ok:
                print(f"  [⚠️] Pre-flight smoke test skipped/warning: {smoke_err}\n")
            else:
                print("  [✓] Battlesnake CLI smoke test passed.\n")
        else:
            print("  [i] Pre-flight binary smoke test skipped (CLI binary or main.py not found).\n")
    else:
        print("  [i] Pre-flight binary smoke test skipped via --skip-smoke.\n")

    # ── Step 2: Simulation Execution ─────────────────────────────────────────
    results_dir = PROJECT_ROOT / "ab_results"
    results_dir.mkdir(parents=True, exist_ok=True)
    t_wall_start = time.perf_counter()

    matrix_title = f"Selected Modes Matrix ({mode_names})" if is_partial_matrix else "Full 4-Mode Ranked Regression Matrix Suite"
    print(f"[2/2] Executing {matrix_title}...")
    matrix_progress = [
        {
            "mode": m["mode"],
            "name": m["name"],
            "num_snakes": m["num_snakes"],
            "status": "PENDING",
            "win_share": 0.0,
            "games": 0,
            "max_games": max_games,
            "target_share": m["target_share"],
            "decision": "PENDING",
            "reason": "",
        }
        for m in active_matrix
    ]

    matrix_results = []
    total_games_all = 0
    agg_results = {
        STRATEGY_BASELINE: {"wins": 0, "losses": 0, "draws": 0},
        STRATEGY_VARIANT: {"wins": 0, "losses": 0, "draws": 0},
    }

    for idx, m_spec in enumerate(active_matrix):
        m_mode = m_spec["mode"]
        m_name = m_spec["name"]
        num_snakes = m_spec["num_snakes"]
        target_share = m_spec["target_share"]
        high_stop = m_spec["high_stop"]
        low_stop = m_spec["low_stop"]
        m_keep_thresh = (
            (THRESHOLD_BUG_FIX if is_bug_fix else THRESHOLD_IMPROVEMENT)
            if num_snakes == 2
            else (THRESHOLD_4P_BUG_FIX if is_bug_fix else THRESHOLD_4P_IMPROVEMENT)
        )

        matrix_progress[idx]["status"] = "RUNNING"
        dash_client.update_matrix(matrix_progress)

        res = _run_single_mode_simulation(
            mode=m_mode,
            mode_name=m_name,
            num_snakes=num_snakes,
            target_share=target_share,
            high_stop=high_stop,
            low_stop=low_stop,
            min_games=min_games,
            max_games=max_games,
            check_interval=check_interval,
            threads=threads,
            dash_client=dash_client,
            desc=desc,
            is_bug_fix=is_bug_fix,
            keep_threshold=m_keep_thresh,
            matrix_progress=matrix_progress,
            results_dir=results_dir,
            base_seed=42_000 + idx * 10_000,
        )

        matrix_results.append(res)
        total_games_all += res["total_played"]
        for k in ("wins", "losses", "draws"):
            agg_results[STRATEGY_BASELINE][k] += res["results"][STRATEGY_BASELINE][k]
            agg_results[STRATEGY_VARIANT][k] += res["results"][STRATEGY_VARIANT][k]

        if res["stop_type"] == "USER_SKIPPED":
            matrix_progress[idx]["status"] = "SKIPPED"
            matrix_progress[idx]["decision"] = "SKIPPED"
        elif res["decision"] == "KEEP":
            matrix_progress[idx]["status"] = "PASSED"
            matrix_progress[idx]["decision"] = "KEEP"
        elif res["decision"] == "PARITY":
            matrix_progress[idx]["status"] = "PARITY"
            matrix_progress[idx]["decision"] = "PARITY"
        else:
            matrix_progress[idx]["status"] = "FAILED"
            matrix_progress[idx]["decision"] = "REVERT"
        matrix_progress[idx]["win_share"] = res["var_win_share"]
        matrix_progress[idx]["games"] = res["total_played"]
        matrix_progress[idx]["reason"] = res["stop_reason"]
        dash_client.update_matrix(matrix_progress)

        # Early stoppage: ONLY halt remaining matrix modes if a regression is confirmed (REVERT)
        if res["decision"] == "REVERT" and res.get("stop_type") != "USER_SKIPPED":
            print(f"\n[🛑] EARLY STOPPAGE: Mode '{m_name}' confirmed regression ({res['var_win_share']:.1f}% vs {target_share:.1f}% target).")
            print("     Halting remaining matrix modes early as regression is confirmed.\n")
            for rem_idx in range(idx + 1, len(active_matrix)):
                matrix_progress[rem_idx]["status"] = "SKIPPED"
                matrix_progress[rem_idx]["decision"] = "SKIPPED"
                matrix_progress[rem_idx]["reason"] = f"Skipped: Prior mode '{m_name}' confirmed regression"
            dash_client.update_matrix(matrix_progress)
            break

    total_wall_sec = time.perf_counter() - t_wall_start
    revert_modes = [r for r in matrix_results if r["decision"] == "REVERT" and r.get("stop_type") != "USER_SKIPPED"]
    keep_modes = [r for r in matrix_results if r["decision"] == "KEEP"]
    parity_modes = [r for r in matrix_results if r["decision"] == "PARITY"]
    skipped_modes = [r for r in matrix_results if r.get("stop_type") == "USER_SKIPPED"]

    if revert_modes:
        overall_recommendation = "REVERT"
        overall_stop_type = "MATRIX_FAILED"
        if len(matrix_results) < len(active_matrix):
            overall_stop_reason = (
                f"Mode '{revert_modes[0]['name']}' confirmed regression "
                f"({revert_modes[0]['var_win_share']:.1f}% vs {revert_modes[0]['target_share']:.1f}%). "
                f"Early stoppage triggered ({len(active_matrix) - len(matrix_results)} remaining modes aborted)."
            )
        else:
            overall_stop_reason = f"{len(revert_modes)} of {len(active_matrix)} tested modes confirmed regression."
    elif skipped_modes and not keep_modes and not parity_modes:
        overall_recommendation = "INCOMPLETE"
        overall_stop_type = "MATRIX_PARTIAL"
        overall_stop_reason = "All tested modes skipped by user."
    elif keep_modes:
        overall_recommendation = "KEEP"
        overall_stop_type = "MATRIX_COMPLETE"
        if parity_modes:
            overall_stop_reason = f"Passed regression criteria ({len(keep_modes)} improved, {len(parity_modes)} at parity)."
        else:
            overall_stop_reason = f"All {len(keep_modes)} tested modes passed with confirmed improvement."
    else:
        overall_recommendation = "PARITY"
        overall_stop_type = "MATRIX_COMPLETE"
        overall_stop_reason = f"All {len(parity_modes)} tested modes performed at parity with no regression detected."
    if is_partial_matrix:
        overall_stop_reason += f" [Note: Isolated mode test on {mode_names}]"

    history_path = save_run_history(
        STRATEGY_BASELINE,
        STRATEGY_VARIANT,
        total_games_all,
        agg_results,
        overall_stop_type,
        overall_stop_reason,
        overall_recommendation,
        experiment_desc=desc,
        matrix_results=matrix_results,
        game_mode=modes,
    )

    dash_client.set_finished(overall_stop_type, overall_stop_reason, overall_recommendation, str(history_path))
    time.sleep(0.3)

    print_matrix_summary(
        matrix_results=matrix_results,
        overall_recommendation=overall_recommendation,
        overall_stop_reason=overall_stop_reason,
        total_games=total_games_all,
        total_wall_sec=total_wall_sec,
        experiment_desc=desc,
        history_path=history_path,
    )


def main():
    args = parse_args()
    run_ab_test_engine(
        desc=args.desc,
        modes=args.modes,
        baseline_module=args.baseline_module,
        variant_module=args.variant_module,
        skip_smoke=args.skip_smoke,
    )


if __name__ == "__main__":
    main()
