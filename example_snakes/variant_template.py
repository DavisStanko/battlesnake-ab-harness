"""
variant_template.py — Runnable starter template for experimental Battlesnake strategy variants.

Runnable copy of survival.py with an annotated hook for testing new algorithmic ideas,
heuristics, tree searches, or food-seeking behaviors.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Set, Tuple

INFO: Dict[str, str] = {
    "apiversion": "1",
    "author": "Battlesnake A/B Harness",
    "color": "#3b82f6",
    "head": "default",
    "tail": "default",
}

DIRECTIONS: Dict[str, Tuple[int, int]] = {
    "up": (0, 1),
    "down": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
}


def choose_move(game_state: Dict[str, Any], wrap: bool = False, constrictor: bool = False) -> str:
    """
    Chooses a move for the variant strategy.
    """
    board = game_state["board"]
    width = int(board["width"])
    height = int(board["height"])
    you = game_state["you"]
    head_x = int(you["head"]["x"])
    head_y = int(you["head"]["y"])

    # Build obstacle set from all living snakes
    obstacles: Set[Tuple[int, int]] = set()
    for snake in board.get("snakes", []):
        body = snake.get("body", [])
        if not body:
            continue
        s_health = int(snake.get("health", 100))

        # In constrictor mode or if health == 100 (snake just ate), the tail does NOT vacate.
        # Otherwise, the tail vacates at the end of the turn and is safe to step onto.
        if constrictor or s_health == 100:
            obstacles.update((int(seg["x"]), int(seg["y"])) for seg in body)
        else:
            # Tail vacates; all segments except the final tail are obstacles
            obstacles.update((int(seg["x"]), int(seg["y"])) for seg in body[:-1])

    surviving_moves: List[str] = []
    for move_name, (dx, dy) in DIRECTIONS.items():
        if wrap:
            nx = (head_x + dx) % width
            ny = (head_y + dy) % height
        else:
            nx = head_x + dx
            ny = head_y + dy
            if nx < 0 or nx >= width or ny < 0 or ny >= height:
                continue

        if (nx, ny) not in obstacles:
            surviving_moves.append(move_name)

    if not surviving_moves:
        return "up"

    # =========================================================================
    # ### YOUR IDEA HERE ###
    #
    # Examples of enhancements to benchmark against baseline:
    # 1. Food seeking: prioritize surviving moves that minimize Manhattan distance to nearest food
    # 2. Flood-fill / territory: pick the move with the largest accessible open space
    # 3. Head-to-head avoidance: avoid squares adjacent to larger opponent heads
    # 4. Minimax lookahead: search multiple plies ahead with alpha-beta pruning
    #
    # Default behavior matches survival.py (uniform random among surviving moves)
    # to confirm statistical PARITY on initial benchmark runs.
    # =========================================================================

    return random.choice(surviving_moves)


def move(game_state: Dict[str, Any]) -> Dict[str, str]:
    """Official Battlesnake /move HTTP handler contract."""
    ruleset = game_state.get("game", {}).get("ruleset", {})
    ruleset_name = str(ruleset.get("name", "standard")).lower()
    wrap = ruleset_name in ("wrapped", "wrap")
    constrictor = ruleset_name == "constrictor"
    return {"move": choose_move(game_state, wrap=wrap, constrictor=constrictor)}
