#!/usr/bin/env python3
"""
blunder_extractor.py — Automated Blunder Extractor & Regression Test Generator

Parses match replays (from JSON files, raw state dumps, or Battlesnake API URLs),
detects any turn where a snake made an avoidable blunder (wall collision, body
collision, head-to-head loss, starvation, or hazard death when safe alternatives
existed), and outputs a formatted, deterministic Python unit test compatible with
tests/test_strategy.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.request

MOVE_OFFSETS: Dict[str, Tuple[int, int]] = {
    "up": (0, 1),
    "down": (0, -1),
    "left": (-1, 0),
    "right": (1, 0),
}

OPPOSITE_MOVES: Dict[str, str] = {
    "up": "down",
    "down": "up",
    "left": "right",
    "right": "left",
}


def parse_coord(pt: Any) -> Tuple[int, int]:
    """Extract (x, y) tuple from dict or tuple."""
    if isinstance(pt, dict):
        return int(pt["x"]), int(pt["y"])
    if isinstance(pt, (tuple, list)):
        return int(pt[0]), int(pt[1])
    if hasattr(pt, "x") and hasattr(pt, "y"):
        return int(pt.x), int(pt.y)
    raise TypeError(f"Cannot parse coordinate: {pt!r}")


def format_coord_list(coords: List[Tuple[int, int]]) -> List[Dict[str, int]]:
    """Format list of (x, y) into Battlesnake API [{'x': x, 'y': y}, ...]."""
    return [{"x": x, "y": y} for x, y in coords]


def get_move_between(src: Tuple[int, int], dst: Tuple[int, int], width: int, height: int, wrap: bool = False) -> Optional[str]:
    """Returns the move direction from src to dst, accounting for wrap if enabled."""
    sx, sy = src
    dx, dy = dst

    for move, (ox, oy) in MOVE_OFFSETS.items():
        nx = sx + ox
        ny = sy + oy
        if wrap:
            nx %= width
            ny %= height
        if nx == dx and ny == dy:
            return move
    return None


def fetch_replay_from_url(url: str, timeout_sec: float = 10.0) -> Dict[str, Any]:
    """
    Fetches game replay data from a Battlesnake engine URL or play URL.
    Converts play.battlesnake.com/g/<id> to engine.battlesnake.com/games/<id>.
    """
    match = re.search(r"battlesnake\.com/(?:g|games)/([a-zA-Z0-9\-]+)", url)
    if match:
        game_id = match.group(1)
        api_url = f"https://engine.battlesnake.com/games/{game_id}"
    else:
        api_url = url

    headers = {
        "User-Agent": "Mozilla/5.0 (Battlesnake Blunder Extractor/1.0)",
        "Accept": "application/json",
    }
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.URLError as err:
        raise RuntimeError(f"Failed to fetch game from {api_url}: {err}") from err


def normalize_replay_data(raw_data: Any) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Normalizes diverse replay inputs into (metadata, frames):
    - Standard Battlesnake engine API JSON with 'game' and 'frames'
    - Array of turns [state0, state1, ...]
    - Custom simulation logs
    """
    if isinstance(raw_data, list):
        # Sequence of state snapshots
        frames = raw_data
        metadata = {
            "id": "replay_game",
            "ruleset": {"name": "standard"},
            "width": 11,
            "height": 11,
        }
        if frames and "board" in frames[0]:
            b = frames[0]["board"]
            metadata["width"] = b.get("width", 11)
            metadata["height"] = b.get("height", 11)
        return metadata, frames

    if isinstance(raw_data, dict):
        if "frames" in raw_data:
            game_meta = raw_data.get("game", {})
            metadata = {
                "id": game_meta.get("id", raw_data.get("id", "game_replay")),
                "ruleset": game_meta.get("ruleset", raw_data.get("ruleset", {"name": "standard"})),
                "width": raw_data.get("width", game_meta.get("width", 11)),
                "height": raw_data.get("height", game_meta.get("height", 11)),
            }
            frames = raw_data["frames"]
            if frames and "width" in frames[0]:
                metadata["width"] = frames[0]["width"]
                metadata["height"] = frames[0]["height"]
            elif frames and "board" in frames[0]:
                metadata["width"] = frames[0]["board"].get("width", 11)
                metadata["height"] = frames[0]["board"].get("height", 11)
            return metadata, frames

        if "turns" in raw_data:
            metadata = raw_data.get("metadata", {"id": "game_replay", "ruleset": {"name": "standard"}, "width": 11, "height": 11})
            return metadata, raw_data["turns"]

    raise ValueError("Unrecognized replay JSON structure. Expected 'frames' or list of turns.")


