"""
ab_dashboard.py — Live Web Dashboard for Battlesnake A/B Testing

Serves a real-time dark-mode web interface displaying:
- Current Win Rate with a central Tug-of-War bar
- Active threads / workers being used
- Total runs progress (current / target)
- Next checkpoint and WR required to stop (early stopping thresholds)
- Configured Min/Max runs
- Multi-Mode Matrix pipeline & 4-Player Match Standings
- Recommendation banner (KEEP vs REVERT)
"""

import argparse
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Tuple, Dict, Any, Optional

def load_run_history() -> list:
    history_file = Path(__file__).parent / "ab_results" / "ab_history.json"
    if history_file.exists():
        try:
            with history_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict):
                            for key in ("baseline", "variant"):
                                sub = entry.get(key)
                                if isinstance(sub, dict):
                                    for num_k in ("wins", "losses", "draws"):
                                        if isinstance(sub.get(num_k), float):
                                            sub[num_k] = round(sub[num_k], 2)
                            if isinstance(entry.get("draws"), float):
                                entry["draws"] = round(entry["draws"], 2)
                    return data[-100:]
        except Exception:
            pass
    return []

class DashboardState:
    def __init__(
        self,
        strategy_baseline: str = "baseline",
        strategy_variant: str = "variant",
        min_games: int = 30,
        max_games: int = 200,
        check_interval: int = 1,
        early_stop_high: float = 35.0,
        early_stop_low: float = 18.0,
        threads: Optional[int] = None,
        target_games: int = 200,
        experiment_desc: str = "Unspecified Experiment",
        game_mode: str = "standard",
        num_snakes: int = 4,
    ):
        self.lock = threading.RLock()
        self.status = "RUNNING"
        self.stop_type = "IN_PROGRESS"
        self.stop_reason = ""
        self.active_pid: Optional[int] = None
        self.total_played = 0
        self.target_games = target_games
        self.min_games = min_games
        self.max_games = max_games
        self.check_interval = check_interval
        self.early_stop_high = early_stop_high
        self.early_stop_low = early_stop_low
        self.threads = threads
        self.strategy_baseline = strategy_baseline
        self.strategy_variant = strategy_variant
        self.experiment_desc = experiment_desc
        self.game_mode = game_mode
        self.num_snakes = num_snakes
        self.target_win_share = 25.0 if num_snakes == 4 else 50.0
        self.matrix_progress = []

        self.wins_baseline = 0
        self.losses_baseline = 0
        self.draws_baseline = 0

        self.wins_variant = 0
        self.losses_variant = 0
        self.draws_variant = 0

        self.snakes_stats = self._init_snakes_stats(num_snakes, strategy_variant, strategy_baseline)

        self.start_time = time.time()
        self.end_time: Optional[float] = None
        self.csv_path = ""
        self.recommendation = "PENDING"
        self.skip_requested: bool = False
        self.last_activity_time = time.time()
        self.last_game_time: float = time.time()

    def _init_snakes_stats(self, num_snakes: int, var_name: str, base_name: str) -> Dict[str, Dict[str, int]]:
        return {
            var_name: {"wins": 0, "losses": 0, "draws": 0},
            base_name: {"wins": 0, "losses": 0, "draws": 0},
        }

    def touch(self):
        with self.lock:
            self.last_activity_time = time.time()

    def handle_update(self, payload: dict):
        with self.lock:
            self.last_activity_time = time.time()
            action = payload.get("action")
            if action in ("init", "update_game", "update_matrix", "register_pid"):
                self.last_game_time = time.time()
            if action == "register_pid":
                self.active_pid = payload.get("pid")
            elif action == "init":
                self.active_pid = payload.get("pid", self.active_pid)
                self.strategy_baseline = payload.get("strategy_baseline", "baseline")
                self.strategy_variant = payload.get("strategy_variant", "variant")
                self.min_games = payload.get("min_games", 30)
                self.max_games = payload.get("max_games", 200)
                self.target_games = payload.get("max_games", 200)
                self.check_interval = payload.get("check_interval", 1)
                self.early_stop_high = payload.get("early_stop_high", 35.0)
                self.early_stop_low = payload.get("early_stop_low", 18.0)
                self.threads = payload.get("threads", self.threads)
                self.experiment_desc = payload.get("experiment_desc", "Unspecified Experiment")
                self.game_mode = payload.get("game_mode", "standard")
                self.num_snakes = payload.get("num_snakes", 4)
                self.target_win_share = payload.get("target_win_share", 25.0 if self.num_snakes == 4 else 50.0)
                self.matrix_progress = payload.get("matrix_progress", self.matrix_progress)
                if self.matrix_progress and isinstance(self.matrix_progress, list):
                    for item in self.matrix_progress:
                        if item.get("mode") == self.game_mode and item.get("status") not in ("PASSED", "PARITY", "FAILED", "SKIPPED"):
                            item["status"] = "RUNNING"
                self.status = "RUNNING"
                self.stop_type = "IN_PROGRESS"
                self.stop_reason = ""
                self.total_played = 0
                self.wins_baseline = 0
                self.losses_baseline = 0
                self.draws_baseline = 0
                self.wins_variant = 0
                self.losses_variant = 0
                self.draws_variant = 0
                self.snakes_stats = self._init_snakes_stats(self.num_snakes, self.strategy_variant, self.strategy_baseline)
                self.start_time = time.time()
                self.end_time = None
                self.csv_path = ""
                self.recommendation = "PENDING"
                self.skip_requested = False
            elif action == "update_matrix":
                self.matrix_progress = payload.get("matrix_progress", self.matrix_progress)
            elif action == "update_game":
                if self.status == "DISCONNECTED":
                    self.status = "RUNNING"
                    self.stop_type = "IN_PROGRESS"
                    self.stop_reason = ""
                self.last_game_time = time.time()
                game_num = payload.get("game_num", 0)
                seed = payload.get("seed", 0)
                winner = payload.get("winner", "ERROR")
                turns = payload.get("turns", 0)
                self.total_played += 1
                if self.num_snakes == 4:
                    if winner == "DRAW":
                        self.draws_baseline += 1
                        self.draws_variant += 1
                        for s_id in self.snakes_stats:
                            self.snakes_stats[s_id]["draws"] += 1
                    elif winner == self.strategy_variant:
                        self.wins_variant += 1
                        self.losses_baseline += 1
                        if winner in self.snakes_stats:
                            self.snakes_stats[winner]["wins"] += 1
                        for s_id in self.snakes_stats:
                            if s_id != winner:
                                self.snakes_stats[s_id]["losses"] += 1
                    else:
                        # An opposing baseline snake won
                        self.wins_baseline += 1
                        self.losses_variant += 1
                        if winner in self.snakes_stats:
                            self.snakes_stats[winner]["wins"] += 1
                        for s_id in self.snakes_stats:
                            if s_id != winner:
                                self.snakes_stats[s_id]["losses"] += 1
                else:
                    if winner == "DRAW":
                        self.draws_baseline += 1
                        self.draws_variant += 1
                        for s_id in self.snakes_stats:
                            self.snakes_stats[s_id]["draws"] += 1
                    elif winner == self.strategy_baseline:
                        self.wins_baseline += 1
                        self.losses_variant += 1
                        if self.strategy_baseline in self.snakes_stats:
                            self.snakes_stats[self.strategy_baseline]["wins"] += 1
                        if self.strategy_variant in self.snakes_stats:
                            self.snakes_stats[self.strategy_variant]["losses"] += 1
                    elif winner == self.strategy_variant:
                        self.wins_variant += 1
                        self.losses_baseline += 1
                        if self.strategy_variant in self.snakes_stats:
                            self.snakes_stats[self.strategy_variant]["wins"] += 1
                        if self.strategy_baseline in self.snakes_stats:
                            self.snakes_stats[self.strategy_baseline]["losses"] += 1
            elif action == "restore":
                d = payload.get("data", {})
                for attr in [
                    "status", "stop_type", "stop_reason", "total_played", "target_games",
                    "min_games", "max_games", "check_interval", "early_stop_high",
                    "early_stop_low", "threads", "strategy_baseline", "strategy_variant",
                    "experiment_desc", "recommendation", "csv_path",
                    "game_mode", "num_snakes", "target_win_share", "matrix_progress", "active_pid"
                ]:
                    if attr in d:
                        setattr(self, attr, d[attr])
                if "elapsed_seconds" in d:
                    self.start_time = time.time() - float(d["elapsed_seconds"])
                if "last_game_time" in d:
                    self.last_game_time = float(d["last_game_time"])
                elif "elapsed_seconds" in d:
                    self.last_game_time = self.start_time
                if "end_time" in d and d["end_time"]:
                    self.end_time = float(d["end_time"])
                if "baseline" in d and isinstance(d["baseline"], dict):
                    self.wins_baseline = d["baseline"].get("wins", self.wins_baseline)
                    self.losses_baseline = d["baseline"].get("losses", self.losses_baseline)
                    self.draws_baseline = d["baseline"].get("draws", self.draws_baseline)
                if "variant" in d and isinstance(d["variant"], dict):
                    self.wins_variant = d["variant"].get("wins", self.wins_variant)
                    self.losses_variant = d["variant"].get("losses", self.losses_variant)
                    self.draws_variant = d["variant"].get("draws", self.draws_variant)
                if "elapsed_seconds" in d:
                    self.start_time = time.time() - float(d["elapsed_seconds"])
                if "snakes_stats" in d and isinstance(d["snakes_stats"], dict):
                    self.snakes_stats = d["snakes_stats"]
                else:
                    self.snakes_stats = self._init_snakes_stats(self.num_snakes, self.strategy_variant, self.strategy_baseline)
                    if self.num_snakes == 2 and self.strategy_variant in self.snakes_stats:
                        self.snakes_stats[self.strategy_variant]["wins"] = self.wins_variant
                        self.snakes_stats[self.strategy_variant]["losses"] = self.losses_variant
                        self.snakes_stats[self.strategy_variant]["draws"] = self.draws_variant
                        if self.strategy_baseline in self.snakes_stats:
                            self.snakes_stats[self.strategy_baseline]["wins"] = self.wins_baseline
                            self.snakes_stats[self.strategy_baseline]["losses"] = self.losses_baseline
                            self.snakes_stats[self.strategy_baseline]["draws"] = self.draws_baseline
            elif action == "set_finished":
                stop_type = payload.get("stop_type", "FINISHED")
                self.status = "FINISHED" if stop_type != "TEST_FAILURE" else "TEST_FAILURE"
                self.stop_type = stop_type
                self.stop_reason = payload.get("stop_reason", "")
                self.recommendation = payload.get("recommendation", "REVERT")
                self.csv_path = str(payload.get("csv_path", ""))
                self.end_time = time.time()

    def update_game(self, game_num: int, seed: int, winner: str, turns: int = 0):
        with self.lock:
            if self.status == "DISCONNECTED":
                self.status = "RUNNING"
                self.stop_type = "IN_PROGRESS"
                self.stop_reason = ""
            self.last_game_time = time.time()
            self.last_activity_time = time.time()
            self.total_played += 1
            draw_share = 1.0 / self.num_snakes
            if self.num_snakes == 4:
                if winner == "DRAW":
                    self.draws_baseline += 1
                    self.draws_variant += 1
                    for s_id in self.snakes_stats:
                        self.snakes_stats[s_id]["draws"] += 1
                elif winner == self.strategy_variant:
                    self.wins_variant += 1
                    self.losses_baseline += 1
                    if winner in self.snakes_stats:
                        self.snakes_stats[winner]["wins"] += 1
                    for s_id in self.snakes_stats:
                        if s_id != winner:
                            self.snakes_stats[s_id]["losses"] += 1
                else:
                    self.wins_baseline += 1
                    self.losses_variant += 1
                    if winner in self.snakes_stats:
                        self.snakes_stats[winner]["wins"] += 1
                    elif self.strategy_baseline in self.snakes_stats:
                        self.snakes_stats[self.strategy_baseline]["wins"] += 1
                    for s_id in self.snakes_stats:
                        if s_id != winner and s_id != self.strategy_baseline:
                            self.snakes_stats[s_id]["losses"] += 1
            else:
                if winner == "DRAW":
                    self.draws_baseline += 1
                    self.draws_variant += 1
                    for s_id in self.snakes_stats:
                        self.snakes_stats[s_id]["draws"] += 1
                elif winner == self.strategy_baseline:
                    self.wins_baseline += 1
                    self.losses_variant += 1
                    if self.strategy_baseline in self.snakes_stats:
                        self.snakes_stats[self.strategy_baseline]["wins"] += 1
                    if self.strategy_variant in self.snakes_stats:
                        self.snakes_stats[self.strategy_variant]["losses"] += 1
                elif winner == self.strategy_variant:
                    self.wins_variant += 1
                    self.losses_baseline += 1
                    if self.strategy_variant in self.snakes_stats:
                        self.snakes_stats[self.strategy_variant]["wins"] += 1
                    if self.strategy_baseline in self.snakes_stats:
                        self.snakes_stats[self.strategy_baseline]["losses"] += 1

    def set_finished(self, stop_type: str, stop_reason: str, recommendation: str, csv_path: Any):
        with self.lock:
            self.last_activity_time = time.time()
            self.status = "FINISHED" if stop_type != "TEST_FAILURE" else "TEST_FAILURE"
            self.stop_type = stop_type
            self.stop_reason = stop_reason
            self.recommendation = recommendation
            self.csv_path = str(csv_path)
            self.end_time = time.time()

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            # Automatic runner liveness check
            if self.active_pid is not None:
                try:
                    os.kill(self.active_pid, 0)
                    # Runner process is alive and actively running!
                    if self.status == "DISCONNECTED":
                        self.status = "RUNNING"
                        self.stop_type = "IN_PROGRESS"
                        self.stop_reason = ""
                except (ProcessLookupError, PermissionError):
                    if self.status == "RUNNING":
                        self.status = "DISCONNECTED"
                        self.stop_type = "DISCONNECTED"
                        self.stop_reason = f"Simulation runner process (PID {self.active_pid}) terminated unexpectedly."
                        if not self.end_time:
                            self.end_time = getattr(self, "last_game_time", time.time())
            elif self.status == "RUNNING":
                # No PID registered; fallback inactivity timer (generous 15 minutes)
                last_g_time = getattr(self, "last_game_time", self.start_time)
                if time.time() - last_g_time > 900.0:
                    self.status = "DISCONNECTED"
                    self.stop_type = "DISCONNECTED"
                    self.stop_reason = "No simulation progress received in over 15 minutes (Runner disconnected)."
                    if not self.end_time:
                        self.end_time = getattr(self, "last_game_time", time.time())

            played = self.total_played
            draw_share = 1.0 / self.num_snakes

            rate_base = (self.wins_baseline / played * 100.0) if played > 0 else 0.0
            rate_var = (self.wins_variant / played * 100.0) if played > 0 else 0.0

            base_pts = self.wins_baseline + (1.0 - draw_share) * self.draws_baseline
            var_pts = self.wins_variant + draw_share * self.draws_variant
            base_ws = (base_pts / played * 100.0) if played > 0 else (100.0 - self.target_win_share)
            var_ws = (var_pts / played * 100.0) if played > 0 else self.target_win_share

            # Per-snake breakdown list
            snakes_list = []
            for s_id, s_data in self.snakes_stats.items():
                w = s_data["wins"]
                d = s_data["draws"]
                l = s_data["losses"]
                is_var = (s_id == self.strategy_variant)
                if self.num_snakes == 4 and len(self.snakes_stats) == 2:
                    pts = w + (0.25 if is_var else 0.75) * d
                    ws = (pts / played * 100.0) if played > 0 else (25.0 if is_var else 75.0)
                    target = 25.0 if is_var else 75.0
                    display_name = f"{s_id.upper()} (VARIANT)" if is_var else f"{s_id.upper()} (3 BASELINES)"
                else:
                    pts = w + draw_share * d
                    ws = (pts / played * 100.0) if played > 0 else self.target_win_share
                    target = self.target_win_share
                    display_name = s_id.upper().replace("_", " ")
                rt = (w / played * 100.0) if played > 0 else 0.0
                snakes_list.append({
                    "id": s_id,
                    "name": display_name,
                    "wins": round(w, 2) if isinstance(w, float) else w,
                    "losses": round(l, 2) if isinstance(l, float) else l,
                    "draws": round(d, 2) if isinstance(d, float) else d,
                    "points": round(pts, 1),
                    "rate": round(rt, 1),
                    "win_share": round(ws, 1),
                    "is_variant": is_var,
                    "target_share": target,
                    "delta_parity": round(ws - target, 1),
                })
            snakes_list.sort(key=lambda s: s["win_share"], reverse=True)

            # Live copy of matrix_progress reflecting running mode's live percentage and game count
            live_matrix_progress = []
            if self.matrix_progress:
                for item in self.matrix_progress:
                    item_copy = dict(item)
                    if (
                        item_copy.get("status") == "RUNNING"
                        or (
                            item_copy.get("mode") == self.game_mode
                            and self.status == "RUNNING"
                            and item_copy.get("status") not in ("PASSED", "PARITY", "FAILED", "SKIPPED")
                        )
                    ):
                        item_copy["status"] = "RUNNING"
                        item_copy["win_share"] = round(var_ws, 1)
                        item_copy["games"] = played
                    live_matrix_progress.append(item_copy)

            import math
            p0 = 50.0 if self.num_snakes == 2 else 25.0
            p0_frac = p0 / 100.0
            eval_n = played if played >= self.min_games else self.min_games
            se_curr = math.sqrt(p0_frac * (1.0 - p0_frac) / eval_n) * 100.0
            dyn_high = min(100.0, round(p0 + 2.80 * se_curr, 1))
            dyn_low = max(0.0, round(p0 - 2.80 * se_curr, 1))

            if played < self.min_games:
                next_check = self.min_games
                next_checkpoint_display = f"Game {self.min_games}"
                games_to_next = self.min_games - played
            else:
                next_check = self.max_games
                next_checkpoint_display = "Continuous (z=2.8)"
                games_to_next = max(0, self.max_games - played)

            now = self.end_time or time.time()
            elapsed = now - self.start_time

            return {
                "status": self.status,
                "stop_type": self.stop_type,
                "stop_reason": self.stop_reason,
                "runner_alive": (self.status == "RUNNING"),
                "last_game_time": getattr(self, "last_game_time", self.start_time),
                "skip_requested": self.skip_requested,
                "active_pid": self.active_pid,
                "total_played": played,
                "target_games": self.target_games,
                "min_games": self.min_games,
                "max_games": self.max_games,
                "check_interval": self.check_interval,
                "early_stop_high": dyn_high,
                "early_stop_low": dyn_low,
                "current_early_stop_high": dyn_high,
                "current_early_stop_low": dyn_low,
                "next_checkpoint_display": next_checkpoint_display,
                "threads": self.threads,
                "strategy_baseline": self.strategy_baseline,
                "strategy_variant": self.strategy_variant,
                "game_mode": self.game_mode,
                "num_snakes": self.num_snakes,
                "target_win_share": self.target_win_share,
                "matrix_progress": live_matrix_progress if live_matrix_progress else self.matrix_progress,
                "snakes_list": snakes_list,
                "baseline": {
                    "wins": round(self.wins_baseline, 2) if isinstance(self.wins_baseline, float) else self.wins_baseline,
                    "losses": round(self.losses_baseline, 2) if isinstance(self.losses_baseline, float) else self.losses_baseline,
                    "draws": round(self.draws_baseline, 2) if isinstance(self.draws_baseline, float) else self.draws_baseline,
                    "points": round(base_pts, 1),
                    "rate": round(rate_base, 1),
                    "win_share": round(base_ws, 1),
                },
                "variant": {
                    "wins": round(self.wins_variant, 2) if isinstance(self.wins_variant, float) else self.wins_variant,
                    "losses": round(self.losses_variant, 2) if isinstance(self.losses_variant, float) else self.losses_variant,
                    "draws": round(self.draws_variant, 2) if isinstance(self.draws_variant, float) else self.draws_variant,
                    "points": round(var_pts, 1),
                    "rate": round(rate_var, 1),
                    "win_share": round(var_ws, 1),
                    "delta_parity": round(var_ws - self.target_win_share, 1),
                },
                "draws": {
                    "count": round(self.draws_baseline, 2) if isinstance(self.draws_baseline, float) else self.draws_baseline,
                    "rate": round((self.draws_baseline / played * 100.0) if played > 0 else 0.0, 1)
                },
                "net_points": round(var_pts - base_pts, 1),
                "next_checkpoint": next_check,
                "games_to_next_checkpoint": games_to_next,
                "recommendation": self.recommendation,
                "csv_path": self.csv_path,
                "elapsed_seconds": round(elapsed, 1),
                "snakes_stats": self.snakes_stats,
                "run_history": load_run_history(),
                "experiment_desc": self.experiment_desc
            }


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Battlesnake A/B Test Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #090d16;
      --card-bg: rgba(18, 24, 38, 0.75);
      --card-border: rgba(255, 255, 255, 0.08);
      --text-main: #f3f4f6;
      --text-muted: #9ca3af;
      --baseline-color: #3b82f6;
      --variant-color: #10b981;
      --draw-color: #9ca3af;
      --danger-color: #ef4444;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Outfit', sans-serif;
      background-color: var(--bg-dark);
      background-image: 
        radial-gradient(at 15% 15%, rgba(59, 130, 246, 0.12) 0px, transparent 45%),
        radial-gradient(at 85% 85%, rgba(16, 185, 129, 0.12) 0px, transparent 45%);
      background-attachment: fixed;
      color: var(--text-main);
      min-height: 100vh;
      padding: 2rem;
    }

    .container { max-width: 1150px; margin: 0 auto; }

    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--card-border);
      gap: 1.25rem;
      flex-wrap: nowrap;
    }

    .logo-group {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      min-width: 0;
      flex-shrink: 1;
    }
    .logo-icon {
      font-size: 2.2rem;
      filter: drop-shadow(0 0 12px rgba(16, 185, 129, 0.5));
      flex-shrink: 0;
    }
    .logo-text {
      min-width: 0;
      overflow: hidden;
    }

    h1 {
      font-size: 1.65rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      background: linear-gradient(135deg, #ffffff 0%, #cbd5e1 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      white-space: nowrap;
    }

    #headerSubtext {
      font-size: 0.85rem;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 480px;
    }

    .header-badges {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: nowrap;
      flex-shrink: 0;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.4rem 0.8rem;
      border-radius: 9999px;
      font-size: 0.82rem;
      font-weight: 600;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
      white-space: nowrap;
      flex-shrink: 0;
    }

    .status-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background-color: var(--variant-color);
      box-shadow: 0 0 10px var(--variant-color);
      animation: pulse 1.5s infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.35; transform: scale(0.85); }
    }

    /* Tug of war hero card */
    .hero-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1.25rem;
      padding: 2rem;
      margin-bottom: 1.5rem;
      backdrop-filter: blur(16px);
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.45);
      position: relative;
      overflow: hidden;
    }

    .hero-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--baseline-color), var(--variant-color));
    }

    .tug-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      margin-bottom: 1.25rem;
    }

    .strategy-stat { display: flex; flex-direction: column; }
    .strategy-stat.baseline { text-align: left; }
    .strategy-stat.variant { text-align: right; }

    .strategy-name {
      font-size: 0.9rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-weight: 700;
      margin-bottom: 0.25rem;
    }

    .baseline .strategy-name { color: var(--baseline-color); }
    .variant .strategy-name { color: var(--variant-color); }

    .win-rate-big {
      font-size: 2.5rem;
      font-weight: 800;
      line-height: 1;
      font-family: 'JetBrains Mono', monospace;
    }

    .games-count { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.35rem; }

    /* Hero Dual Bars Section */
    .bar-section {
      margin-top: 1.25rem;
      margin-bottom: 1.25rem;
    }

    .bar-label-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.75rem;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 1.5rem;
    }

    /* Bar 1: Points Win Share Bar */
    .tug-bar-wrapper { position: relative; margin-top: 1.25rem; margin-bottom: 1.25rem; }

    .tug-bar-container {
      position: relative;
      height: 36px;
      background: rgba(0, 0, 0, 0.5);
      border-radius: 18px;
      overflow: hidden;
      display: flex;
      border: 1px solid rgba(255, 255, 255, 0.12);
      box-shadow: inset 0 3px 8px rgba(0, 0, 0, 0.7);
    }

    .tug-side {
      height: 100%;
      transition: width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      align-items: center;
      font-weight: 700;
      font-size: 0.82rem;
      font-family: 'JetBrains Mono', monospace;
      white-space: nowrap;
      overflow: hidden;
      min-width: 0;
      flex-shrink: 0;
      box-sizing: border-box;
    }

    .tug-side.baseline-pts-bar {
      background: linear-gradient(90deg, #1e3a8a, var(--baseline-color));
      justify-content: flex-start;
      padding-left: 0.85rem;
      color: #fff;
    }

    .tug-side.variant-pts-bar {
      background: linear-gradient(90deg, var(--variant-color), #047857);
      justify-content: flex-end;
      padding-right: 0.85rem;
      color: #fff;
    }

    .tug-divider {
      position: absolute;
      left: 50%; top: 0; bottom: 0;
      width: 2px;
      background: rgba(255, 255, 255, 0.9);
      z-index: 10;
      box-shadow: 0 0 10px #fff;
    }

    .tug-divider-label {
      position: absolute;
      left: 50%;
      top: -22px;
      transform: translateX(-50%);
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.06em;
      white-space: nowrap;
    }

    .tug-threshold {
      position: absolute;
      top: 0; bottom: 0;
      width: 2px;
      z-index: 9;
      border-left: 1.5px dashed rgba(255, 255, 255, 0.5);
    }

    .tug-threshold.threshold-keep { left: 40%; }
    .tug-threshold.threshold-revert { left: 60%; }

    .tug-threshold-label {
      position: absolute;
      top: -22px;
      font-size: 0.72rem;
      font-weight: 700;
      letter-spacing: 0.04em;
      transform: translateX(-50%);
      white-space: nowrap;
    }

    .tug-threshold-label.label-keep { left: 40%; color: var(--variant-color); }
    .tug-threshold-label.label-revert { left: 60%; color: var(--baseline-color); }

    .tug-footer {
      display: flex;
      justify-content: center;
      gap: 1.5rem;
      font-size: 0.85rem;
      color: var(--text-muted);
      margin-top: 1rem;
    }

    /* Recommendation Banner */
    .banner {
      border-radius: 1rem;
      padding: 1.25rem 1.75rem;
      margin-bottom: 1.5rem;
      display: none;
      align-items: center;
      justify-content: space-between;
      border: 1px solid transparent;
      backdrop-filter: blur(12px);
    }

    .banner.info {
      background: rgba(59, 130, 246, 0.1);
      border-color: rgba(59, 130, 246, 0.3);
      color: #93c5fd;
    }

    .banner.keep {
      background: rgba(16, 185, 129, 0.15);
      border-color: rgba(16, 185, 129, 0.4);
      color: #6ee7b7;
      box-shadow: 0 0 30px rgba(16, 185, 129, 0.2);
    }

    .banner.revert {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.4);
      color: #fca5a5;
      box-shadow: 0 0 30px rgba(239, 68, 68, 0.2);
    }

    .banner.parity {
      background: rgba(245, 158, 11, 0.15);
      border-color: rgba(245, 158, 11, 0.4);
      color: #fcd34d;
      box-shadow: 0 0 30px rgba(245, 158, 11, 0.2);
    }

    .banner-title { font-size: 1.2rem; font-weight: 800; display: flex; align-items: center; gap: 0.5rem; }
    .banner-desc { font-size: 0.9rem; opacity: 0.95; margin-top: 0.25rem; }

    /* Combined Stats Card */
    .stats-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 1.25rem;
      margin-bottom: 1.5rem;
    }

    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.25rem 1.5rem;
      backdrop-filter: blur(12px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }

    .stat-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.75rem;
      color: var(--text-muted);
      font-size: 0.825rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .stat-icon { font-size: 1.2rem; }

    .stat-value {
      font-size: 1.75rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      margin-bottom: 0.25rem;
    }

    .stat-subtext { font-size: 0.825rem; color: var(--text-muted); }

    .progress-bar-wrap {
      height: 8px;
      background: rgba(255, 255, 255, 0.1);
      border-radius: 4px;
      overflow: hidden;
      margin-top: 0.8rem;
    }

    .progress-bar-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--baseline-color), var(--variant-color));
      transition: width 0.4s ease;
    }

    /* Logs Table */
    .table-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.5rem;
      backdrop-filter: blur(12px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
      margin-bottom: 1.5rem;
    }

    .table-title {
      font-size: 1.1rem;
      font-weight: 700;
      margin-bottom: 1rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .history-table-wrapper {
      overflow-x: auto;
      border-radius: 0.5rem;
    }

    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }

    th {
      position: sticky;
      top: 0;
      background: #111827;
      z-index: 10;
      text-align: center;
      padding: 0.6rem 0.5rem;
      color: var(--text-muted);
      font-weight: 600;
      border-bottom: 1px solid var(--card-border);
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.05em;
      white-space: nowrap;
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.4);
    }

    td {
      padding: 0.5rem 0.5rem;
      text-align: center;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: 'JetBrains Mono', monospace;
      white-space: nowrap;
    }

    th.col-desc {
      text-align: left;
      max-width: 300px;
      min-width: 180px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: default;
    }

    td.col-desc {
      text-align: left;
      position: relative;
      max-width: 300px;
      min-width: 180px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      cursor: pointer;
    }

    td.col-desc .tooltip-text {
      visibility: hidden;
      opacity: 0;
      width: max-content;
      max-width: 360px;
      background-color: #1e293b;
      color: #f8fafc;
      text-align: left;
      border-radius: 8px;
      padding: 0.6rem 0.85rem;
      position: absolute;
      z-index: 100;
      bottom: 125%;
      left: 50%;
      transform: translateX(-50%);
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.6);
      border: 1px solid rgba(255, 255, 255, 0.15);
      font-size: 0.8rem;
      font-weight: 500;
      white-space: normal;
      line-height: 1.4;
      transition: opacity 0.2s ease, visibility 0.2s ease;
      pointer-events: none;
    }

    td.col-desc:hover .tooltip-text {
      visibility: visible;
      opacity: 1;
    }

    #headerSubtext {
      max-width: 550px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    .winner-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 95px;
      height: 28px;
      padding: 0 0.5rem;
      border-radius: 6px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      box-sizing: border-box;
      white-space: nowrap;
    }

    .winner-pill.variant {
      background: rgba(16, 185, 129, 0.2);
      color: var(--variant-color);
      border: 1px solid rgba(16, 185, 129, 0.4);
    }

    .winner-pill.baseline {
      background: rgba(59, 130, 246, 0.2);
      color: var(--baseline-color);
      border: 1px solid rgba(59, 130, 246, 0.4);
    }

    .winner-pill.draw {
      background: rgba(156, 163, 175, 0.2);
      color: var(--text-muted);
      border: 1px solid rgba(156, 163, 175, 0.4);
    }

    .winner-pill.revert {
      background: rgba(239, 68, 68, 0.2);
      color: var(--danger-color);
      border: 1px solid rgba(239, 68, 68, 0.4);
    }

    .winner-pill.parity {
      background: rgba(245, 158, 11, 0.2);
      color: #f59e0b;
      border: 1px solid rgba(245, 158, 11, 0.4);
    }


    /* Multi-Mode Matrix Pipeline Ribbon */
    .matrix-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.5rem;
      backdrop-filter: blur(14px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }

    .matrix-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }

    .matrix-title {
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: -0.01em;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .matrix-subtitle {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
    }

    .matrix-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.85rem;
    }

    @media (max-width: 900px) {
      .matrix-grid { grid-template-columns: repeat(2, 1fr); }
      header {
        flex-direction: column;
        align-items: flex-start;
        gap: 1rem;
      }
      .header-badges {
        width: 100%;
        justify-content: flex-start;
        flex-wrap: wrap;
      }
    }

    .matrix-col {
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.07);
      border-radius: 0.75rem;
      padding: 0.85rem 1rem;
      transition: all 0.3s ease;
      position: relative;
      overflow: hidden;
    }

    .matrix-col.running {
      border-color: rgba(59, 130, 246, 0.55);
      box-shadow: 0 0 16px rgba(59, 130, 246, 0.25);
      background: rgba(59, 130, 246, 0.08);
    }

    .matrix-col.passed {
      border-color: rgba(16, 185, 129, 0.5);
      background: rgba(16, 185, 129, 0.08);
    }

    .matrix-col.parity {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.08);
    }

    .matrix-col.failed {
      border-color: rgba(239, 68, 68, 0.5);
      background: rgba(239, 68, 68, 0.08);
    }

    .matrix-col.skipped {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.08);
    }

    .matrix-col-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.4rem;
    }

    .matrix-col-name {
      font-size: 0.85rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .matrix-col-status {
      font-size: 0.68rem;
      font-weight: 800;
      padding: 0.18rem 0.45rem;
      border-radius: 9999px;
      text-transform: uppercase;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: 0.03em;
    }

    .matrix-status-pending { background: rgba(156, 163, 175, 0.15); color: var(--text-muted); }
    .matrix-status-running { background: rgba(59, 130, 246, 0.25); color: #60a5fa; animation: pulse 1.5s infinite; }
    .matrix-status-passed { background: rgba(16, 185, 129, 0.25); color: #34d399; }
    .matrix-status-parity { background: rgba(245, 158, 11, 0.25); color: #f59e0b; }
    .matrix-status-failed { background: rgba(239, 68, 68, 0.25); color: #f87171; }
    .matrix-status-skipped { background: rgba(245, 158, 11, 0.25); color: #fbbf24; }

    .matrix-col-score {
      font-size: 1.35rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      margin-bottom: 0.2rem;
    }

    .matrix-col-meta {
      font-size: 0.74rem;
      color: var(--text-muted);
    }


    .row-variant {
      background: rgba(16, 185, 129, 0.12) !important;
      font-weight: 700;
    }

    .b-seg-1 { background: linear-gradient(90deg, #1e3a8a, #2563eb) !important; }
    .b-seg-2 { background: linear-gradient(90deg, #1d4ed8, #3b82f6) !important; border-left: 1px solid rgba(255, 255, 255, 0.2); }
    .b-seg-3 { background: linear-gradient(90deg, #2563eb, #60a5fa) !important; border-left: 1px solid rgba(255, 255, 255, 0.2); }
    .b-sub-2 { background: linear-gradient(90deg, #1d4ed8, #2563eb) !important; border-left: 1px solid rgba(255, 255, 255, 0.15); }
    .b-sub-3 { background: linear-gradient(90deg, #2563eb, #3b82f6) !important; border-left: 1px solid rgba(255, 255, 255, 0.15); }

    /* Skip Button */
    .btn-skip {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid rgba(245, 158, 11, 0.45);
      color: #fcd34d;
      font-family: inherit;
      font-weight: 700;
      font-size: 0.8rem;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      cursor: pointer;
      transition: all 0.2s ease;
      user-select: none;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .btn-skip:hover:not(:disabled) {
      background: rgba(245, 158, 11, 0.35);
      border-color: rgba(245, 158, 11, 0.8);
      color: #ffffff;
      box-shadow: 0 0 12px rgba(245, 158, 11, 0.4);
      transform: translateY(-1px);
    }
    .btn-skip:disabled {
      opacity: 0.35;
      cursor: not-allowed;
      transform: none;
    }

    /* Cancel / Stop Button */
    .btn-cancel {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.45);
      color: #fca5a5;
      font-family: inherit;
      font-weight: 700;
      font-size: 0.8rem;
      padding: 0.35rem 0.85rem;
      border-radius: 9999px;
      cursor: pointer;
      transition: all 0.2s ease;
      user-select: none;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .btn-cancel:hover:not(:disabled) {
      background: rgba(239, 68, 68, 0.35);
      border-color: rgba(239, 68, 68, 0.8);
      color: #ffffff;
      box-shadow: 0 0 12px rgba(239, 68, 68, 0.4);
      transform: translateY(-1px);
    }
    .btn-cancel:disabled {
      opacity: 0.35;
      cursor: not-allowed;
      transform: none;
    }
  </style>
</head>
<body>

<div class="container">
  <header>
    <div class="logo-group">
      <span class="logo-icon">🐍</span>
      <div class="logo-text">
        <h1>Battlesnake A/B Test Monitor</h1>
        <div style="font-size: 0.85rem; color: var(--text-muted);" id="headerSubtext">Testing: Unspecified Experiment</div>
      </div>
    </div>
    <div class="header-badges">
      <div class="badge">
        ⏱️ <span id="elapsedTime">0s</span>
      </div>
      <div class="badge">
        ⚡ <span id="threadsCount">-</span> Threads
      </div>
      <div class="badge" id="statusBadge">
        <span class="status-dot" id="statusDot"></span>
        <span id="statusText">CONNECTING...</span>
      </div>
      <button class="btn-skip" id="skipBtnHeader" onclick="skipActiveMode()" title="Skip the current mode and advance to next">
        <span>⏭️</span>
        <span id="skipBtnText">Skip Mode</span>
      </button>
      <button class="btn-cancel" id="cancelBtnHeader" onclick="cancelActiveRun()" title="Stop the active A/B test run">
        <span>⏹️</span>
        <span id="cancelBtnText">Stop Run</span>
      </button>
    </div>
  </header>

  <!-- Multi-Mode Regression Matrix (shown when matrix_progress has entries) -->
  <div class="matrix-card" id="matrixCard" style="display: none;">
    <div class="matrix-header">
      <span class="matrix-title">🏁 Multi-Mode Regression Matrix</span>
      <span class="matrix-subtitle" id="matrixSubtitle">0 / 4 Modes Complete</span>
    </div>
    <div class="matrix-grid" id="matrixGrid">
      <!-- Dynamic mode cards -->
    </div>
  </div>

  <!-- Tug-of-War Hero Card -->
  <div class="hero-card">
    <div class="tug-header">
      <div class="strategy-stat baseline">
        <div class="strategy-name" id="baselineName">BASELINE</div>
        <div class="win-rate-big" id="baselinePts" style="color: var(--baseline-color);">0.0 Pts</div>
        <div class="games-count" id="baselineSubtext">0 Wins</div>
      </div>

      <div style="text-align: center; margin-bottom: 0.25rem;">
        <div style="font-weight: 700; font-size: 0.75rem; color: var(--text-muted); letter-spacing: 0.1em; text-transform: uppercase;" id="leadTitle">Win Value Lead</div>
        <div style="font-size: 1.25rem; font-weight: 800; font-family: 'JetBrains Mono', monospace;" id="leadText">EVEN</div>
        <div style="font-size: 0.82rem; font-weight: 700; color: var(--draw-color); font-family: 'JetBrains Mono', monospace; margin-top: 0.25rem;" id="drawsHeaderRate">0 Draws</div>
      </div>

      <div class="strategy-stat variant">
        <div class="strategy-name" id="variantName">VARIANT</div>
        <div class="win-rate-big" id="variantPts" style="color: var(--variant-color);">0.0 Pts</div>
        <div class="games-count" id="variantSubtext">0 Wins</div>
      </div>
    </div>

    <!-- Win Share Bar Section -->
    <div class="bar-section">
      <!-- Bar 1: Win Share Tug-of-War Bar -->
      <div class="bar-label-header" style="justify-content: center;">
        <span>Win Share</span>
      </div>
      <div class="tug-bar-wrapper">
        <div class="tug-threshold-label label-keep" id="labelKeep">60% KEEP</div>
        <div class="tug-divider-label" id="labelParity">50% PARITY</div>
        <div class="tug-threshold-label label-revert" id="labelRevert">40% REVERT</div>
        <div class="tug-bar-container" id="tugBarContainer">
          <div class="tug-threshold threshold-keep" id="threshKeep"></div>
          <div class="tug-divider" id="threshParity"></div>
          <div class="tug-threshold threshold-revert" id="threshRevert"></div>
          <div class="tug-baselines-wrap" id="tugBaselinesWrap" style="width: 50%; display: flex; height: 100%; flex-shrink: 0; overflow: hidden; box-sizing: border-box;">
            <div class="tug-side baseline-pts-bar b-seg-1" id="baseSeg1" style="width: 100%;">
              <span id="baseSeg1Text">50.0%</span>
            </div>
            <div class="tug-side baseline-pts-bar b-seg-2" id="baseSeg2" style="width: 0%; display: none;">
              <span id="baseSeg2Text"></span>
            </div>
            <div class="tug-side baseline-pts-bar b-seg-3" id="baseSeg3" style="width: 0%; display: none;">
              <span id="baseSeg3Text"></span>
            </div>
          </div>
          <div class="tug-side variant-pts-bar" id="variantPtsBar" style="width: 50%; flex-shrink: 0;">
            <span id="variantPtsBarText">50.0%</span>
          </div>
      </div>
    </div>
  </div>
</div>

  <!-- Combined Key Metrics Grid (Run Progress) -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-header">
        <span>Run Progress</span>
        <span class="stat-icon">⚡</span>
      </div>
      <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 0.35rem;">
        <div>
          <div class="stat-value" id="runsValue">0 / 0</div>
          <div class="stat-subtext" id="runsSubtext">0% Complete</div>
        </div>
        <div style="text-align: right;">
          <div class="stat-value" style="font-size: 1.4rem; color: #60a5fa;" id="paceValue">0.000 games/sec</div>
        </div>
      </div>
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" id="runsProgress" style="width: 0%;"></div>
      </div>
    </div>
  </div>


  <!-- Dynamic Decision Status Banner -->
  <div class="banner info" id="decisionBanner">
    <div>
      <div class="banner-title" id="bannerTitle">ℹ️ Test In Progress</div>
      <div class="banner-desc" id="bannerDesc">Accumulating game results to evaluate early stop thresholds...</div>
    </div>
  </div>

  <!-- A/B Test Simulation Run History -->
  <div class="table-card">
    <div class="table-title">
      <span>📜 Run History</span>
      <span style="font-size: 0.8rem; font-weight: 500; color: var(--text-muted);" id="historyCount">Last 10</span>
    </div>
    <div class="history-table-wrapper">
      <table>
        <thead>
          <tr>
            <th style="text-align: left; min-width: 90px;" title="Timestamp of simulation run completion">Date & Time</th>
            <th class="col-desc" style="text-align: left;" title="Full title and description of tested heuristic change">Tested Change</th>
            <th style="min-width: 80px;" title="Constrictor 4P Variant Win Share (25.0% Target)">Constrictor</th>
            <th style="min-width: 80px;" title="Royale 4P Variant Win Share (25.0% Target)">Royale</th>
            <th style="min-width: 80px;" title="Duel 1v1 Variant Win Share (50.0% Target)">Duel</th>
            <th style="min-width: 80px;" title="Standard 4P Variant Win Share (25.0% Target)">Standard</th>
            <th style="min-width: 90px;" title="Automated recommendation (KEEP / PARITY / REVERT)">Decision</th>
          </tr>
        </thead>
        <tbody id="historyTableBody">
          <tr>
            <td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2rem;">No previous simulation runs recorded.</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
  function formatSeconds(sec) {
    if (!sec || sec <= 0 || !isFinite(sec)) return '0s';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);

    if (d > 0) {
      return `${d}d ${h}h ${m}m`;
    }
    if (h > 0) {
      return `${h}h ${m}m ${s}s`;
    }
    if (m > 0) {
      return `${m}m ${s}s`;
    }
    return `${s}s`;
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function setText(id, text) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (el && el.innerText !== String(text)) {
      el.innerText = String(text);
    }
  }

  function setHTML(id, html) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (el && el.innerHTML !== String(html)) {
      el.innerHTML = String(html);
    }
  }

  function setWidth(id, widthVal) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (el) {
      let wStr = String(widthVal);
      if (!wStr.endsWith('%') && !wStr.endsWith('px') && !wStr.endsWith('rem')) {
        wStr += '%';
      }
      if (el.style.width !== wStr) {
        el.style.width = wStr;
      }
    }
  }

  let lastHistoryJSON = '';

  async function updateDashboard() {
    try {
      const res = await fetch('/api/data');
      if (!res.ok) return;
      const data = await res.json();

      // Header Status & Experiment Info
      const statusDot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');
      if (statusDot && statusText) {
        if (data.status === 'FINISHED') {
          if (statusDot.style.animation !== 'none') statusDot.style.animation = 'none';
          let targetColor = 'var(--danger-color)';
          if (data.recommendation === 'KEEP') targetColor = 'var(--variant-color)';
          else if (data.recommendation === 'PARITY') targetColor = '#f59e0b';
          if (statusDot.style.backgroundColor !== targetColor) statusDot.style.backgroundColor = targetColor;
          setText(statusText, data.recommendation === 'KEEP' ? 'KEEP' : (data.recommendation === 'PARITY' ? 'PARITY' : (data.recommendation === 'REVERT' ? 'REVERT' : 'FINISHED')));
        } else if (data.status === 'DISCONNECTED') {
          if (statusDot.style.animation !== 'none') statusDot.style.animation = 'none';
          statusDot.style.backgroundColor = 'var(--danger-color)';
          setText(statusText, 'DISCONNECTED');
        } else if (data.status === 'STOPPED') {
          if (statusDot.style.animation !== 'none') statusDot.style.animation = 'none';
          statusDot.style.backgroundColor = 'var(--draw-color)';
          setText(statusText, 'STOPPED');
        } else if (data.status === 'RUNNING' && data.runner_alive !== false) {
          const targetColor = 'var(--variant-color)';
          if (statusDot.style.backgroundColor !== targetColor) statusDot.style.backgroundColor = targetColor;
          if (statusDot.style.animation !== 'pulse 2s infinite') statusDot.style.animation = 'pulse 2s infinite';
          setText(statusText, 'LIVE');
        } else {
          if (statusDot.style.animation !== 'none') statusDot.style.animation = 'none';
          statusDot.style.backgroundColor = 'var(--draw-color)';
          setText(statusText, String(data.status || 'STOPPED').toUpperCase());
        }
      }

      // Update skip and cancel buttons state
      const skipBtn = document.getElementById('skipBtnHeader');
      if (skipBtn) {
        if (data.status === 'RUNNING' && data.runner_alive !== false) {
          if (data.skip_requested) {
            skipBtn.disabled = true;
            skipBtn.style.opacity = '0.6';
            skipBtn.style.pointerEvents = 'none';
            setHTML(skipBtn, '<span>⏳</span> <span id="skipBtnText">Advancing...</span>');
          } else {
            skipBtn.disabled = false;
            skipBtn.style.opacity = '1';
            skipBtn.style.pointerEvents = 'auto';
            setHTML(skipBtn, '<span>⏭️</span> <span id="skipBtnText">Skip Mode</span>');
          }
        } else {
          skipBtn.disabled = true;
          skipBtn.style.opacity = '0.35';
          skipBtn.style.pointerEvents = 'none';
          setHTML(skipBtn, '<span>⏭️</span> <span id="skipBtnText">Skip Mode</span>');
        }
      }

      const cancelBtns = document.querySelectorAll('.btn-cancel');
      cancelBtns.forEach(btn => {
        if (data.status === 'RUNNING' && data.runner_alive !== false) {
          btn.disabled = false;
          btn.style.opacity = '1';
          btn.style.pointerEvents = 'auto';
        } else {
          btn.disabled = true;
          btn.style.opacity = '0.35';
          btn.style.pointerEvents = 'none';
        }
      });

      const subtext = document.getElementById('headerSubtext');
      if (subtext) {
        const expTitle = data.experiment_desc || 'Unspecified Experiment';
        const newSub = `Testing: ${expTitle}`;
        if (subtext.innerText !== newSub) subtext.innerText = newSub;
        if (subtext.title !== newSub) subtext.title = newSub;
      }

      // Mode Badge
      const modeNames = {
        'duel': 'DUEL 1v1',
        'standard': 'STANDARD 4P',
        'ffa': 'STANDARD 4P',
        'royale': 'ROYALE 4P',
        'constrictor': 'CONSTRICTOR 4P',
        'wrapped': 'WRAPPED 1v1'
      };
      const activeMode = data.game_mode || 'duel';
      const modeDisplay = modeNames[activeMode] || activeMode.toUpperCase();
      setText('modeName', modeDisplay);

      const modeIcons = {
        'duel': '⚔️',
        'standard': '👑',
        'ffa': '👑',
        'royale': '🔥',
        'constrictor': '🐍',
        'wrapped': '🔄'
      };
      setText('modeIcon', modeIcons[activeMode] || '🎮');

      setText('threadsCount', (data.threads != null && data.threads > 0) ? data.threads : '-');
      setText('elapsedTime', formatSeconds(data.elapsed_seconds || 0));

      const elapsed = data.elapsed_seconds || 0;
      const isRunning = (data.status === 'RUNNING' && data.runner_alive !== false);
      const pace = (isRunning && elapsed > 0) ? (data.total_played / elapsed) : 0;
      if (isRunning) {
        setText('paceValue', `${pace.toFixed(3)} games/sec`);
      } else {
        setText('paceValue', `0.000 games/sec (Paused)`);
      }

      // Strategy Names & Lead
      const is4p = data.num_snakes === 4;
      if (is4p) {
        setText('baselineName', 'OPPONENTS (3x BASELINE)');
      } else {
        setText('baselineName', (data.strategy_baseline || 'baseline').toUpperCase());
      }
      setText('variantName', (data.strategy_variant || 'variant').toUpperCase());

      // Points & Rates Calculation
      const played = data.total_played || 0;
      const baseW = (data.baseline && data.baseline.wins != null) ? data.baseline.wins : 0;
      const varW = (data.variant && data.variant.wins != null) ? data.variant.wins : 0;
      const drawsCount = data.draws ? data.draws.count : ((data.baseline && data.baseline.draws != null) ? data.baseline.draws : 0);

      const drawShare = is4p ? 0.25 : 0.5;
      const basePts = (data.baseline && typeof data.baseline.points === 'number') ? data.baseline.points : (baseW + (1.0 - drawShare) * drawsCount);
      const varPts = (data.variant && typeof data.variant.points === 'number') ? data.variant.points : (varW + drawShare * drawsCount);
      const targetWS = data.target_win_share || (is4p ? 25.0 : 50.0);
      const baseWS = (data.baseline && typeof data.baseline.win_share === 'number') ? data.baseline.win_share : (played > 0 ? (basePts / played * 100) : (100.0 - targetWS));
      const varWS = (data.variant && typeof data.variant.win_share === 'number') ? data.variant.win_share : (played > 0 ? (varPts / played * 100) : targetWS);

      // Matrix Pipeline Display — must be after played/varWS are computed so RUNNING mode shows live %
      const matrixCard = document.getElementById('matrixCard');
      const matrixGrid = document.getElementById('matrixGrid');
      const matrixSubtitle = document.getElementById('matrixSubtitle');
      if (matrixCard && matrixGrid) {
        const matrixList = data.matrix_progress || [];
        if (matrixList.length > 0) {
          matrixCard.style.display = 'block';
          const completedCount = matrixList.filter(m => m.status === 'PASSED' || m.status === 'PARITY' || m.status === 'FAILED' || m.status === 'SKIPPED').length;
          if (matrixSubtitle) matrixSubtitle.innerText = `${completedCount} / ${matrixList.length} Modes Complete`;

          matrixGrid.innerHTML = matrixList.map((m, idx) => {
            const isCurrentRunning = m.status === 'RUNNING' || (data.game_mode === m.mode && data.status === 'RUNNING' && m.status !== 'PASSED' && m.status !== 'PARITY' && m.status !== 'FAILED' && m.status !== 'SKIPPED');
            const effectiveStatus = isCurrentRunning ? 'RUNNING' : (m.status || 'PENDING');
            const statusClass = effectiveStatus.toLowerCase();
            let statusBadge = `<span class="matrix-col-status matrix-status-${statusClass}">${effectiveStatus}</span>`;
            const winShareVal = isCurrentRunning ? varWS : (m.win_share || 0);
            const gamesVal = isCurrentRunning ? played : (m.games || 0);

            let scoreText = effectiveStatus === 'PENDING' ? '--' : (effectiveStatus === 'SKIPPED' && !gamesVal ? 'SKIPPED' : `${winShareVal.toFixed(1)}%`);
            let metaText = `Target: > ${(m.target_share || (m.num_snakes === 4 ? 25.0 : 50.0)).toFixed(1)}% (${gamesVal}/${m.max_games || 200}G)`;
            let scoreColor = effectiveStatus === 'PASSED' ? 'var(--variant-color)' : (effectiveStatus === 'PARITY' ? '#f59e0b' : (effectiveStatus === 'FAILED' ? 'var(--danger-color)' : (effectiveStatus === 'SKIPPED' ? '#fbbf24' : (effectiveStatus === 'RUNNING' ? '#60a5fa' : 'var(--text-main)'))));
            return `
              <div class="matrix-col ${statusClass}">
                <div class="matrix-col-top">
                  <span class="matrix-col-name" title="${m.name || m.mode}">${idx + 1}. ${m.name || m.mode.toUpperCase()}</span>
                  ${statusBadge}
                </div>
                <div class="matrix-col-score" style="color: ${scoreColor};">${scoreText}</div>
                <div class="matrix-col-meta">${metaText}</div>
              </div>
            `;
          }).join('');
        } else {
          matrixCard.style.display = 'none';
        }
      }

      setText('baselinePts', `${basePts.toFixed(1)} Pts`);
      setText('variantPts', `${varPts.toFixed(1)} Pts`);
      setText('baselineSubtext', `${baseW} Wins`);
      setText('variantSubtext', `${varW} Wins`);
      setText('drawsHeaderRate', `${drawsCount} Draws`);

      // Dynamic Threshold Markers & Labels
      const labelParity = document.getElementById('labelParity');
      const threshParity = document.getElementById('threshParity');
      const labelKeep = document.getElementById('labelKeep');
      const threshKeep = document.getElementById('threshKeep');
      const labelRevert = document.getElementById('labelRevert');
      const threshRevert = document.getElementById('threshRevert');

      const highStop = data.current_early_stop_high || data.early_stop_high || (is4p ? 35.0 : 60.0);
      const lowStop = data.current_early_stop_low || data.early_stop_low || (is4p ? 18.0 : 40.0);

      if (is4p) {
        // 4-Player Parity = 25% for variant (75% for baselines)
        if (labelParity) { labelParity.innerText = '25% PARITY'; labelParity.style.left = '75%'; }
        if (threshParity) { threshParity.style.left = '75%'; }
        const keepPos = 100 - highStop;
        if (labelKeep) {
          labelKeep.innerText = `${highStop.toFixed(1)}% KEEP`;
          labelKeep.style.left = `${keepPos}%`;
          labelKeep.style.transform = keepPos < 10 ? 'translateX(-15%)' : 'translateX(-50%)';
        }
        if (threshKeep) { threshKeep.style.left = `${keepPos}%`; }
        const revertPos = 100 - lowStop;
        if (labelRevert) {
          labelRevert.innerText = `${lowStop.toFixed(1)}% REVERT`;
          labelRevert.style.left = `${revertPos}%`;
          labelRevert.style.transform = revertPos > 88 ? 'translateX(-85%)' : 'translateX(-50%)';
        }
        if (threshRevert) { threshRevert.style.left = `${revertPos}%`; }
      } else {
        // 1v1 Parity = 50%
        if (labelParity) { labelParity.innerText = '50% PARITY'; labelParity.style.left = '50%'; }
        if (threshParity) { threshParity.style.left = '50%'; }
        const keepPos = 100 - highStop;
        if (labelKeep) {
          labelKeep.innerText = `${highStop.toFixed(1)}% KEEP`;
          labelKeep.style.left = `${keepPos}%`;
          labelKeep.style.transform = keepPos < 10 ? 'translateX(-15%)' : 'translateX(-50%)';
        }
        if (threshKeep) { threshKeep.style.left = `${keepPos}%`; }
        const revertPos = 100 - lowStop;
        if (labelRevert) {
          labelRevert.innerText = `${lowStop.toFixed(1)}% REVERT`;
          labelRevert.style.left = `${revertPos}%`;
          labelRevert.style.transform = revertPos > 88 ? 'translateX(-85%)' : 'translateX(-50%)';
        }
        if (threshRevert) { threshRevert.style.left = `${revertPos}%`; }
      }

      // Lead Text
      const leadEl = document.getElementById('leadText');
      const leadTitle = document.getElementById('leadTitle');
      if (leadEl) {
        if (is4p) {
          if (leadTitle) leadTitle.innerText = 'VARIANT vs 25% PARITY';
          const deltaParity = varWS - 25.0;
          if (Math.abs(deltaParity) < 0.05) {
            setText(leadEl, 'PARITY');
            leadEl.style.color = 'var(--text-main)';
          } else if (deltaParity > 0) {
            setText(leadEl, `+${deltaParity.toFixed(1)}%`);
            leadEl.style.color = 'var(--variant-color)';
          } else {
            setText(leadEl, `${deltaParity.toFixed(1)}%`);
            leadEl.style.color = 'var(--danger-color)';
          }
        } else {
          if (leadTitle) leadTitle.innerText = 'WIN VALUE LEAD';
          const netPts = varPts - basePts;
          if (Math.abs(netPts) < 0.01) {
            setText(leadEl, 'EVEN');
            leadEl.style.color = 'var(--text-main)';
          } else if (netPts > 0) {
            setText(leadEl, `+${netPts.toFixed(1)}`);
            leadEl.style.color = 'var(--variant-color)';
          } else {
            setText(leadEl, `-${Math.abs(netPts).toFixed(1)}`);
            leadEl.style.color = 'var(--baseline-color)';
          }
        }
      }

      // Bar 1: Win Share (Points) Tug-of-War Bar
      const tugBaselinesWrap = document.getElementById('tugBaselinesWrap');
      const varPtsBar = document.getElementById('variantPtsBar');
      const baseSeg1 = document.getElementById('baseSeg1');
      const baseSeg2 = document.getElementById('baseSeg2');
      const baseSeg3 = document.getElementById('baseSeg3');
      const baseSegs = [baseSeg1, baseSeg2, baseSeg3];

      if (is4p) {
        if (played === 0) {
          if (tugBaselinesWrap) tugBaselinesWrap.style.width = '75%';
          if (varPtsBar) {
            varPtsBar.style.display = 'flex';
            varPtsBar.style.width = '25%';
            setText('variantPtsBarText', '25.0%');
          }
          if (baseSeg1) {
            baseSeg1.style.display = 'flex';
            baseSeg1.style.width = '100%';
            setText('baseSeg1Text', 'BASELINES 75.0%');
          }
          if (baseSeg2) { baseSeg2.style.display = 'none'; baseSeg2.style.width = '0%'; }
          if (baseSeg3) { baseSeg3.style.display = 'none'; baseSeg3.style.width = '0%'; }
        } else {
          if (tugBaselinesWrap) tugBaselinesWrap.style.width = `${baseWS}%`;
          if (varPtsBar) {
            if (varWS <= 0) {
              varPtsBar.style.display = 'none';
              varPtsBar.style.width = '0%';
            } else {
              varPtsBar.style.display = 'flex';
              varPtsBar.style.width = `${varWS}%`;
              setText('variantPtsBarText', varWS > 6 ? `${varWS.toFixed(1)}%` : '');
            }
          }
          if (data.snakes_list && data.snakes_list.length > 0) {
            const baseSnakes = data.snakes_list.filter(s => !s.is_variant);
            const totalBasePts = baseSnakes.reduce((acc, s) => acc + (s.points || 0), 0);
            if (totalBasePts <= 0) {
              baseSegs.forEach(seg => {
                if (seg) {
                  seg.style.display = 'none';
                  seg.style.width = '0%';
                }
              });
            } else if (baseSnakes.length === 1) {
              if (baseSeg1) {
                baseSeg1.style.display = 'flex';
                baseSeg1.style.width = '100%';
                setText('baseSeg1Text', baseWS > 6 ? `Base ${baseWS.toFixed(1)}%` : '');
              }
              if (baseSeg2) { baseSeg2.style.display = 'none'; baseSeg2.style.width = '0%'; }
              if (baseSeg3) { baseSeg3.style.display = 'none'; baseSeg3.style.width = '0%'; }
            } else {
              baseSegs.forEach((seg, i) => {
                if (seg && baseSnakes[i]) {
                  const pts = baseSnakes[i].points || 0;
                  if (pts <= 0) {
                    seg.style.display = 'none';
                    seg.style.width = '0%';
                  } else {
                    const wPct = (pts / totalBasePts) * 100;
                    seg.style.display = 'flex';
                    seg.style.width = `${wPct}%`;
                    setText(`baseSeg${i + 1}Text`, (baseWS * (wPct / 100)) > 7 ? `B${i + 1} ${(baseSnakes[i].win_share).toFixed(1)}%` : '');
                  }
                }
              });
            }
          }
        }
      } else {
        if (tugBaselinesWrap) tugBaselinesWrap.style.width = `${baseWS}%`;
        if (varPtsBar) {
          if (played > 0 && varWS <= 0) {
            varPtsBar.style.display = 'none';
            varPtsBar.style.width = '0%';
          } else {
            varPtsBar.style.display = 'flex';
            varPtsBar.style.width = `${varWS}%`;
            setText('variantPtsBarText', varWS > 6 ? `${varWS.toFixed(1)}%` : '');
          }
        }
        if (baseSeg1) {
          if (played > 0 && baseWS <= 0) {
            baseSeg1.style.display = 'none';
            baseSeg1.style.width = '0%';
          } else {
            baseSeg1.style.display = 'flex';
            baseSeg1.style.width = '100%';
            setText('baseSeg1Text', baseWS > 6 ? `${baseWS.toFixed(1)}%` : '');
          }
        }
        if (baseSeg2) { baseSeg2.style.display = 'none'; baseSeg2.style.width = '0%'; }
        if (baseSeg3) { baseSeg3.style.display = 'none'; baseSeg3.style.width = '0%'; }
      }
      // Runs Card & ETA Calculation
      const maxG = data.target_games || data.max_games || 300;
      setText('runsValue', `${played} / ${maxG} Games`);
      const pctComplete = maxG > 0 ? ((played / maxG) * 100).toFixed(1) : 0;
      setWidth('runsProgress', pctComplete);

      const remainingGames = Math.max(0, maxG - played);
      if (data.status === 'FINISHED' || data.status === 'STOPPED' || remainingGames === 0) {
        setText('runsSubtext', `${pctComplete}% Complete (${data.status === 'STOPPED' ? 'Stopped' : 'Done'})`);
      } else if (data.status === 'DISCONNECTED') {
        setText('runsSubtext', `${pctComplete}% Complete (Disconnected)`);
      } else if (pace > 0 && isRunning) {
        const etaSec = remainingGames / pace;
        setText('runsSubtext', `${pctComplete}% Complete (~${formatSeconds(etaSec)} left)`);
      } else {
        setText('runsSubtext', `${pctComplete}% Complete (Calculating ETA...)`);
      }

      // Banner (only show when test is finished with final recommendation, stopped, or disconnected)
      const banner = document.getElementById('decisionBanner');
      const bannerTitle = document.getElementById('bannerTitle');
      const bannerDesc = document.getElementById('bannerDesc');

      if (banner) {
        if (data.status === 'DISCONNECTED') {
          if (banner.style.display !== 'flex') banner.style.display = 'flex';
          if (banner.className !== 'banner revert') banner.className = 'banner revert';
          setHTML(bannerTitle, '⚠️ DISCONNECTED');
          setText(bannerDesc, `${data.stop_reason || 'The simulation runner is not active.'} Paused at ${played}/${maxG} games.`);
        } else if (data.status === 'FINISHED' || data.status === 'STOPPED') {
          if (banner.style.display !== 'flex') banner.style.display = 'flex';
          const targetStr = is4p ? '25.0% Individual Parity' : '50.0% Parity';
          if (data.stop_type === 'MANUAL_ABORT' || data.status === 'STOPPED') {
            if (banner.className !== 'banner info') banner.className = 'banner info';
            setHTML(bannerTitle, '⏹️ TEST STOPPED / CANCELLED');
            setText(bannerDesc, `${data.stop_reason || 'Simulation run stopped by user.'} Progress: ${played}/${maxG} games played.`);
          } else if (data.stop_type === 'USER_SKIPPED' || data.recommendation === 'SKIPPED') {
            if (banner.className !== 'banner info') banner.className = 'banner info';
            setHTML(bannerTitle, '⏭️ TEST SKIPPED BY USER');
            setText(bannerDesc, `${data.stop_reason || 'Simulation mode skipped by user.'} Progress: ${played}/${maxG} games played.`);
          } else if (data.recommendation === 'KEEP') {
            if (banner.className !== 'banner keep') banner.className = 'banner keep';
            setHTML(bannerTitle, '🎉 RECOMMENDATION: KEEP VARIANT');
            setText(bannerDesc, `${data.stop_reason || ''} Final Win Share: ${varWS.toFixed(1)}% (${varPts.toFixed(1)} Pts) vs Target: ${targetStr}`);
          } else if (data.recommendation === 'PARITY') {
            if (banner.className !== 'banner parity') banner.className = 'banner parity';
            setHTML(bannerTitle, '🟡 RECOMMENDATION: PARITY (NO REGRESSION)');
            setText(bannerDesc, `${data.stop_reason || ''} Final Win Share: ${varWS.toFixed(1)}% (${varPts.toFixed(1)} Pts) vs Target: ${targetStr}. Confirmed no regression; suitable for bug fixes and neutral refactors.`);
          } else {
            if (banner.className !== 'banner revert') banner.className = 'banner revert';
            setHTML(bannerTitle, '⚠️ RECOMMENDATION: REVERT VARIANT');
            setText(bannerDesc, `${data.stop_reason || ''} Final Win Share: ${varWS.toFixed(1)}% (${varPts.toFixed(1)} Pts) vs Target: ${targetStr}`);
          }
        } else {
          if (banner.style.display !== 'none') banner.style.display = 'none';
        }
      }

      // Run History Table (Only re-render DOM rows if history list content actually changed)
      const historyBody = document.getElementById('historyTableBody');
      const historyCount = document.getElementById('historyCount');
      if (historyBody) {
        const historyList = data.run_history || [];
        const currentHistoryJSON = JSON.stringify(historyList);
        if (currentHistoryJSON !== lastHistoryJSON) {
          lastHistoryJSON = currentHistoryJSON;
          if (historyList.length === 0) {
            setText(historyCount, '0 runs');
            setHTML(historyBody, `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2rem;">No previous simulation runs recorded.</td></tr>`);
          } else {
            const recentHistory = [...historyList].slice(-10).reverse();
            setText(historyCount, historyList.length < 10 ? `Last ${historyList.length}` : 'Last 10');
            const rowsHtml = recentHistory.map((run) => {
              const rec = run.recommendation || 'PENDING';
              let pillClass = 'draw';
              let badgeText = rec;
              if (rec === 'KEEP') {
                pillClass = 'variant';
                badgeText = '🎉 KEEP';
              } else if (rec === 'PARITY') {
                pillClass = 'parity';
                badgeText = '🟡 PARITY';
              } else if (rec === 'REVERT') {
                pillClass = 'revert';
                badgeText = '⚠️ REVERT';
              } else if (rec === 'SKIPPED') {
                pillClass = 'draw';
                badgeText = '⏭️ SKIPPED';
              } else if (rec === 'TEST_FAILURE') {
                pillClass = 'revert';
                badgeText = '❌ TEST FAILED';
              }
              const rawDesc = run.experiment_desc || run.description || 'Unspecified Experiment';
              const expDesc = escapeHtml(rawDesc);

              const timeParts = String(run.timestamp || '').split(/[ T]/);
              const dateStr = timeParts[0] || '-';
              const timeStr = timeParts[1] || '';
              const timeCell = timeStr 
                ? `<div style="line-height: 1.25;"><div>${dateStr}</div><div style="color: var(--text-muted); font-size: 0.76rem;">${timeStr}</div></div>`
                : dateStr;

              // Helper to resolve mode stats from matrix_results or legacy run fields
              const getModeData = (modeKey) => {
                if (run.matrix_results && Array.isArray(run.matrix_results)) {
                  const m = run.matrix_results.find(item => item.mode === modeKey);
                  if (m) {
                    const ws = (typeof m.var_win_share === 'number') ? m.var_win_share : null;
                    const games = m.total_played || m.games || 0;
                    const dec = m.decision || (m.passed ? 'KEEP' : 'FAILED');
                    return { winShare: ws, games: games, decision: dec };
                  }
                }
                // Legacy fallback for single-mode runs
                if (run.game_mode === modeKey || (modeKey === 'duel' && run.num_snakes === 2) || (modeKey === 'standard' && (!run.game_mode || run.game_mode === 'standard'))) {
                  if (run.variant) {
                    const ws = (typeof run.variant.win_share === 'number') ? run.variant.win_share : (typeof run.variant.rate === 'number' ? run.variant.rate : null);
                    const games = run.total_games || 0;
                    const dec = run.recommendation || '';
                    return { winShare: ws, games: games, decision: dec };
                  }
                }
                return null;
              };

              const renderModeCell = (modeData, target) => {
                if (!modeData || modeData.winShare == null) {
                  return `<div style="color: var(--text-muted); font-size: 0.85rem;">—</div>`;
                }
                const ws = modeData.winShare;
                const dec = modeData.decision;
                let color = 'var(--text-main)';
                if (dec === 'KEEP' || ws >= target + 0.5) {
                  color = 'var(--variant-color)';
                } else if (dec === 'PARITY' || (ws >= target - 3.0 && ws <= target + 3.0)) {
                  color = '#f59e0b';
                } else if (dec === 'REVERT' || dec === 'FAILED' || ws < target - 3.0) {
                  color = 'var(--danger-color)';
                } else if (dec === 'SKIPPED') {
                  color = '#fbbf24';
                }
                const gStr = modeData.games > 0 ? `${modeData.games}G` : '';
                return `
                  <div style="line-height: 1.25;">
                    <div style="font-weight: 700; color: ${color}; font-size: 0.92rem;">${ws.toFixed(1)}%</div>
                    <div style="color: var(--text-muted); font-size: 0.72rem;">${gStr}${gStr && dec ? ' • ' : ''}${dec ? dec : ''}</div>
                  </div>
                `;
              };

              const constCell = renderModeCell(getModeData('constrictor'), 25.0);
              const royaleCell = renderModeCell(getModeData('royale'), 25.0);
              const duelCell = renderModeCell(getModeData('duel'), 50.0);
              const standardCell = renderModeCell(getModeData('standard'), 25.0);

              return `
                <tr>
                  <td style="white-space: nowrap; min-width: 85px; text-align: left;">${timeCell}</td>
                  <td class="col-desc" style="font-weight: 600; color: var(--text-main); font-size: 0.85rem; text-align: left;">
                    ${expDesc}
                    <span class="tooltip-text">${expDesc}</span>
                  </td>
                  <td style="white-space: nowrap; min-width: 80px; text-align: center;">${constCell}</td>
                  <td style="white-space: nowrap; min-width: 80px; text-align: center;">${royaleCell}</td>
                  <td style="white-space: nowrap; min-width: 80px; text-align: center;">${duelCell}</td>
                  <td style="white-space: nowrap; min-width: 80px; text-align: center;">${standardCell}</td>
                  <td style="white-space: nowrap; min-width: 85px; text-align: center;"><span class="winner-pill ${pillClass}">${badgeText}</span></td>
                </tr>
              `;
            }).join('');
            setHTML(historyBody, rowsHtml);
          }
        }
      }
    } catch (e) {
      console.error('Dashboard update error:', e);
      const statusDot = document.getElementById('statusDot');
      const statusText = document.getElementById('statusText');
      if (statusDot && statusText) {
        statusDot.style.animation = 'none';
        statusDot.style.backgroundColor = 'var(--draw-color)';
        statusText.innerText = 'DISCONNECTED';
      }
    }
  }

  async function skipActiveMode() {
    if (!confirm('Are you sure you want to skip the current test mode and advance to the next?')) return;
    const skipBtn = document.getElementById('skipBtnHeader');
    if (skipBtn) {
      skipBtn.disabled = true;
      skipBtn.style.opacity = '0.6';
      skipBtn.style.pointerEvents = 'none';
      skipBtn.innerHTML = '<span>⏳</span> <span id="skipBtnText">Skipping...</span>';
    }
    try {
      const res = await fetch('/api/skip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'skip' })
      });
      const data = await res.json();
      console.log('Skip response:', data);
      await updateDashboard();
    } catch (err) {
      console.error('Failed to skip test mode:', err);
      alert('Error requesting skip: ' + err);
      if (skipBtn) {
        skipBtn.disabled = false;
        skipBtn.style.opacity = '1';
        skipBtn.style.pointerEvents = 'auto';
        skipBtn.innerHTML = '<span>⏭️</span> <span id="skipBtnText">Skip Mode</span>';
      }
    }
  }

  async function cancelActiveRun() {
    if (!confirm('Are you sure you want to stop the active A/B test run?')) return;
    const btns = document.querySelectorAll('.btn-cancel');
    btns.forEach(b => {
      b.disabled = true;
      b.innerHTML = '<span>⏳</span> <span>Stopping...</span>';
    });
    try {
      const res = await fetch('/api/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'cancel' })
      });
      const data = await res.json();
      console.log('Cancel response:', data);
      await updateDashboard();
    } catch (err) {
      console.error('Failed to cancel test:', err);
      alert('Error requesting stop: ' + err);
      btns.forEach(b => {
        b.disabled = false;
        b.innerHTML = '<span>⏹️</span> <span>Stop Run</span>';
      });
    }
  }

  setInterval(updateDashboard, 500);
  updateDashboard();
</script>
</body>
</html>
"""

def stop_active_tasks(state: DashboardState) -> Dict[str, Any]:
    import signal
    killed_pids = []
    candidate_pids = set()
    current_pid = os.getpid()
    parent_pid = os.getppid()

    with state.lock:
        if state.active_pid and isinstance(state.active_pid, int):
            if state.active_pid not in (current_pid, parent_pid):
                candidate_pids.add(state.active_pid)

    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid in (current_pid, parent_pid):
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="ignore").replace("\x00", " ")
                if "ab_test.py" in cmdline and "ab_dashboard.py" not in cmdline and "-c" not in cmdline:
                    candidate_pids.add(pid)
            except (OSError, IOError):
                continue
    except Exception:
        pass

    for p in list(candidate_pids):
        try:
            os.kill(p, 0)
        except OSError:
            continue

        try:
            os.kill(p, signal.SIGINT)
            killed_pids.append(p)
        except Exception:
            try:
                os.kill(p, signal.SIGTERM)
                killed_pids.append(p)
            except Exception:
                pass

    if killed_pids:
        time.sleep(0.3)
        for p in killed_pids:
            try:
                os.kill(p, 0)
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass

    with state.lock:
        state.status = "STOPPED"
        state.stop_type = "MANUAL_ABORT"
        state.stop_reason = "Run stopped by user via dashboard Stop button."
        state.recommendation = "PENDING"
        state.end_time = time.time()
        state.active_pid = None
        state.last_activity_time = time.time()

    return {"status": "ok", "killed_pids": killed_pids}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    state: Optional[DashboardState] = None

    def do_GET(self):
        if self.path == "/api/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        if self.path == "/api/check_skip":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            skipped = False
            if self.state:
                with self.state.lock:
                    skipped = self.state.skip_requested
                    self.state.skip_requested = False
            self.wfile.write(json.dumps({"skip": skipped}).encode("utf-8"))
            return

        if self.path == "/api/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.state:
                self.state.touch()
                data = json.dumps(self.state.to_dict()).encode("utf-8")
                self.wfile.write(data)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

    def do_POST(self):
        if self.path == "/api/skip":
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                self.rfile.read(content_length)
            resp_data = {"status": "error", "message": "No state initialized"}
            if self.state:
                with self.state.lock:
                    self.state.skip_requested = True
                    self.state.last_activity_time = time.time()
                resp_data = {"status": "ok", "skip_requested": True}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            return

        if self.path in ("/api/cancel", "/api/stop"):
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                self.rfile.read(content_length)
            resp_data = {"status": "error", "message": "No state initialized"}
            if self.state:
                resp_data = stop_active_tasks(self.state)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            return

        if self.path == "/api/update":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode('utf-8'))
                if self.state:
                    self.state.handle_update(payload)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "updated"}).encode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            return

    def log_message(self, format, *args):
        pass

class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def start_inactivity_watchdog(state: DashboardState, timeout_seconds: int = 600):
    def _watch():
        while True:
            time.sleep(15)
            with state.lock:
                idle_time = time.time() - state.last_activity_time
                is_running = (state.status == "RUNNING")

            if idle_time > timeout_seconds and not is_running:
                print(f"\n[Dashboard] Inactive for {timeout_seconds // 60} minutes with no active tests. Auto-shutting down dashboard server...")
                os._exit(0)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()

def start_dashboard_server(
    state: Optional[DashboardState] = None,
    preferred_port: int = 8888,
    timeout_seconds: int = 600
) -> Tuple[socketserver.TCPServer, int]:
    if state is None:
        state = DashboardState()
    DashboardHandler.state = state

    actual_port = preferred_port
    server = None

    for p in range(preferred_port, preferred_port + 25):
        try:
            server = ReusableTCPServer(("0.0.0.0", p), DashboardHandler)
            actual_port = server.server_address[1]
            break
        except OSError:
            continue

    if not server:
        raise RuntimeError(f"Could not bind dashboard HTTP server on ports {preferred_port}-{preferred_port+25}")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    start_inactivity_watchdog(state, timeout_seconds=timeout_seconds)

    url = f"http://localhost:{actual_port}"
    print(f"  [Dashboard] Live dashboard server running at {url}")

    return server, actual_port


class DashboardClient:
    def __init__(self, port: int = 8888):
        self.port = port
        self.url = f"http://localhost:{port}"

    def is_running(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.url}/api/ping")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                return resp.status == 200
        except Exception:
            return False

    def post_update(self, payload: dict, sync: bool = False):
        def _send():
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.url}/api/update",
                    data=data,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    pass
            except Exception:
                pass
        if sync:
            _send()
        else:
            threading.Thread(target=_send, daemon=True).start()

    def register_pid(self, pid: Optional[int] = None):
        if pid is None:
            pid = os.getpid()
        self.post_update({"action": "register_pid", "pid": pid})

    def cancel_test(self) -> Dict[str, Any]:
        try:
            req = urllib.request.Request(
                f"{self.url}/api/cancel",
                data=b"{}",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def check_skip(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.url}/api/check_skip")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("skip", False))
        except Exception:
            return False

    def skip_mode(self) -> Dict[str, Any]:
        try:
            req = urllib.request.Request(
                f"{self.url}/api/skip",
                data=b"{}",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def init_test(
        self,
        strategy_baseline: str,
        strategy_variant: str,
        min_games: int,
        max_games: int,
        check_interval: int,
        early_stop_high: float,
        early_stop_low: float,
        threads: int,
        experiment_desc: str,
        game_mode: str = "standard",
        num_snakes: int = 2,
        target_win_share: Optional[float] = None,
        matrix_progress: Optional[list] = None,
        pid: Optional[int] = None,
    ):
        payload = {
            "action": "init",
            "pid": pid if pid is not None else os.getpid(),
            "strategy_baseline": strategy_baseline,
            "strategy_variant": strategy_variant,
            "min_games": min_games,
            "max_games": max_games,
            "check_interval": check_interval,
            "early_stop_high": early_stop_high,
            "early_stop_low": early_stop_low,
            "threads": threads,
            "experiment_desc": experiment_desc,
            "game_mode": game_mode,
            "num_snakes": num_snakes,
            "target_win_share": target_win_share if target_win_share is not None else (25.0 if num_snakes == 4 else 50.0),
        }
        if matrix_progress is not None:
            payload["matrix_progress"] = matrix_progress
        self.post_update(payload, sync=True)

    def update_matrix(self, matrix_progress: list):
        self.post_update({
            "action": "update_matrix",
            "matrix_progress": matrix_progress,
        }, sync=True)

    def update_game(self, game_num: int, seed: int, winner: str, turns: int = 0):
        self.post_update({
            "action": "update_game",
            "game_num": game_num,
            "seed": seed,
            "winner": winner,
            "turns": turns,
        })

    def set_finished(self, stop_type: str, stop_reason: str, recommendation: str, csv_path: str):
        self.post_update({
            "action": "set_finished",
            "stop_type": stop_type,
            "stop_reason": stop_reason,
            "recommendation": recommendation,
            "csv_path": csv_path,
        }, sync=True)


def ensure_dashboard_running(port: int = 8888, timeout_seconds: int = 600) -> DashboardClient:
    client = DashboardClient(port=port)
    if not client.is_running():
        script_path = str(Path(__file__).resolve())
        cmd = [sys.executable, script_path, "--port", str(port), "--timeout", str(timeout_seconds)]

        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        start_t = time.time()
        while time.time() - start_t < 3.0:
            if client.is_running():
                print(f"  [Dashboard] Started background dashboard server at http://localhost:{port}")
                break
            time.sleep(0.1)
        else:
            print(f"  [!] Could not connect to background dashboard on port {port}")
            return client
    else:
        print(f"  [Dashboard] Connected to active background dashboard at http://localhost:{port}")

    try:
        client.register_pid(os.getpid())
    except Exception:
        pass

    return client


def main():
    parser = argparse.ArgumentParser(description="Battlesnake A/B Test Web Dashboard Server")
    parser.add_argument("--port", type=int, default=8888, help="Port to run dashboard server on")
    parser.add_argument("--timeout", type=int, default=600, help="Inactivity auto-kill timeout in seconds (default: 600s / 10m)")
    parser.add_argument("--load-state", type=str, default="", help="Path to JSON state file to restore on startup")
    args = parser.parse_args()

    state = DashboardState()
    if args.load_state and os.path.exists(args.load_state):
        try:
            with open(args.load_state, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
            state.handle_update({"action": "restore", "data": saved_data})
            print(f"[*] Restored dashboard state from {args.load_state}")
        except Exception as e:
            print(f"[!] Failed to restore state: {e}")

    print(f"[*] Starting Battlesnake A/B Dashboard Server on port {args.port}...")
    start_dashboard_server(state, preferred_port=args.port, timeout_seconds=args.timeout)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Dashboard server stopped.")

if __name__ == "__main__":
    main()
