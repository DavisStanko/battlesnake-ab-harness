"""
zoo_adapter.py — Adapter strategy bridging Battlesnake test harness to a Snake Zoo container.

Implements the standard harness strategy interface:
    choose_move(game_state: dict, wrap: bool = False, constrictor: bool = False) -> str
and
    move(game_state: dict) -> dict

POSTs game_state as JSON to a configurable snake /move endpoint.
Sets game.timeout = 250 in game_state before sending.
Falls back to "up" if the POST fails, returns non-200, or times out.
"""

from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

INFO: Dict[str, str] = {
    "apiversion": "1",
    "author": "Snake Zoo Adapter",
    "color": "#8855bb",
    "head": "default",
    "tail": "default",
}

# Configurable target URL. Can be set via environment variable ZOO_SNAKE_URL,
# or modified at runtime via set_target_url().
DEFAULT_TARGET_URL = os.getenv("ZOO_SNAKE_URL", "http://localhost:8000")
TARGET_URL = DEFAULT_TARGET_URL
SLUG_URL_MAP: Dict[str, str] = {}

# Timeout in seconds for HTTP POST request (allowing generous overhead for network round-trip beyond game.timeout ms)
REQUEST_TIMEOUT_SEC = float(os.getenv("ZOO_REQUEST_TIMEOUT_SEC", "0.5"))


def set_target_url(url: str) -> None:
    """Set the target URL for the snake under test."""
    global TARGET_URL
    TARGET_URL = url


def get_target_url() -> str:
    """Get the target URL."""
    return TARGET_URL


def set_slug_urls(mapping: Dict[str, str]) -> None:
    """Set mapping of snake slug to target URL."""
    global SLUG_URL_MAP
    SLUG_URL_MAP = dict(mapping)


def register_pool(pool: list) -> None:
    """Register a list of {slug, url} dicts."""
    global SLUG_URL_MAP
    for item in pool:
        if isinstance(item, dict):
            slug = item.get("slug")
            url = item.get("url")
            if slug and url:
                SLUG_URL_MAP[slug] = url


def get_url_for_snake(name_or_id: str) -> str:
    """Resolve target URL for a snake by name, id, or slug."""
    if name_or_id in SLUG_URL_MAP:
        return SLUG_URL_MAP[name_or_id]
    base = name_or_id.rsplit("_", 1)[0]
    if base in SLUG_URL_MAP:
        return SLUG_URL_MAP[base]
    for slug, url in SLUG_URL_MAP.items():
        if name_or_id.startswith(slug):
            return url
    return TARGET_URL


def get_move_endpoint(base_url: str) -> str:
    """Ensure the URL points to the /move endpoint."""
    return get_endpoint(base_url, "move")


def get_endpoint(base_url: str, action: str = "move") -> str:
    """Ensure the URL points to the requested action endpoint."""
    url = base_url.rstrip("/")
    if url.endswith(f"/{action}"):
        return url
    return f"{url}/{action}"


def _ensure_game_settings(game_state: Dict[str, Any]) -> None:
    """Ensure game timeout and ruleset settings are populated."""
    if "game" not in game_state or not isinstance(game_state["game"], dict):
        game_state["game"] = {}
    game_state["game"]["timeout"] = 250

    ruleset = game_state["game"].get("ruleset")
    if not isinstance(ruleset, dict):
        ruleset = {"name": "standard", "version": "v1.2.3"}
    settings = ruleset.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    settings.setdefault("foodSpawnChance", 15)
    settings.setdefault("minimumFood", 1)
    settings.setdefault("hazardDamagePerTurn", 14)
    settings.setdefault("royale", {"shrinkEveryNTurns": 25})
    settings.setdefault("squad", {
        "allowBodyCollisions": False,
        "sharedElimination": False,
        "sharedHealth": False,
        "sharedLength": False,
    })
    ruleset["settings"] = settings
    game_state["game"]["ruleset"] = ruleset


def start(game_state: Dict[str, Any]) -> None:
    """Send /start request to snake."""
    _ensure_game_settings(game_state)
    you_name = game_state.get("you", {}).get("name", "")
    you_id = game_state.get("you", {}).get("id", "")
    target = get_url_for_snake(you_name) if you_name else get_url_for_snake(you_id)
    endpoint = get_endpoint(target, "start")

    try:
        data = json.dumps(game_state).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "BattlesnakeHarnessAdapter/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            resp.read()
    except Exception as exc:
        logger.debug("Snake zoo adapter start request failed to %s: %s", endpoint, exc)


def choose_move(game_state: Dict[str, Any], wrap: bool = False, constrictor: bool = False) -> str:
    """
    POST game state to the target snake endpoint and return the move string.
    """
    _ensure_game_settings(game_state)

    you_name = game_state.get("you", {}).get("name", "")
    you_id = game_state.get("you", {}).get("id", "")
    target = get_url_for_snake(you_name) if you_name else get_url_for_snake(you_id)
    endpoint = get_endpoint(target, "move")

    try:
        data = json.dumps(game_state).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "BattlesnakeHarnessAdapter/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            if resp.status == 200:
                body = resp.read().decode("utf-8")
                res_json = json.loads(body)
                move = res_json.get("move")
                if isinstance(move, str) and move.strip():
                    return move.strip().lower()
    except Exception as exc:
        logger.debug("Snake zoo adapter request failed to %s: %s", endpoint, exc)

    return "up"


def move(game_state: Dict[str, Any]) -> Dict[str, str]:
    """Official Battlesnake /move HTTP handler contract."""
    ruleset = game_state.get("game", {}).get("ruleset", {})
    ruleset_name = str(ruleset.get("name", "standard")).lower()
    wrap = ruleset_name in ("wrapped", "wrap")
    constrictor = ruleset_name == "constrictor"
    return {"move": choose_move(game_state, wrap=wrap, constrictor=constrictor)}
