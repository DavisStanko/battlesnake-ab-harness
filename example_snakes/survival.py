"""
survival.py — Reference "don't kill yourself this turn" baseline strategy.

Pure Python, no C extension, no heuristics.
Rejects any move leading to:
- Out of bounds (respecting wrap when enabled)
- Collision with own body or another snake's body
- A tail square of a snake with health == 100 (tail stays; otherwise tail vacates and is safe)

Picks uniformly at random among surviving moves; if none survive, moves up.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Set, Tuple

INFO: Dict[str, str] = {
    "apiversion": "1",
    "author": "Battlesnake A/B Harness",
    "color": "#16a34a",
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
    Chooses a safe 1-turn move avoiding immediate collisions.
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

    if surviving_moves:
        return random.choice(surviving_moves)
    return "up"


def move(game_state: Dict[str, Any]) -> Dict[str, str]:
    """Official Battlesnake /move HTTP handler contract."""
    ruleset = game_state.get("game", {}).get("ruleset", {})
    ruleset_name = str(ruleset.get("name", "standard")).lower()
    wrap = ruleset_name in ("wrapped", "wrap")
    constrictor = ruleset_name == "constrictor"
    return {"move": choose_move(game_state, wrap=wrap, constrictor=constrictor)}