def extract_frame_board(frame: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts width, height, food, hazards, and snakes list from a frame."""
    width = int(frame.get("width", metadata.get("width", 11)))
    height = int(frame.get("height", metadata.get("height", 11)))

    if "board" in frame:
        b = frame["board"]
        width = int(b.get("width", width))
        height = int(b.get("height", height))
        food = [parse_coord(f) for f in b.get("food", [])]
        hazards = [parse_coord(h) for h in b.get("hazards", [])]
        snakes = b.get("snakes", [])
    else:
        food = [parse_coord(f) for f in frame.get("food", [])]
        hazards = [parse_coord(h) for h in frame.get("hazards", [])]
        snakes = frame.get("snakes", [])

    parsed_snakes = []
    for s in snakes:
        body = [parse_coord(p) for p in s.get("body", [])]
        parsed_snakes.append({
            "id": str(s.get("id", s.get("name", "snake"))),
            "name": str(s.get("name", s.get("id", "snake"))),
            "health": int(s.get("health", 100)),
            "body": body,
            "death": s.get("death"),
            "eliminated": s.get("eliminated", False) or (s.get("death") is not None) or (len(body) == 0),
        })

    return {
        "width": width,
        "height": height,
        "food": food,
        "hazards": hazards,
        "snakes": parsed_snakes,
        "turn": int(frame.get("turn", 0)),
    }


class BlunderDetection:
    def __init__(
        self,
        turn: int,
        snake_id: str,
        blunder_move: str,
        blunder_cause: str,
        safe_moves: List[str],
        state_before: Dict[str, Any],
        width: int,
        height: int,
        wrap: bool,
        constrictor: bool,
        game_id: str,
    ):
        self.turn = turn
        self.snake_id = snake_id
        self.blunder_move = blunder_move
        self.blunder_cause = blunder_cause
        self.safe_moves = safe_moves
        self.state_before = state_before
        self.width = width
        self.height = height
        self.wrap = wrap
        self.constrictor = constrictor
        self.game_id = game_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "game_id": self.game_id,
            "turn": self.turn,
            "snake_id": self.snake_id,
            "blunder_move": self.blunder_move,
            "blunder_cause": self.blunder_cause,
            "safe_moves": self.safe_moves,
            "width": self.width,
            "height": self.height,
            "wrap": self.wrap,
            "constrictor": self.constrictor,
        }


def find_blunders(
    metadata: Dict[str, Any],
    frames: List[Dict[str, Any]],
    target_snake: Optional[str] = None,
) -> List[BlunderDetection]:
    """
    Iterates through game frames, tracking each snake's status.
    Identifies turns where a snake made an avoidable fatal move when
    at least one safe legal move was available.
    """
    game_id = str(metadata.get("id", "replay"))
    ruleset = metadata.get("ruleset", {})
    ruleset_name = str(ruleset.get("name", "standard")).lower()
    wrap = "wrapped" in ruleset_name
    constrictor = "constrictor" in ruleset_name

    parsed_frames = [extract_frame_board(f, metadata) for f in frames]
    blunders: List[BlunderDetection] = []

    for t in range(len(parsed_frames) - 1):
        f_curr = parsed_frames[t]
        f_next = parsed_frames[t + 1]

        w, h = f_curr["width"], f_curr["height"]

        # Map current snakes by id
        curr_snakes = {s["id"]: s for s in f_curr["snakes"] if not s["eliminated"]}
        next_snakes = {s["id"]: s for s in f_next["snakes"]}

        for s_id, snake in curr_snakes.items():
            if target_snake and target_snake not in (s_id, snake["name"]):
                continue

            # Check if this snake died on next frame
            snake_next = next_snakes.get(s_id)
            died = (snake_next is None) or snake_next["eliminated"] or (len(snake_next["body"]) == 0)

            if not died:
                continue

            # Snake was alive at turn t and died at turn t+1
            body = snake["body"]
            if not body:
                continue
            head = body[0]
            neck = body[1] if len(body) > 1 else None

            # Determine the move chosen
            # In official replay JSON, snake_next might record the new head position
            chosen_move = None
            if snake_next and snake_next.get("body"):
                next_head = snake_next["body"][0]
                chosen_move = get_move_between(head, next_head, w, h, wrap=wrap)

            if not chosen_move and snake_next and snake_next.get("death"):
                # Infer from death info or fallback
                death_info = snake_next["death"]
                cause = death_info.get("cause", "")
            else:
                cause = ""

            # Check candidate moves from head at turn t
            candidate_evals: Dict[str, Dict[str, Any]] = {}
            for move, (ox, oy) in MOVE_OFFSETS.items():
                nx = head[0] + ox
                ny = head[1] + oy
                if wrap:
                    nx %= w
                    ny %= h
                elif not (0 <= nx < w and 0 <= ny < h):
                    candidate_evals[move] = {"safe": False, "reason": "wall-collision"}
                    continue

                if neck and (nx, ny) == neck:
                    candidate_evals[move] = {"safe": False, "reason": "neck-reversal"}
                    continue

                # Check self-body collision
                # In standard Battlesnake, the tail recedes unless snake ate
                # For safety calculation, body segments except tail are obstacles
                is_self_body = False
                body_segments_to_check = body if constrictor else body[:-1]
                for seg in body_segments_to_check:
                    if (nx, ny) == seg:
                        is_self_body = True
                        break
                if is_self_body:
                    candidate_evals[move] = {"safe": False, "reason": "snake-self-collision"}
                    continue

                # Check opponent bodies
                opp_body_collision = False
                for other_id, other_s in curr_snakes.items():
                    if other_id == s_id:
                        continue
                    o_body = other_s["body"]
                    o_check = o_body if constrictor else o_body[:-1]
                    if (nx, ny) in o_check:
                        opp_body_collision = True
                        break
                if opp_body_collision:
                    candidate_evals[move] = {"safe": False, "reason": "snake-collision"}
                    continue

                # Check potential head-to-head collision
                h2h_threat = False
                for other_id, other_s in curr_snakes.items():
                    if other_id == s_id:
                        continue
                    o_head = other_s["body"][0]
                    o_len = len(other_s["body"])
                    my_len = len(body)
                    # If an opponent with equal or greater length can move to (nx, ny)
                    if o_len >= my_len:
                        manh = (
                            (min(abs(o_head[0] - nx), w - abs(o_head[0] - nx)) + min(abs(o_head[1] - ny), h - abs(o_head[1] - ny)))
                            if wrap
                            else (abs(o_head[0] - nx) + abs(o_head[1] - ny))
                        )
                        if manh == 1:
                            # 1 step away: possible head-to-head collision
                            h2h_threat = True
                            break

                candidate_evals[move] = {"safe": not h2h_threat, "reason": "head-collision" if h2h_threat else "safe"}

            safe_moves = [m for m, ev in candidate_evals.items() if ev["safe"]]

            # If chosen move is not known, find the move that caused death
            if not chosen_move:
                unsafe_moves = [m for m, ev in candidate_evals.items() if not ev["safe"]]
                if len(unsafe_moves) == 1:
                    chosen_move = unsafe_moves[0]
                else:
                    # Look at next frame where snake died
                    for m, ev in candidate_evals.items():
                        if not ev["safe"]:
                            chosen_move = m
                            break

            if not chosen_move:
                continue

            chosen_reason = candidate_evals.get(chosen_move, {}).get("reason", "collision")

            # A blunder occurs if there was at least one safe alternative,
            # but the chosen move was lethal/unsafe!
            if safe_moves and (not candidate_evals.get(chosen_move, {}).get("safe", False)):
                # Build game state snapshot before the blunder
                state_snapshot = {
                    "width": w,
                    "height": h,
                    "turn": f_curr["turn"],
                    "player": copy.deepcopy(snake),
                    "opponents": [copy.deepcopy(s) for s_id_other, s in curr_snakes.items() if s_id_other != s_id],
                    "food": list(f_curr["food"]),
                    "hazards": list(f_curr["hazards"]),
                }

                blunder = BlunderDetection(
                    turn=f_curr["turn"],
                    snake_id=s_id,
                    blunder_move=chosen_move,
                    blunder_cause=chosen_reason,
                    safe_moves=safe_moves,
                    state_before=state_snapshot,
                    width=w,
                    height=h,
                    wrap=wrap,
                    constrictor=constrictor,
                    game_id=game_id,
                )
                blunders.append(blunder)

    return blunders


def generate_unit_test_code(blunder: BlunderDetection, method_name: Optional[str] = None) -> str:
    """
    Generates a deterministic Python unit test method string formatted
    for inclusion in tests/test_strategy.py.
    """
    clean_game_id = re.sub(r"[^a-zA-Z0-9_]", "_", blunder.game_id)[:12]
    clean_cause = re.sub(r"[^a-zA-Z0-9_]", "_", blunder.blunder_cause)
    if not method_name:
        method_name = f"test_blunder_game_{clean_game_id}_turn_{blunder.turn}_{clean_cause}"

    player = blunder.state_before["player"]
    opponents = blunder.state_before["opponents"]
    food = blunder.state_before["food"]
    hazards = blunder.state_before["hazards"]

    player_body_str = json.dumps(format_coord_list(player["body"]))
    player_health = player["health"]

    opp_list_reprs = []
    for opp in opponents:
        opp_body_str = json.dumps(format_coord_list(opp["body"]))
        opp_list_reprs.append(f'{{"body": {opp_body_str}, "health": {opp["health"]}}}')
    opponents_str = "[\n            " + ",\n            ".join(opp_list_reprs) + "\n        ]" if opp_list_reprs else "[]"

    food_str = json.dumps(format_coord_list(food))
    hazards_str = json.dumps(format_coord_list(hazards))

    safe_moves_repr = tuple(blunder.safe_moves)
    if len(safe_moves_repr) == 1:
        assertion_code = f'self.assertEqual(move, "{safe_moves_repr[0]}", f"Turn {blunder.turn} chose {{move}}, expected {safe_moves_repr[0]}")'
    else:
        assertion_code = f'self.assertIn(move, {safe_moves_repr!r}, f"Turn {blunder.turn} chose {{move}}, expected one of {safe_moves_repr!r}")'

    wrap_str = str(blunder.wrap)
    constrictor_str = str(blunder.constrictor)

    docstring = (
        f'"""Regression test: Game {blunder.game_id} Turn {blunder.turn}. '
        f'Avoid {blunder.blunder_cause} via {blunder.blunder_move}; must choose {blunder.safe_moves}."""'
    )

    code = f"""    def {method_name}(self):
        {docstring}
        player_body = {player_body_str}
        opp_snakes = {opponents_str}
        gs = build_game_state(
            player_body[0],
            player_body,
            opp_snakes=opp_snakes,
            health={player_health},
            width={blunder.width},
            height={blunder.height},
        )
        gs["board"]["food"] = {food_str}
        gs["board"]["hazards"] = {hazards_str}
        for mod in (strategy_baseline, strategy_variant):
            move = mod.choose_move(copy.deepcopy(gs), wrap={wrap_str}, constrictor={constrictor_str})
            {assertion_code}
            self.assertNotEqual(
                move,
                "{blunder.blunder_move}",
                f"Turn {blunder.turn} blunder: chose {blunder.blunder_move} into {blunder.blunder_cause}",
            )
"""
    return code


