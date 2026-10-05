"""
simulator.py — Battlesnake Forward Game Simulator

Simulates one game turn given a board state and a set of moves.
Matches the official Battlesnake game engine rules exactly.

Turn Resolution Order:
1. Move each snake:
   Add a new head cell in the chosen direction.
   Remove the tail cell, unless the snake ate food this turn (see step 3).
2. Reduce health:
   Each snake loses 1 health per turn, before food and hazard checks.
3. Apply food:
   If a snake's new head lands on a food cell, remove that food from the board,
   increase the snake's length by 1 (do not remove the tail this turn),
   and reset that snake's health to 100.
4. Apply hazards (if used):
   If a snake's new head lands on a hazard cell, apply extra health loss per the
   hazard damage rule (default: 15 health lost, on top of normal 1 per turn).
   Food on a hazard square overrides hazard damage (resets health to 100).
5. Check starvation:
   Any snake with health at or below 0 is eliminated.
6. Check out-of-bounds:
   Any snake whose new head is outside the board width/height is eliminated.
7. Check self-collision:
   Any snake whose new head hits its own body (including neck, excluding the
   tail cell that moves away unless it just ate) is eliminated.
8. Check other-snake body collision:
   Any snake whose new head hits another snake's body (any segment except that
   other snake's head) is eliminated.
9. Check head-to-head collision:
   If two snakes' new heads land on the same cell:
   - The shorter snake is eliminated.
   - If both snakes are the same length, both are eliminated.
   - This overrides body-collision checks for that cell.
10. Apply all eliminations simultaneously:
   All checks above are based on the state before any elimination is applied.
   One snake's elimination does not prevent another snake's collision check in the same turn.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Battlesnake Elimination Cause Constants
NOT_ELIMINATED = ""
ELIMINATED_BY_COLLISION = "snake-collision"
ELIMINATED_BY_SELF_COLLISION = "snake-self-collision"
ELIMINATED_BY_OUT_OF_HEALTH = "out-of-health"
ELIMINATED_BY_HEAD_COLLISION = "head-collision"
ELIMINATED_BY_OUT_OF_BOUNDS = "wall-collision"
ELIMINATED_BY_HAZARD = "hazard"

# Movement directions and coordinate offsets
MOVE_OFFSETS: Dict[str, Tuple[int, int]] = {
    "up": (0, 1),
    "down": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
}

Coord = Tuple[int, int]


def parse_coord(coord: Any) -> Tuple[int, int]:
    """Parse coordinate from dict {'x': int, 'y': int}, tuple (x, y), or object with x, y attributes."""
    if isinstance(coord, dict):
        return (int(coord["x"]), int(coord["y"]))
    if isinstance(coord, (tuple, list)):
        return (int(coord[0]), int(coord[1]))
    if hasattr(coord, "x") and hasattr(coord, "y"):
        return (int(coord.x), int(coord.y))
    raise TypeError(f"Unsupported coordinate format: {coord!r}")


def format_coord(coord: Tuple[int, int], as_dict: bool) -> Any:
    """Format coordinate as dict {'x': x, 'y': y} or tuple (x, y)."""
    return {"x": coord[0], "y": coord[1]} if as_dict else (coord[0], coord[1])


def get_default_move(body: Sequence[Tuple[int, int]]) -> str:
    """
    Official Battlesnake getDefaultMove logic:
    Determines forward direction away from neck (body[1]), fallback 'up'.
    """
    if len(body) >= 2:
        head, neck = body[0], body[1]
        if head[0] == neck[0] + 1:
            return "right"
        if head[0] == neck[0] - 1:
            return "left"
        if head[1] == neck[1] + 1:
            return "up"
        if head[1] == neck[1] - 1:
            return "down"
    return "up"


def _extract_snake_id(snake: Any) -> str:
    """Extract ID string from snake dict or object."""
    if isinstance(snake, dict):
        return str(snake.get("id", snake.get("ID", "")))
    return str(getattr(snake, "id", getattr(snake, "ID", "")))


def parse_moves(
    moves: Any,
    snakes: Sequence[Any],
) -> Dict[str, str]:
    """
    Parse moves from multiple possible formats:
    - Dict: {snake_id: 'up', ...}
    - List of dicts: [{'id': snake_id, 'move': 'up'}, ...]
    - List of tuples: [(snake_id, 'up'), ...]
    - List of strings: ['up', 'down', ...] matched by index to snakes
    """
    result: Dict[str, str] = {}
    if isinstance(moves, dict):
        for k, v in moves.items():
            result[str(k)] = str(v).strip().lower()
    elif isinstance(moves, (list, tuple)):
        if not moves:
            return result
        first = moves[0]
        if isinstance(first, str):
            for i, move in enumerate(moves):
                if i < len(snakes):
                    s_id = _extract_snake_id(snakes[i])
                    result[s_id] = str(move).strip().lower()
        elif isinstance(first, dict):
            for item in moves:
                s_id = str(item.get("id", item.get("ID", "")))
                m = str(item.get("move", item.get("Move", ""))).strip().lower()
                if s_id:
                    result[s_id] = m
        elif isinstance(first, (list, tuple)):
            for item in moves:
                if len(item) == 2:
                    result[str(item[0])] = str(item[1]).strip().lower()
    return result


class _SnakeInternal:
    """Internal tracking representation for a snake during turn simulation."""

    def __init__(
        self,
        snake_id: str,
        body: List[Tuple[int, int]],
        health: int,
        raw_snake: Dict[str, Any],
        as_dict: bool,
    ):
        self.id = snake_id
        self.old_body = body
        self.new_head: Tuple[int, int] = body[0] if body else (0, 0)
        self.new_body: List[Tuple[int, int]] = []
        self.health = health
        self.raw_snake = raw_snake
        self.as_dict = as_dict
        self.move: str = ""
        self.ate_food: bool = False
        self.eliminated: bool = False
        self.eliminated_cause: str = NOT_ELIMINATED
        self.eliminated_by: str = ""


def simulate_turn(
    *args: Any,
    hazard_damage: Optional[int] = None,
    return_all_snakes: bool = False,
    **kwargs: Any,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Simulate one game turn according to official Battlesnake rules.

    Supported Calling Signatures:
      simulate_turn(board_state, snakes, moves, ...)
      simulate_turn(game_state, moves, ...)
      simulate_turn(width, height, food, hazards, snakes, moves, ...)
      simulate_turn(board_state=..., snakes=..., moves=..., ...)

    Parameters
    ----------
    board_state : dict or object
        Contains board dimensions and lists of items:
        - 'width' (int)
        - 'height' (int)
        - 'food' / 'food_positions' (list of coordinates)
        - 'hazards' / 'hazard_positions' (list of coordinates, optional)
    snakes : list of dicts
        List of snakes. Each snake must have:
        - 'id' (str)
        - 'body' (ordered list of coordinates, head first)
        - 'health' (int, 0-100)
    moves : dict or list
        Chosen move for each snake: 'up', 'down', 'left', or 'right'.
        Can be a dict {snake_id: move} or list of moves.
    hazard_damage : int, optional
        Extra health lost when landing on hazard. Default is 15 (per rules).
    return_all_snakes : bool, optional
        If True, the second return value includes all snakes with their
        eliminated status. If False (default), returns only alive snakes.

    Returns
    -------
    new_board_state : dict
        Updated board state containing 'width', 'height', 'food', 'hazards',
        'snakes' (alive snakes), and 'eliminated_snakes' (eliminated snakes).
    new_snake_states : list of dicts
        New snake states after applying all moves for one turn.
        Defaults to active (alive) snakes.
    """
    # ── 0. Parse Arguments ──────────────────────────────────────────────────
    board_input: Any = None
    snakes_input: Any = None
    moves_input: Any = None

    if len(args) == 6:
        w, h, f, hz, snakes_input, moves_input = args
        board_input = {"width": w, "height": h, "food": f, "hazards": hz}
    elif len(args) == 3:
        board_input, snakes_input, moves_input = args
    elif len(args) == 2:
        board_input, moves_input = args
    elif len(args) == 1:
        board_input = args[0]
    elif len(args) == 0:
        board_input = kwargs.get("board_state", kwargs.get("board", kwargs.get("game_state")))
        snakes_input = kwargs.get("snakes")
        moves_input = kwargs.get("moves")

    if snakes_input is None and isinstance(board_input, dict):
        if "snakes" in board_input:
            snakes_input = board_input["snakes"]
        elif "board" in board_input and isinstance(board_input["board"], dict):
            snakes_input = board_input["board"].get("snakes", [])
            board_input = board_input["board"]

    if moves_input is None:
        moves_input = kwargs.get("moves", {})

    if board_input is None:
        raise ValueError("Missing board state input.")
    if snakes_input is None:
        snakes_input = []

    # ── Unpack Board State ───────────────────────────────────────────────────
    raw_board: Dict[str, Any]
    if isinstance(board_input, dict):
        raw_board = copy.deepcopy(board_input)
        width = int(raw_board.get("width", raw_board.get("w", 11)))
        height = int(raw_board.get("height", raw_board.get("h", 11)))
        raw_food = raw_board.get("food", raw_board.get("food_positions", raw_board.get("foods", [])))
        raw_hazards = raw_board.get("hazards", raw_board.get("hazard_positions", []))
    else:
        raw_board = {}
        width = int(getattr(board_input, "width", 11))
        height = int(getattr(board_input, "height", 11))
        raw_food = getattr(board_input, "food", getattr(board_input, "food_positions", []))
        raw_hazards = getattr(board_input, "hazards", getattr(board_input, "hazard_positions", []))

    # Detect coordinate style (dict vs tuple) from inputs
    as_dict_board = True
    if raw_food and not isinstance(raw_food[0], dict):
        as_dict_board = False
    elif raw_hazards and not isinstance(raw_hazards[0], dict):
        as_dict_board = False

    food_list: List[Tuple[int, int]] = [parse_coord(p) for p in raw_food]
    hazard_list: List[Tuple[int, int]] = [parse_coord(p) for p in raw_hazards]
    hazard_set: Set[Tuple[int, int]] = set(hazard_list)
    food_set: Set[Tuple[int, int]] = set(food_list)

    # Resolve hazard damage default
    if hazard_damage is None:
        if "hazard_damage" in raw_board:
            hazard_damage = int(raw_board["hazard_damage"])
        elif "ruleset" in raw_board and isinstance(raw_board["ruleset"], dict):
            settings = raw_board["ruleset"].get("settings", {})
            hazard_damage = int(settings.get("hazardDamagePerTurn", 15))
        else:
            hazard_damage = 15

    # ── Unpack Snakes ────────────────────────────────────────────────────────
    parsed_moves = parse_moves(moves_input, snakes_input)
    internal_snakes: List[_SnakeInternal] = []

    for s in snakes_input:
        if isinstance(s, dict):
            s_raw = copy.deepcopy(s)
            s_id = str(s.get("id", s.get("ID", "")))
            raw_body = s.get("body", [])
            health = int(s.get("health", 100))
        else:
            s_id = str(getattr(s, "id", getattr(s, "ID", "")))
            raw_body = getattr(s, "body", [])
            health = int(getattr(s, "health", 100))
            s_raw = {"id": s_id, "body": raw_body, "health": health}

        as_dict_snake = True
        if raw_body and not isinstance(raw_body[0], dict):
            as_dict_snake = False

        body = [parse_coord(pt) for pt in raw_body]
        internal_snakes.append(
            _SnakeInternal(
                snake_id=s_id,
                body=body,
                health=health,
                raw_snake=s_raw,
                as_dict=as_dict_snake,
            )
        )

    # ── RULE 1: Move each snake & RULE 3: Check Food ─────────────────────────
    # First, calculate new head for each snake
    eaten_foods: Set[Tuple[int, int]] = set()

    wrap = bool(kwargs.get("wrap", False))
    constrictor = bool(kwargs.get("constrictor", False))

    for snake in internal_snakes:
        if len(snake.old_body) == 0:
            continue

        move = parsed_moves.get(snake.id)
        if move not in MOVE_OFFSETS:
            move = get_default_move(snake.old_body)
        snake.move = move

        dx, dy = MOVE_OFFSETS[move]
        curr_head = snake.old_body[0]
        nx, ny = curr_head[0] + dx, curr_head[1] + dy
        if wrap and width > 0 and height > 0:
            nx = nx % width
            ny = ny % height
        snake.new_head = (nx, ny)

        if snake.new_head in food_set:
            snake.ate_food = True
            eaten_foods.add(snake.new_head)

    # Apply movement:
    # Add new head cell. Remove tail cell, unless snake ate food or constrictor mode.
    for snake in internal_snakes:
        if len(snake.old_body) == 0:
            continue
        if snake.ate_food or constrictor:
            # Snake ate food or constrictor: do not remove tail this turn (length increases by 1)
            snake.new_body = [snake.new_head] + snake.old_body
        else:
            # Snake did not eat food: remove tail cell
            snake.new_body = [snake.new_head] + snake.old_body[:-1]

    # ── RULE 2: Reduce health ────────────────────────────────────────────────
    # Each snake loses 1 health per turn, before food and hazard checks.
    for snake in internal_snakes:
        snake.health -= 1

    # ── RULE 3: Apply food (health reset & food removal) ─────────────────────
    # If snake's new head landed on food, reset health to 100.
    for snake in internal_snakes:
        if snake.ate_food:
            snake.health = 100

    # Remove eaten food from board
    remaining_food = [f for f in food_list if f not in eaten_foods]

    # ── RULE 4: Apply hazards (if used) ──────────────────────────────────────
    # If snake's new head lands on a hazard cell, apply extra hazard damage.
    # Food overrides hazard damage (if snake landed on food, hazard damage is not applied).
    for snake in internal_snakes:
        if len(snake.new_body) == 0:
            continue
        if snake.new_head in hazard_set:
            if snake.ate_food:
                # Food overrides hazard damage
                continue
            snake.health -= hazard_damage
            if snake.health < 0:
                snake.health = 0

    # ── RULES 5, 6, 7, 8, 9, 10: Check Eliminations Simultaneously ───────────
    # All checks are based on the state before any elimination is applied.
    eliminations: Dict[int, Tuple[str, str]] = {}  # snake_index -> (cause, eliminated_by)

    for i, snake in enumerate(internal_snakes):
        if len(snake.new_body) == 0:
            eliminations[i] = (ELIMINATED_BY_OUT_OF_HEALTH, "")
            continue

        # RULE 5: Check starvation (health <= 0)
        if snake.health <= 0:
            cause = ELIMINATED_BY_HAZARD if (snake.new_head in hazard_set and not snake.ate_food) else ELIMINATED_BY_OUT_OF_HEALTH
            eliminations[i] = (cause, "")
            continue

        # RULE 6: Check out-of-bounds
        hx, hy = snake.new_head
        if hx < 0 or hx >= width or hy < 0 or hy >= height:
            eliminations[i] = (ELIMINATED_BY_OUT_OF_BOUNDS, "")
            continue

        # RULE 7: Check self-collision
        # - Hit own neck (immediate 180° reversal into neck):
        hit_neck = len(snake.old_body) >= 2 and snake.new_head == snake.old_body[1]
        # - Hit own body segments (excluding tail that moved away, already reflected in new_body):
        hit_own_body = snake.new_head in snake.new_body[1:]
        if hit_neck or hit_own_body:
            eliminations[i] = (ELIMINATED_BY_SELF_COLLISION, snake.id)
            continue

        # RULE 9: Check head-to-head collision
        # "If two snakes' new heads land on the same cell:
        #  - The shorter snake is eliminated.
        #  - If both snakes are the same length, both are eliminated.
        #  - This overrides body-collision checks for that cell."
        head_collided = False
        for j, other in enumerate(internal_snakes):
            if i != j and len(other.new_body) > 0 and snake.new_head == other.new_head:
                head_collided = True
                if len(snake.new_body) <= len(other.new_body):
                    eliminations[i] = (ELIMINATED_BY_HEAD_COLLISION, other.id)
                    break
        if head_collided:
            continue

        # RULE 8: Check other-snake body collision
        # "Any snake whose new head hits another snake's body (any segment except that
        #  other snake's head) is eliminated."
        for j, other in enumerate(internal_snakes):
            if i != j and len(other.new_body) > 1 and snake.new_head in other.new_body[1:]:
                eliminations[i] = (ELIMINATED_BY_COLLISION, other.id)
                break

    # RULE 10: Apply all eliminations simultaneously
    for i, (cause, elim_by) in eliminations.items():
        internal_snakes[i].eliminated = True
        internal_snakes[i].eliminated_cause = cause
        internal_snakes[i].eliminated_by = elim_by

    # ── Output Construction ──────────────────────────────────────────────────
    alive_snakes_out: List[Dict[str, Any]] = []
    eliminated_snakes_out: List[Dict[str, Any]] = []
    all_snakes_out: List[Dict[str, Any]] = []

    for snake in internal_snakes:
        s_dict = copy.deepcopy(snake.raw_snake)
        s_dict["id"] = snake.id
        s_dict["health"] = snake.health
        formatted_body = [format_coord(pt, snake.as_dict) for pt in snake.new_body]
        s_dict["body"] = formatted_body
        s_dict["head"] = formatted_body[0] if formatted_body else None
        s_dict["length"] = len(formatted_body)

        s_dict["eliminated"] = snake.eliminated
        s_dict["eliminated_cause"] = snake.eliminated_cause
        s_dict["eliminated_by"] = snake.eliminated_by

        all_snakes_out.append(s_dict)
        if snake.eliminated:
            eliminated_snakes_out.append(s_dict)
        else:
            alive_snakes_out.append(s_dict)

    # Build updated board state
    new_board_state: Dict[str, Any] = copy.deepcopy(raw_board)
    new_board_state["width"] = width
    new_board_state["height"] = height
    formatted_food = [format_coord(f, as_dict_board) for f in remaining_food]
    new_board_state["food"] = formatted_food
    if "food_positions" in new_board_state:
        new_board_state["food_positions"] = formatted_food
    formatted_hazards = [format_coord(h, as_dict_board) for h in hazard_list]
    new_board_state["hazards"] = formatted_hazards
    if "hazard_positions" in new_board_state:
        new_board_state["hazard_positions"] = formatted_hazards

    new_board_state["snakes"] = alive_snakes_out
    new_board_state["eliminated_snakes"] = eliminated_snakes_out

    if "turn" in new_board_state and isinstance(new_board_state["turn"], int):
        new_board_state["turn"] += 1

    final_snakes = all_snakes_out if return_all_snakes else alive_snakes_out
    return new_board_state, final_snakes


# Convenience aliases
simulate_game_turn = simulate_turn
forward_simulate = simulate_turn
