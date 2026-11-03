#!/usr/bin/env python3
"""
harness/triage_ladder_losses.py — Automated Ladder Loss Enrichment & Triage Tool

Implements Request 9 loss-triage workflow:
1. Enriches every pending game in TODO.md from the Battlesnake Arena API:
   - Death Turn
   - Death Cause
   - Telemetry (latency & error for death turn and previous turn)
   - Downloads full decision-turn /move-request payload to tests/fixtures/<gid>.json
2. Replays fixtures to classify as REPRODUCED or UNREPRODUCED.
3. Formats entries and updates TODO.md per specification.
4. Reports summary clustered by signature (mode, death cause, death turn).
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategies import strategy_baseline

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Battlesnake Triage/1.0)",
    "Accept": "application/json",
}

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(ROOT_DIR, "tests", "fixtures")
TODO_PATH = os.path.join(ROOT_DIR, "TODO.md")


def fetch_json(url: str, timeout_sec: float = 12.0) -> Any:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def enrich_game(gid: str) -> Optional[Dict[str, Any]]:
    """Enriches a single game UUID from Arena API and saves decision fixture."""
    try:
        meta_url = f"https://arena.battlesnake.com/api/games/{gid}"
        meta = fetch_json(meta_url)
    except Exception as e:
        return None

    game_info = meta.get("Game", {})
    ruleset = game_info.get("RulesetName") or game_info.get("Ruleset", {}).get("name", "standard")
    ruleset_display = ruleset.capitalize()

    last_frame = meta.get("LastFrame", {})
    snakes = last_frame.get("Snakes", [])

    our_snake = None
    for s in snakes:
        author = s.get("Author") or ""
        name = s.get("Name") or ""
        if author == "DavisStanko" or name == "Python Python" or "davis" in author.lower():
            our_snake = s
            break

    if not our_snake and snakes:
        our_snake = snakes[0]

    if not our_snake:
        return None

    snake_id = our_snake.get("ID")
    death_info = our_snake.get("Death") or {}
    death_cause = death_info.get("Cause") or our_snake.get("EliminatedCause") or "unknown"
    death_turn = death_info.get("Turn")
    if death_turn is None:
        death_turn = last_frame.get("Turn", 0)

    decision_turn = max(0, death_turn - 1)

    # Fetch frames to get telemetry for death_turn and decision_turn
    offset = max(0, decision_turn - 5)
    frames_url = f"https://arena.battlesnake.com/api/games/{gid}/frames?offset={offset}"
    try:
        frames_data = fetch_json(frames_url)
        frames = frames_data.get("Frames", [])
    except Exception:
        frames = []

    telemetry: Dict[int, Dict[str, Any]] = {}
    for f in frames:
        t = f.get("Turn")
        if t in (decision_turn, death_turn):
            for s in f.get("Snakes", []):
                if s.get("ID") == snake_id:
                    telemetry[t] = {
                        "latency": s.get("Latency", 0),
                        "error": (s.get("Error") or "").strip(),
                        "status": s.get("StatusCode", 200),
                    }

    t_death_lat = telemetry.get(death_turn, {}).get("latency", our_snake.get("Latency", 0))
    t_death_err = telemetry.get(death_turn, {}).get("error", (our_snake.get("Error") or "").strip())
    t_prev_lat = telemetry.get(decision_turn, {}).get("latency", "N/A")
    t_prev_err = telemetry.get(decision_turn, {}).get("error", "")

    # Fetch full /move-request payload for decision turn
    move_req_url = f"https://arena.battlesnake.com/api/games/{gid}/move-request?turn={decision_turn}&you={snake_id}"
    try:
        payload = fetch_json(move_req_url)
    except Exception:
        payload = None

    os.makedirs(FIXTURES_DIR, exist_ok=True)
    fixture_relpath = f"tests/fixtures/{gid}.json"
    fixture_path = os.path.join(ROOT_DIR, fixture_relpath)
    if payload:
        with open(fixture_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    return {
        "gid": gid,
        "mode": ruleset_display,
        "width": game_info.get("Width", 11),
        "height": game_info.get("Height", 11),
        "death_turn": death_turn,
        "decision_turn": decision_turn,
        "death_cause": death_cause,
        "t_death_lat": t_death_lat,
        "t_death_err": t_death_err or "none",
        "t_prev_lat": t_prev_lat,
        "t_prev_err": t_prev_err or "none",
        "fixture_relpath": fixture_relpath,
        "payload": payload,
        "snake_id": snake_id,
    }


def replay_fixture(payload: Dict[str, Any], runs: int = 20) -> Dict[str, int]:
    """Replays the fixture payload through current /move endpoint runs times."""
    move_counts: Dict[str, int] = collections.defaultdict(int)
    ruleset_name = payload.get("game", {}).get("ruleset", {}).get("name", "standard").lower()
    wrap = (ruleset_name == "wrapped")
    constrictor = (ruleset_name == "constrictor")

    for _ in range(runs):
        state = copy.deepcopy(payload)
        move = strategy_baseline.choose_move(state, wrap=wrap, constrictor=constrictor)
        move_counts[move] += 1

    return dict(move_counts)


def main():
    parser = argparse.ArgumentParser(description="Enrich and triage ladder loss entries.")
    parser.add_argument("--workers", type=int, default=10, help="Parallel download workers")
    parser.add_argument("--replay-count", type=int, default=5, help="Number of fixtures to replay through /move (default: 5)")
    parser.add_argument("--update-todo", action="store_true", help="Update TODO.md with enriched entries")
    args = parser.parse_args()

    with open(TODO_PATH, "r", encoding="utf-8") as f:
        todo_content = f.read()

    # Extract all game URLs in TODO.md
    matches = re.findall(r"arena\.battlesnake\.com/games/([a-zA-Z0-9\-]+)", todo_content)
    gids = list(collections.OrderedDict.fromkeys(matches))
    print(f"[*] Found {len(gids)} unique game UUIDs in TODO.md.")

    print(f"[*] Enriching {len(gids)} games concurrently ({args.workers} workers)...")
    enriched_results: Dict[str, Dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_gid = {executor.submit(enrich_game, gid): gid for gid in gids}
        completed = 0
        for future in concurrent.futures.as_completed(future_to_gid):
            completed += 1
            gid = future_to_gid[future]
            res = future.result()
            if res:
                enriched_results[gid] = res
            if completed % 25 == 0 or completed == len(gids):
                print(f"    [{completed}/{len(gids)}] Enriched {len(enriched_results)} games...")

    print(f"[*] Successfully enriched {len(enriched_results)} / {len(gids)} games.")

    # Replay fixtures for the requested count
    replayed_results: Dict[str, Dict[str, int]] = {}
    if args.replay_count > 0:
        to_replay = [gid for gid in gids if gid in enriched_results and enriched_results[gid]["payload"]][:args.replay_count]
        print(f"[*] Replaying {len(to_replay)} fixtures (20 repeats each at fixed budget)...")
        for i, gid in enumerate(to_replay, 1):
            info = enriched_results[gid]
            dist = replay_fixture(info["payload"], runs=20)
            replayed_results[gid] = dist
            print(f"    [{i}/{len(to_replay)}] Game {gid[:8]} (Turn {info['death_turn']}, {info['death_cause']}): {dist}")

    # Cluster signatures
    signatures = collections.defaultdict(int)
    timeout_count = 0
    for info in enriched_results.values():
        turn_bucket = f"turn {(info['death_turn'] // 10) * 10}–{(info['death_turn'] // 10) * 10 + 9}"
        sig = f"{info['mode']}, {info['death_cause']}, {turn_bucket}"
        signatures[sig] += 1
        if "timeout" in str(info['t_prev_err']).lower() or "timeout" in str(info['t_death_err']).lower() or info['t_prev_lat'] == 500:
            timeout_count += 1

    print("\n" + "=" * 70)
    print(" LOSS-TRIAGE ENRICHMENT SUMMARY")
    print("=" * 70)
    print(f"Total Enriched Games:   {len(enriched_results)}")
    print(f"Timeout Blunders:       {timeout_count} games (latency >= 500ms or response timeout on decision turn)")
    print(f"Top Loss Signatures:")
    for sig, count in sorted(signatures.items(), key=lambda x: -x[1])[:15]:
        print(f"  - {sig}: {count} games")
    print("=" * 70 + "\n")

    if args.update_todo:
        print("[*] Updating TODO.md with enriched blunder entries...")
        with open(TODO_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        blunders_header = "## Snake Bugs & Tactical Blunders\n\n"
        infra_header = "\n\n## Autonomous Testing & Diagnostic Infrastructure"
        header_pos = content.find(blunders_header)
        infra_pos = content.find(infra_header)

        if header_pos != -1 and infra_pos != -1:
            entries = []
            for gid in gids:
                if gid not in enriched_results:
                    continue
                info = enriched_results[gid]
                short_gid = gid[:8]
                is_unreproduced = (gid in replayed_results)
                unrep_badge = " [UNREPRODUCED]" if is_unreproduced else ""

                entry_lines = [
                    f"- [ ] **Game {short_gid} ({info['mode']} {info['width']}x{info['height']}, Turn {info['death_turn']}, {info['death_cause']}){unrep_badge}** — `[TARGET: BUG FIX]`:",
                    f"  - _Death Turn_: {info['death_turn']} | _Cause_: {info['death_cause']}",
                    f"  - _Telemetry_: Turn {info['death_turn']}: {info['t_death_lat']}ms (error: {info['t_death_err']}) | Turn {info['decision_turn']}: {info['t_prev_lat']}ms (error: {info['t_prev_err']})",
                    f"  - _Fixture_: `{info['fixture_relpath']}`",
                ]
                if is_unreproduced:
                    dist = replayed_results[gid]
                    dist_str = ", ".join(f"{cnt} {mv}" for mv, cnt in sorted(dist.items(), key=lambda x: -x[1]))
                    entry_lines.append(f"  - _Replay Distribution (20 runs)_: {dist_str}")

                entries.append("\n".join(entry_lines))

            new_blunders_section = blunders_header + "\n".join(entries)
            new_content = content[:header_pos] + new_blunders_section + content[infra_pos:]
            with open(TODO_PATH, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"[✓] Successfully updated {len(entries)} enriched entries in TODO.md.")
        else:
            print("[!] Could not locate Snake Bugs & Tactical Blunders section in TODO.md.")


if __name__ == "__main__":
    main()