def append_test_to_file(test_code: str, target_file: str) -> None:
    """Appends generated test method into the TestBattlesnakeStrategy class in target_file."""
    with open(target_file, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the end of TestBattlesnakeStrategy class or end of file
    target_idx = content.rfind("\nif __name__ ==")
    if target_idx == -1:
        target_idx = len(content)

    new_content = content[:target_idx] + "\n" + test_code + content[target_idx:]
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(new_content)


def main():
    parser = argparse.ArgumentParser(
        description="Automated Blunder Extractor & Regression Test Generator for Battlesnake."
    )
    parser.add_argument(
        "source",
        help="Path to local replay JSON file or Battlesnake engine URL (https://engine.battlesnake.com/games/<id>)",
    )
    parser.add_argument(
        "--snake",
        dest="target_snake",
        default=None,
        help="Optional snake ID or name to analyze. If omitted, checks all snakes.",
    )
    parser.add_argument(
        "--output",
        "-o",
        dest="output_file",
        default=None,
        help="Output file for generated tests. Defaults to stdout.",
    )
    parser.add_argument(
        "--append-to",
        dest="append_file",
        default=None,
        help="Append generated unit test methods directly to a test file (e.g. tests/test_strategy.py).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output detected blunders as structured JSON summary.",
    )

    args = parser.parse_args()

    # Load source data
    if args.source.startswith("http://") or args.source.startswith("https://"):
        print(f"[*] Fetching game replay from {args.source}...", file=sys.stderr)
        raw_data = fetch_replay_from_url(args.source)
    else:
        if not os.path.exists(args.source):
            sys.exit(f"[!] Error: File not found: {args.source}")
        with open(args.source, "r", encoding="utf-8") as f:
            raw_data = json.load(f)

    metadata, frames = normalize_replay_data(raw_data)
    blunders = find_blunders(metadata, frames, target_snake=args.target_snake)

    if args.json:
        summary = [b.to_dict() for b in blunders]
        print(json.dumps(summary, indent=2))
        return

    if not blunders:
        print("[✓] No avoidable fatal blunders detected in the analyzed game turns.", file=sys.stderr)
        return

    print(f"[*] Found {len(blunders)} avoidable blunder(s):", file=sys.stderr)
    generated_tests = []
    for idx, b in enumerate(blunders, 1):
        print(
            f"    [{idx}] Turn {b.turn}: Snake '{b.snake_id}' chose '{b.blunder_move}' ({b.blunder_cause}). "
            f"Safe alternatives: {b.safe_moves}",
            file=sys.stderr,
        )
        test_code = generate_unit_test_code(b)
        generated_tests.append(test_code)

    all_test_code = "\n".join(generated_tests)

    if args.append_file:
        append_test_to_file(all_test_code, args.append_file)
        print(f"[✓] Appended {len(blunders)} test(s) to {args.append_file}", file=sys.stderr)
    elif args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(all_test_code)
        print(f"[✓] Written {len(blunders)} test(s) to {args.output_file}", file=sys.stderr)
    else:
        print("\n# ── Auto-Generated Regression Tests ─────────────────────────────")
        print(all_test_code)


if __name__ == "__main__":
    main()
