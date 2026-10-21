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
import collections
import http.server
import json
import os
import socketserver
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Tuple, Dict, Any, Optional, List

_HISTORY_CACHE: Optional[list] = None
_HISTORY_MTIME: float = 0.0

MODE_TIMINGS_FILE = Path(__file__).resolve().parent / "mode_timings.json"
_MODE_TIMINGS_CACHE: Optional[list] = None
_MODE_TIMINGS_MTIME: float = 0.0


def load_mode_timings(timings_path: Optional[Path] = None) -> list:
    """Loads persistent mode timing history from mode_timings.json with mtime caching."""
    global _MODE_TIMINGS_CACHE, _MODE_TIMINGS_MTIME
    p = timings_path or MODE_TIMINGS_FILE
    if not p.is_file():
        return []
    try:
        mtime = p.stat().st_mtime
        if _MODE_TIMINGS_CACHE is not None and mtime == _MODE_TIMINGS_MTIME and timings_path is None:
            return _MODE_TIMINGS_CACHE
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                if timings_path is None:
                    _MODE_TIMINGS_CACHE = data
                    _MODE_TIMINGS_MTIME = mtime
                return data
    except Exception:
        if _MODE_TIMINGS_CACHE is not None and timings_path is None:
            return _MODE_TIMINGS_CACHE
    return []


def append_mode_timing(
    mode: str,
    run_type: str = "self",
    games: int = 0,
    elapsed_seconds: float = 0.0,
    timings_path: Optional[Path] = None,
) -> None:
    """Appends {mode, run_type, games, elapsed_seconds} to persistent mode_timings.json."""
    if not mode or games <= 0 or elapsed_seconds <= 0:
        return
    p = timings_path or MODE_TIMINGS_FILE
    mode_clean = str(mode).strip().lower()
    run_type_clean = str(run_type).strip().lower()
    new_entry = {
        "mode": mode_clean,
        "run_type": run_type_clean,
        "games": int(games),
        "elapsed_seconds": round(float(elapsed_seconds), 2),
    }

    entries = []
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list):
                    entries = d
        except Exception:
            entries = []

    # Deduplicate if last entry is identical
    if entries:
        last = entries[-1]
        if (
            last.get("mode") == new_entry["mode"]
            and last.get("run_type") == new_entry["run_type"]
            and last.get("games") == new_entry["games"]
            and abs(last.get("elapsed_seconds", 0) - new_entry["elapsed_seconds"]) < 5.0
        ):
            return

    entries.append(new_entry)
    tmp_path = p.with_suffix(".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp_path, p)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def get_mode_medians(
    timings: list,
    target_run_type: str = "self",
) -> Dict[str, float]:
    """Computes median seconds-per-game per mode for the target run_type, falling back to any run_type."""
    by_mode_runtype = collections.defaultdict(list)
    by_mode_any = collections.defaultdict(list)

    target_rt = target_run_type.strip().lower()
    for entry in timings:
        m = str(entry.get("mode", "")).strip().lower()
        rt = str(entry.get("run_type", "self")).strip().lower()
        g = entry.get("games", 0)
        el = entry.get("elapsed_seconds", 0.0)
        if m and g > 0 and el > 0:
            spg = float(el) / float(g)
            by_mode_runtype[(m, rt)].append(spg)
            by_mode_any[m].append(spg)

    medians = {}
    for m in by_mode_any:
        if (m, target_rt) in by_mode_runtype:
            medians[m] = float(statistics.median(by_mode_runtype[(m, target_rt)]))
        elif m in by_mode_any:
            medians[m] = float(statistics.median(by_mode_any[m]))
    return medians


def calculate_eta(
    matrix_progress: Optional[list],
    current_mode: str,
    current_played: int,
    current_max_games: int,
    current_mode_elapsed: float,
    run_type: str = "self",
    mode_timings: Optional[list] = None,
    timings_path: Optional[Path] = None,
    min_stable_games: int = 20,
    depth_data: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Computes total ETA across all modes with remaining games:
    1. For depth test runs (run_type="depth"):
       Uses remaining positions * repeats * budget (blended with live position rate).
    2. For current mode:
       If current_played >= min_stable_games, blends using live rate (mode_elapsed / current_played).
       Before threshold, uses history median for this mode and run_type (self or pool).
    3. For modes still to come:
       Uses history median seconds-per-game for that mode and run_type.
    4. A mode with no history falls back to current mode's rate.
    5. A run with no history at all behaves exactly as dashboard does now (current mode remaining / pace).
    """
    if run_type == "depth":
        depth_info = depth_data or {}
        total_pos = depth_info.get("total_positions", 12) or 12
        completed_pos = depth_info.get("completed_positions", current_played) or current_played
        rem_pos = max(0, total_pos - completed_pos)
        time_ms = depth_info.get("time_ms", 450) or 450
        repeats = depth_info.get("repeats", 5) or 5
        live_spp = (current_mode_elapsed / completed_pos) if (completed_pos > 0 and current_mode_elapsed > 0) else None
        budget_spp = (repeats * 2 * time_ms) / 1000.0
        spg = live_spp if live_spp is not None else budget_spp
        eta_sec = rem_pos * spg
        return {
            "total_eta_seconds": round(eta_sec, 1),
            "has_history": True,
            "is_stable": bool(completed_pos >= 2 and live_spp is not None),
            "breakdown": [{
                "mode": "depth_fixtures",
                "games_left": rem_pos,
                "rate_spg": round(spg, 3),
                "rate_source": "live" if live_spp is not None else "budget",
                "eta_seconds": round(eta_sec, 1),
            }],
        }

    timings = mode_timings if mode_timings is not None else load_mode_timings(timings_path)
    has_any_history = bool(timings and len(timings) > 0)

    effective_min_stable = 10 if run_type == "pool" else min_stable_games
    curr_live_spg = (current_mode_elapsed / current_played) if (current_played > 0 and current_mode_elapsed > 0) else None
    is_curr_stable = bool(current_played >= effective_min_stable and curr_live_spg is not None)

    # Legacy fallback when no history at all
    if not has_any_history:
        rem = max(0, current_max_games - current_played)
        legacy_eta = (rem * curr_live_spg) if curr_live_spg is not None else None
        return {
            "total_eta_seconds": round(legacy_eta, 1) if legacy_eta is not None else None,
            "has_history": False,
            "is_stable": is_curr_stable,
            "breakdown": [{
                "mode": current_mode,
                "games_left": rem,
                "rate_spg": round(curr_live_spg, 3) if curr_live_spg is not None else None,
                "rate_source": "legacy",
                "eta_seconds": round(legacy_eta, 1) if legacy_eta is not None else None,
            }],
        }

    medians = get_mode_medians(timings, target_run_type=run_type)

    # Fallback rate for modes with no history:
    # "A mode with no history falls back to the current mode's rate (today's behavior)."
    curr_mode_clean = str(current_mode).strip().lower()
    fallback_spg = curr_live_spg if curr_live_spg is not None else medians.get(curr_mode_clean)

    # Determine modes with remaining games
    modes_to_calc = []
    if matrix_progress and isinstance(matrix_progress, list):
        for item in matrix_progress:
            m_name = str(item.get("mode", "")).strip().lower()
            status = str(item.get("status", "PENDING")).strip().upper()
            m_max = item.get("max_games", 200) or 200
            is_curr = (m_name == curr_mode_clean or status == "RUNNING")
            if is_curr:
                m_left = max(0, (current_max_games or m_max) - current_played)
            else:
                if status in ("PASSED", "PARITY", "FAILED", "SKIPPED"):
                    m_left = 0
                else:
                    m_left = m_max
            modes_to_calc.append((m_name, m_left, is_curr))
    else:
        m_left = max(0, current_max_games - current_played)
        modes_to_calc.append((curr_mode_clean, m_left, True))

    total_eta = 0.0
    breakdown = []
    has_valid_rate = False

    for m_name, m_left, is_curr in modes_to_calc:
        if m_left <= 0:
            continue

        spg = None
        source = "history"

        if is_curr:
            if is_curr_stable:
                spg = curr_live_spg
                source = "live"
            else:
                spg = medians.get(m_name) or fallback_spg
                source = "history" if m_name in medians else "fallback"
        else:
            spg = medians.get(m_name) or fallback_spg
            source = "history" if m_name in medians else "fallback"

        if spg is not None and spg > 0:
            has_valid_rate = True
            mode_eta = m_left * spg
            total_eta += mode_eta
            breakdown.append({
                "mode": m_name,
                "games_left": m_left,
                "rate_spg": round(spg, 3),
                "rate_source": source,
                "eta_seconds": round(mode_eta, 1),
            })
        else:
            breakdown.append({
                "mode": m_name,
                "games_left": m_left,
                "rate_spg": None,
                "rate_source": "none",
                "eta_seconds": None,
            })

    final_eta = round(total_eta, 1) if has_valid_rate else None
    return {
        "total_eta_seconds": final_eta,
        "has_history": True,
        "is_stable": is_curr_stable,
        "breakdown": breakdown,
    }

def load_run_history(max_entries: int = 15) -> list:
    global _HISTORY_CACHE, _HISTORY_MTIME
    history_file = Path(__file__).resolve().parent.parent / "ab_results" / "ab_history.json"
    if not history_file.exists():
        history_file = Path(__file__).parent / "ab_results" / "ab_history.json"
    if history_file.exists():
        try:
            mtime = history_file.stat().st_mtime
            if _HISTORY_CACHE is not None and mtime == _HISTORY_MTIME:
                return _HISTORY_CACHE
            with history_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    sliced = data[-max_entries:] if len(data) > max_entries else data
                    for entry in sliced:
                        if isinstance(entry, dict):
                            entry.pop("games", None)
                            if "matrix_results" in entry and isinstance(entry["matrix_results"], list):
                                for mr in entry["matrix_results"]:
                                    if isinstance(mr, dict):
                                        mr.pop("games", None)
                                        mr.pop("rows", None)
                            for key in ("baseline", "variant"):
                                sub = entry.get(key)
                                if isinstance(sub, dict):
                                    for num_k in ("wins", "losses", "draws"):
                                        if isinstance(sub.get(num_k), float):
                                            sub[num_k] = round(sub[num_k], 2)
                            if isinstance(entry.get("draws"), float):
                                entry["draws"] = round(entry["draws"], 2)
                    _HISTORY_CACHE = sliced
                    _HISTORY_MTIME = mtime
                    return sliced
        except Exception:
            if _HISTORY_CACHE is not None:
                return _HISTORY_CACHE
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
        status: str = "IDLE",
    ):
        self.lock = threading.RLock()
        self.status = status
        self.stop_type = "IDLE" if status == "IDLE" else "IN_PROGRESS"
        self.stop_reason = "" if status == "RUNNING" else "No active benchmark running"
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
        self.run_type = "self"

        self.pool_data: Dict[str, Any] = {
            "cand_score": 0.0,
            "base_score": 0.0,
            "cand_wins": 0,
            "base_wins": 0,
            "total_games": 0,
            "cand_share": 0.0,
            "base_share": 0.0,
            "delta": 0.0,
            "z_score": 0.0,
            "opponents": [],
            "opponents_map": {},
            "mode_stats": {},
        }
        self.depth_data: Dict[str, Any] = {
            "completed_positions": 0,
            "total_positions": 12,
            "time_ms": 450,
            "repeats": 5,
            "median_var_depth": 0.0,
            "median_base_depth": 0.0,
            "delta_depth": 0.0,
            "nps_ratio": 1.0,
            "agreement_pct": 100.0,
            "passed": True,
            "fixtures": [],
            "mode_summaries": {},
            "aggregate": {},
        }

        self.wins_baseline = 0
        self.losses_baseline = 0
        self.draws_baseline = 0

        self.wins_variant = 0
        self.losses_variant = 0
        self.draws_variant = 0

        self.snakes_stats = self._init_snakes_stats(num_snakes, strategy_variant, strategy_baseline)
        self.unit_tests_data: Dict[str, Any] = {
            "status": "PENDING",
            "passed": 0,
            "total": 40,
            "details": "",
        }

        self.suite_start_time: Optional[float] = None
        self._last_desc: str = experiment_desc
        self.start_time: Optional[float] = time.time() if status == "RUNNING" else None
        self.end_time: Optional[float] = None
        self.csv_path = ""
        self.recommendation = "PENDING"
        self.skip_requested: bool = False
        self.last_activity_time = time.time()
        self.last_game_time: float = 0.0

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
                if self.total_played > 0 and self.start_time:
                    prev_elapsed = (self.end_time or time.time()) - self.start_time
                    append_mode_timing(
                        mode=self.game_mode,
                        run_type=getattr(self, "run_type", "self"),
                        games=self.total_played,
                        elapsed_seconds=prev_elapsed,
                    )
                self.run_type = payload.get("run_type", getattr(self, "run_type", "self"))
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
                new_desc = payload.get("experiment_desc", "Unspecified Experiment")
                self.experiment_desc = new_desc
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
                self.pool_data = {
                    "cand_score": 0.0,
                    "base_score": 0.0,
                    "cand_wins": 0,
                    "base_wins": 0,
                    "total_games": 0,
                    "cand_share": 0.0,
                    "base_share": 0.0,
                    "delta": 0.0,
                    "z_score": 0.0,
                    "opponents": [],
                    "opponents_map": {},
                    "mode_stats": {},
                }
                self.depth_data = {
                    "completed_positions": 0,
                    "total_positions": 12,
                    "time_ms": 450,
                    "repeats": 5,
                    "median_var_depth": 0.0,
                    "median_base_depth": 0.0,
                    "delta_depth": 0.0,
                    "nps_ratio": 1.0,
                    "agreement_pct": 100.0,
                    "passed": True,
                    "fixtures": [],
                    "mode_summaries": {},
                    "aggregate": {},
                }
                self.snakes_stats = self._init_snakes_stats(self.num_snakes, self.strategy_variant, self.strategy_baseline)
                self.start_time = time.time()
                explicit_suite = payload.get("suite_start_time")
                if explicit_suite:
                    self.suite_start_time = float(explicit_suite)
                elif self.suite_start_time is None or getattr(self, "_last_desc", None) != new_desc:
                    self.suite_start_time = self.start_time
                self._last_desc = new_desc
                self.end_time = None
                self.csv_path = ""
                self.recommendation = "PENDING"
                self.skip_requested = False
            elif action == "update_matrix":
                self.matrix_progress = payload.get("matrix_progress", self.matrix_progress)
            elif action == "update_game":
                if self.status in ("DISCONNECTED", "IDLE"):
                    self.status = "RUNNING"
                    self.stop_type = "IN_PROGRESS"
                    self.stop_reason = ""
                if not self.start_time:
                    self.start_time = time.time()
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
            elif action == "init_depth":
                self.run_type = "depth"
                self.status = "RUNNING"
                self.stop_type = "IN_PROGRESS"
                self.stop_reason = ""
                self.start_time = time.time()
                self.last_game_time = time.time()
                self.end_time = None
                self.depth_data = {
                    "total_positions": payload.get("total_positions", 12),
                    "completed_positions": 0,
                    "time_ms": payload.get("time_ms", 450),
                    "repeats": payload.get("repeats", 5),
                    "median_var_depth": 0.0,
                    "median_base_depth": 0.0,
                    "delta_depth": 0.0,
                    "nps_ratio": 1.0,
                    "agreement_pct": 100.0,
                    "passed": True,
                    "fixtures": [],
                    "mode_summaries": {},
                    "aggregate": {},
                }
                self.target_games = payload.get("total_positions", 12)
            elif action == "update_pool_game":
                if self.status in ("DISCONNECTED", "IDLE"):
                    self.status = "RUNNING"
                    self.stop_type = "IN_PROGRESS"
                    self.stop_reason = ""
                if not self.start_time:
                    self.start_time = time.time()
                self.last_game_time = time.time()
                self.total_played += 1
                c_score = float(payload.get("cand_score", 0.0))
                b_score = float(payload.get("base_score", 0.0))
                self.pool_data["cand_score"] += c_score
                self.pool_data["base_score"] += b_score
                self.pool_data["total_games"] = self.total_played
                if payload.get("cand_won"):
                    self.pool_data["cand_wins"] += 1
                    self.wins_variant += 1
                if payload.get("base_won"):
                    self.pool_data["base_wins"] += 1
                    self.wins_baseline += 1
                c_sh = (self.pool_data["cand_score"] / self.total_played * 100.0) if self.total_played > 0 else 0.0
                b_sh = (self.pool_data["base_score"] / self.total_played * 100.0) if self.total_played > 0 else 0.0
                self.pool_data["cand_share"] = round(c_sh, 1)
                self.pool_data["base_share"] = round(b_sh, 1)
                self.pool_data["delta"] = round(c_sh - b_sh, 1)
                for opp in payload.get("opponents", []):
                    if opp not in self.pool_data.setdefault("opponents_map", {}):
                        self.pool_data["opponents_map"][opp] = {"games": 0, "cand_score": 0.0, "base_score": 0.0}
                    om = self.pool_data["opponents_map"][opp]
                    om["games"] += 1
                    om["cand_score"] += c_score
                    om["base_score"] += b_score
                self.pool_data["opponents"] = [
                    {
                        "opponent": opp,
                        "games": om["games"],
                        "cand_win_pct": round(om["cand_score"] / om["games"] * 100.0, 1) if om["games"] > 0 else 0.0,
                        "base_win_pct": round(om["base_score"] / om["games"] * 100.0, 1) if om["games"] > 0 else 0.0,
                        "delta": round((om["cand_score"] - om["base_score"]) / om["games"] * 100.0, 1) if om["games"] > 0 else 0.0,
                    }
                    for opp, om in self.pool_data.get("opponents_map", {}).items()
                ]
            elif action == "update_pool_mode":
                m_mode = payload.get("mode")
                if self.matrix_progress:
                    for item in self.matrix_progress:
                        if item.get("mode") == m_mode:
                            dec = payload.get("decision", "PARITY")
                            item["status"] = "PASSED" if dec == "KEEP" else ("PARITY" if dec == "PARITY" else "FAILED")
                            item["cand_share"] = round(float(payload.get("cand_share", 0.0)), 1)
                            item["base_share"] = round(float(payload.get("base_share", 0.0)), 1)
                            item["delta"] = round(float(payload.get("delta", 0.0)), 1)
                            item["z_score"] = round(float(payload.get("z_score", 0.0)), 2)
                            item["decision"] = dec
                if payload.get("opponents"):
                    self.pool_data["opponents"] = payload.get("opponents")
            elif action == "update_depth_position":
                if self.status in ("DISCONNECTED", "IDLE"):
                    self.status = "RUNNING"
                    self.stop_type = "IN_PROGRESS"
                    self.stop_reason = ""
                if not self.start_time:
                    self.start_time = time.time()
                self.last_game_time = time.time()
                comp = payload.get("completed_positions", 0)
                tot = payload.get("total_positions", 12)
                self.total_played = comp
                self.target_games = tot
                self.depth_data["completed_positions"] = comp
                self.depth_data["total_positions"] = tot
                fixture_entry = {
                    "mode": payload.get("mode", ""),
                    "turn": payload.get("turn", 0),
                    "base_med_depth": round(float(payload.get("base_med_depth", 0.0)), 1),
                    "var_med_depth": round(float(payload.get("var_med_depth", 0.0)), 1),
                    "delta_depth": round(float(payload.get("delta_depth", 0.0)), 1),
                    "base_med_nps": int(payload.get("base_med_nps", 0)),
                    "var_med_nps": int(payload.get("var_med_nps", 0)),
                    "nps_ratio": round(float(payload.get("nps_ratio", 1.0)), 2),
                    "agreement_pct": round(float(payload.get("agreement_pct", 100.0)), 1),
                    "passed": bool(payload.get("passed", True)),
                }
                f_list = self.depth_data.setdefault("fixtures", [])
                existing_idx = next((i for i, f in enumerate(f_list) if f["mode"] == fixture_entry["mode"] and f["turn"] == fixture_entry["turn"]), None)
                if existing_idx is not None:
                    f_list[existing_idx] = fixture_entry
                else:
                    f_list.append(fixture_entry)
                var_depths = [f["var_med_depth"] for f in f_list]
                base_depths = [f["base_med_depth"] for f in f_list]
                nps_ratios = [f["nps_ratio"] for f in f_list]
                agrees = [f["agreement_pct"] for f in f_list]
                if var_depths and base_depths:
                    import statistics
                    med_v = statistics.median(var_depths)
                    med_b = statistics.median(base_depths)
                    self.depth_data["median_var_depth"] = round(med_v, 1)
                    self.depth_data["median_base_depth"] = round(med_b, 1)
                    self.depth_data["delta_depth"] = round(med_v - med_b, 1)
                if nps_ratios:
                    import statistics
                    self.depth_data["nps_ratio"] = round(statistics.median(nps_ratios), 2)
                if agrees:
                    import statistics
                    self.depth_data["agreement_pct"] = round(statistics.mean(agrees), 1)
                self.depth_data["passed"] = all(f.get("passed", True) for f in f_list)
            elif action == "finish_depth":
                self.depth_data["median_base_depth"] = round(float(payload.get("overall_base_d", 0.0)), 1)
                self.depth_data["median_var_depth"] = round(float(payload.get("overall_var_d", 0.0)), 1)
                self.depth_data["delta_depth"] = round(float(payload.get("overall_delta_d", 0.0)), 1)
                self.depth_data["nps_ratio"] = round(float(payload.get("overall_nps_r", 1.0)), 2)
                self.depth_data["agreement_pct"] = round(float(payload.get("overall_agree", 100.0)), 1)
                self.depth_data["passed"] = bool(payload.get("overall_pass", True))
            elif action == "update_unit_tests":
                self.unit_tests_data = {
                    "status": payload.get("status", "PASSED"),
                    "passed": payload.get("passed", 40),
                    "total": payload.get("total", 40),
                    "details": payload.get("details", ""),
                }
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
                if self.total_played > 0 and self.start_time:
                    mode_elapsed = self.end_time - self.start_time
                    append_mode_timing(
                        mode=self.game_mode,
                        run_type=getattr(self, "run_type", "self"),
                        games=self.total_played,
                        elapsed_seconds=mode_elapsed,
                    )

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
                if self.total_played == 0 and self.active_pid is None:
                    # No active test runner connected
                    self.status = "IDLE"
                    self.stop_type = "IDLE"
                    self.stop_reason = "No active benchmark running"
                    self.start_time = None
                else:
                    last_g_time = getattr(self, "last_game_time", self.start_time)
                    if last_g_time and (time.time() - last_g_time > 900.0):
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

            if self.status == "IDLE" or not self.start_time:
                suite_elapsed = 0.0
                mode_elapsed = 0.0
                eta_info = {"total_eta_seconds": None, "has_history": False, "is_stable": False, "breakdown": []}
            else:
                now = self.end_time or time.time()
                suite_start = getattr(self, "suite_start_time", None) or self.start_time or now
                suite_elapsed = max(0.0, now - suite_start)
                mode_start = self.start_time or now
                mode_elapsed = max(0.0, now - mode_start)

                if self.status == "RUNNING" and (self.total_played > 0 or self.active_pid is not None):
                    eta_info = calculate_eta(
                        matrix_progress=live_matrix_progress if live_matrix_progress else self.matrix_progress,
                        current_mode=self.game_mode,
                        current_played=played,
                        current_max_games=self.target_games or self.max_games or 200,
                        current_mode_elapsed=mode_elapsed,
                        run_type=getattr(self, "run_type", "self"),
                        depth_data=getattr(self, "depth_data", None),
                    )
                else:
                    eta_info = {"total_eta_seconds": None, "has_history": False, "is_stable": False, "breakdown": []}

            return {
                "status": self.status,
                "stop_type": self.stop_type,
                "stop_reason": self.stop_reason,
                "runner_alive": (self.status == "RUNNING" and (self.active_pid is not None or self.total_played > 0)),
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
                "elapsed_seconds": round(suite_elapsed, 1),
                "mode_elapsed_seconds": round(mode_elapsed, 1),
                "suite_elapsed_seconds": round(suite_elapsed, 1),
                "eta_seconds": eta_info.get("total_eta_seconds"),
                "eta_breakdown": eta_info.get("breakdown", []),
                "run_type": getattr(self, "run_type", "self"),
                "pool_data": getattr(self, "pool_data", None),
                "depth_data": getattr(self, "depth_data", None),
                "unit_tests_data": getattr(self, "unit_tests_data", {"status": "PENDING", "passed": 0, "total": 40, "details": ""}),
                "snakes_stats": self.snakes_stats,
                "run_history": load_run_history(),
                "experiment_desc": self.experiment_desc
            }

            now = time.time()
            if not hasattr(self, "_last_disk_dump") or (now - self._last_disk_dump > 2.0):
                self._last_disk_dump = now
                try:
                    with open("/tmp/dashboard_live_state.json.tmp", "w", encoding="utf-8") as f:
                        json.dump(res, f)
                    os.replace("/tmp/dashboard_live_state.json.tmp", "/tmp/dashboard_live_state.json")
                except Exception:
                    pass

            return res


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text x="50" y="50" dominant-baseline="central" text-anchor="middle" font-size="80">🐍</text></svg>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Battlesnake A/B Test Dashboard</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text x='50' y='50' dominant-baseline='central' text-anchor='middle' font-size='80'>🐍</text></svg>">
  <link rel="alternate icon" href="/favicon.ico">
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

    #elapsedTime {
      display: inline-block;
      width: 11ch;
      font-family: 'JetBrains Mono', monospace;
      font-variant-numeric: tabular-nums;
      font-feature-settings: "tnum";
      text-align: left;
    }

    .status-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background-color: var(--variant-color);
      box-shadow: 0 0 10px var(--variant-color);
      animation: pulse 1.5s infinite;
      transition: all 0.2s ease;
    }

    .badge-status-idle {
      border-color: rgba(148, 163, 184, 0.25);
      color: var(--text-muted);
    }
    .badge-status-idle .status-dot {
      background-color: var(--draw-color);
      box-shadow: none;
      animation: none;
    }

    .badge-status-running {
      border-color: rgba(56, 189, 248, 0.4);
      background: rgba(56, 189, 248, 0.1);
      color: #38bdf8;
    }
    .badge-status-running .status-dot {
      background-color: #38bdf8;
      box-shadow: 0 0 10px #38bdf8;
      animation: pulse 1.5s infinite;
    }

    .badge-status-keep {
      border-color: rgba(16, 185, 129, 0.5);
      background: rgba(16, 185, 129, 0.15);
      color: var(--variant-color);
      font-weight: 700;
    }
    .badge-status-keep .status-dot {
      background-color: var(--variant-color);
      box-shadow: 0 0 10px var(--variant-color);
      animation: none;
    }

    .badge-status-parity {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.15);
      color: #f59e0b;
      font-weight: 700;
    }
    .badge-status-parity .status-dot {
      background-color: #f59e0b;
      box-shadow: 0 0 10px #f59e0b;
      animation: none;
    }

    .badge-status-reject {
      border-color: rgba(239, 68, 68, 0.5);
      background: rgba(239, 68, 68, 0.15);
      color: var(--danger-color);
      font-weight: 700;
    }
    .badge-status-reject .status-dot {
      background-color: var(--danger-color);
      box-shadow: 0 0 10px var(--danger-color);
      animation: none;
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

    /* Recommendation Banner (Permanent slot, zero layout shift) */
    .banner {
      border-radius: 1rem;
      padding: 1.25rem 1.75rem;
      margin-bottom: 1.5rem;
      display: flex;
      min-height: 82px;
      box-sizing: border-box;
      align-items: center;
      justify-content: space-between;
      border: 1px solid rgba(255, 255, 255, 0.08);
      backdrop-filter: blur(12px);
      transition: background 0.3s ease, border-color 0.3s ease, box-shadow 0.3s ease, color 0.3s ease;
    }

    .banner.idle {
      background: rgba(255, 255, 255, 0.03);
      border-color: rgba(255, 255, 255, 0.08);
      color: var(--text-muted);
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

    /* Stage Rail Styles (Requirement 1) */
    .stage-rail-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.5rem;
      backdrop-filter: blur(14px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }

    .stage-rail-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.85rem;
    }

    .pipeline-title {
      font-size: 1.05rem;
      font-weight: 800;
      letter-spacing: -0.01em;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .pipeline-subtitle {
      font-size: 0.8rem;
      font-weight: 600;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
    }

    .stage-rail {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 0.5rem;
      background: rgba(0, 0, 0, 0.45);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 0.75rem;
      padding: 0.4rem;
    }

    @media (max-width: 900px) {
      .stage-rail { grid-template-columns: repeat(2, 1fr); }
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

    .stage-segment {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 0.75rem 0.9rem;
      border-radius: 0.55rem;
      cursor: pointer;
      transition: all 0.2s ease;
      border: 1px solid transparent;
      user-select: none;
      background: rgba(255, 255, 255, 0.02);
    }

    .stage-segment:hover {
      background: rgba(255, 255, 255, 0.05);
      border-color: rgba(255, 255, 255, 0.12);
    }

    .stage-segment.active {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.25);
      box-shadow: 0 2px 10px rgba(0, 0, 0, 0.4);
    }

    .stage-segment.active.running {
      border-color: rgba(59, 130, 246, 0.65);
      box-shadow: 0 0 14px rgba(59, 130, 246, 0.3);
      background: rgba(59, 130, 246, 0.12);
    }

    .stage-segment.active.passed, .stage-segment.active.done {
      border-color: rgba(16, 185, 129, 0.65);
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.25);
      background: rgba(16, 185, 129, 0.12);
    }

    .stage-segment.active.parity {
      border-color: rgba(245, 158, 11, 0.65);
      box-shadow: 0 0 14px rgba(245, 158, 11, 0.25);
      background: rgba(245, 158, 11, 0.12);
    }

    .stage-segment.active.failed {
      border-color: rgba(239, 68, 68, 0.65);
      box-shadow: 0 0 14px rgba(239, 68, 68, 0.25);
      background: rgba(239, 68, 68, 0.12);
    }

    .stage-seg-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.4rem;
      margin-bottom: 0.4rem;
    }

    .stage-seg-index {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.08);
      font-size: 0.68rem;
      font-weight: 700;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      flex-shrink: 0;
    }

    .stage-seg-name {
      font-size: 0.84rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1;
      margin: 0 0.25rem;
    }

    .stage-seg-status {
      font-size: 0.64rem;
      font-weight: 800;
      padding: 0.15rem 0.4rem;
      border-radius: 9999px;
      text-transform: uppercase;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: 0.02em;
      flex-shrink: 0;
    }

    .stage-seg-footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72rem;
      color: var(--text-muted);
    }

    .stage-detail-panel {
      margin-top: 1.25rem;
      padding-top: 1.25rem;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }

    /* Unit Detail Line (Requirement 2) */
    .unit-detail-line {
      display: flex;
      align-items: center;
      gap: 0.85rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.95rem;
      background: rgba(0, 0, 0, 0.3);
      padding: 0.85rem 1.25rem;
      border-radius: 0.65rem;
      border: 1px solid rgba(255, 255, 255, 0.06);
      flex-wrap: wrap;
    }

    /* Live Matrix Table (Requirement 3) */
    .live-matrix-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 0.5rem;
    }

    .live-matrix-table th {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text-muted);
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.75rem 0.85rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      text-align: center;
    }

    .live-matrix-table th:first-child {
      text-align: left;
    }

    .live-matrix-table td {
      padding: 0.85rem 0.85rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.88rem;
      text-align: center;
    }

    .live-matrix-table td:first-child {
      text-align: left;
    }

    .live-matrix-table tr.active-running {
      background: rgba(59, 130, 246, 0.08);
      box-shadow: inset 3px 0 0 #3b82f6;
    }

    .live-matrix-table tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }

    /* Depth Summary Bar & Toggle (Requirement 4) */
    .depth-summary-bar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(0, 0, 0, 0.3);
      padding: 0.85rem 1.25rem;
      border-radius: 0.65rem;
      border: 1px solid rgba(255, 255, 255, 0.06);
      flex-wrap: wrap;
      gap: 0.75rem;
    }

    .depth-summary-line {
      display: flex;
      align-items: center;
      gap: 0.85rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.92rem;
      flex-wrap: wrap;
    }

    .sep-dot {
      color: var(--text-muted);
      opacity: 0.6;
    }

    .btn-toggle-expand {
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: var(--text-main);
      padding: 0.4rem 0.85rem;
      border-radius: 0.45rem;
      font-size: 0.78rem;
      font-weight: 700;
      cursor: pointer;
      font-family: 'Inter', sans-serif;
      transition: all 0.2s ease;
    }

    .btn-toggle-expand:hover {
      background: rgba(255, 255, 255, 0.12);
      border-color: rgba(255, 255, 255, 0.3);
    }

    /* Last Completed Run Card (Requirement 6) */
    .last-run-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 1rem;
      padding: 1.35rem 1.6rem;
      margin-bottom: 1.5rem;
      backdrop-filter: blur(14px);
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
    }

    .last-run-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      padding-bottom: 0.85rem;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      flex-wrap: wrap;
      gap: 0.75rem;
    }

    .last-run-title {
      font-size: 1.1rem;
      font-weight: 800;
      color: var(--text-main);
      margin-top: 0.15rem;
    }

    .last-run-metrics-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 1rem;
    }

    @media (max-width: 900px) {
      .last-run-metrics-grid { grid-template-columns: repeat(2, 1fr); }
    }

    .last-run-metric {
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 0.75rem;
      padding: 0.85rem 1.1rem;
    }

    .metric-label {
      font-size: 0.72rem;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      text-transform: uppercase;
      margin-bottom: 0.3rem;
    }

    .metric-value {
      font-size: 1.45rem;
      font-weight: 800;
      font-family: 'JetBrains Mono', monospace;
      margin-bottom: 0.2rem;
    }

    .metric-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    .last-run-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 9999px;
      padding: 0.35rem 0.85rem;
      font-size: 0.78rem;
      font-family: 'JetBrains Mono', monospace;
    }

    .chip-mode {
      font-weight: 700;
      color: var(--text-main);
    }

    .chip-val {
      color: #60a5fa;
      font-weight: 700;
    }

    .chip-z {
      color: var(--text-muted);
    }

    .chip-badge {
      font-size: 0.65rem;
      font-weight: 800;
      padding: 0.1rem 0.4rem;
      border-radius: 9999px;
      text-transform: uppercase;
    }

    .chip-badge.parity { background: rgba(245, 158, 11, 0.25); color: #f59e0b; }
    .chip-badge.passed, .chip-badge.keep { background: rgba(16, 185, 129, 0.25); color: #34d399; }
    .chip-badge.failed, .chip-badge.revert { background: rgba(239, 68, 68, 0.25); color: #f87171; }

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
    .matrix-status-passed, .matrix-status-done { background: rgba(16, 185, 129, 0.25); color: #34d399; }
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
  </style>
</head>
<body>

<div class="container">
  <!-- Critical Unit Test Failure Banner (Top of page, Requirement 2) -->
  <div id="unitTestFailBanner" class="banner revert" style="display: none; margin-bottom: 1.5rem; border: 2px solid var(--danger-color); background: rgba(239, 68, 68, 0.15);">
    <div>
      <div class="banner-title" style="color: var(--danger-color); font-size: 1.1rem; font-weight: 800;">🚨 CRITICAL PRE-FLIGHT TEST FAILURE</div>
      <div class="banner-desc" id="unitTestFailBannerDesc">Safety test suite failed. All subsequent benchmark stages have been aborted to prevent deploying a faulty strategy.</div>
    </div>
  </div>

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
        <span id="statusText">IDLE</span>
      </div>
    </div>
  </header>

  <!-- Horizontal Stage Rail (Requirement 1) -->
  <div class="stage-rail-card" id="pipelineCard">
    <div class="stage-rail-header">
      <span class="pipeline-title">⚡ Validation Pipeline</span>
    </div>
    <div class="stage-rail" id="stageRail">
      <div class="stage-segment pending" id="railSegment-unit">
        <div class="stage-seg-header">
          <span class="stage-seg-index">1</span>
          <span class="stage-seg-name">Unit Tests</span>
          <span class="stage-seg-status matrix-status-pending" id="railStatus-unit">PENDING</span>
        </div>
        <div class="stage-seg-footer">
          <span>Safety Suite</span>
          <span class="stage-seg-duration" id="railDuration-unit">--</span>
        </div>
      </div>

      <div class="stage-segment pending" id="railSegment-depth">
        <div class="stage-seg-header">
          <span class="stage-seg-index">2</span>
          <span class="stage-seg-name">Depth Benchmark</span>
          <span class="stage-seg-status matrix-status-pending" id="railStatus-depth">PENDING</span>
        </div>
        <div class="stage-seg-footer">
          <span>Fixed Search</span>
          <span class="stage-seg-duration" id="railDuration-depth">--</span>
        </div>
      </div>

      <div class="stage-segment active pending" id="railSegment-self">
        <div class="stage-seg-header">
          <span class="stage-seg-index">3</span>
          <span class="stage-seg-name">Self-Play Matrix</span>
          <span class="stage-seg-status matrix-status-pending" id="railStatus-self">PENDING</span>
        </div>
        <div class="stage-seg-footer">
          <span>4 Modes Matrix</span>
          <span class="stage-seg-duration" id="railDuration-self">--</span>
        </div>
      </div>

      <div class="stage-segment pending" id="railSegment-pool">
        <div class="stage-seg-header">
          <span class="stage-seg-index">4</span>
          <span class="stage-seg-name">Zoo Pool Gate</span>
          <span class="stage-seg-status matrix-status-pending" id="railStatus-pool">PENDING</span>
        </div>
        <div class="stage-seg-footer">
          <span>Opponent Pool</span>
          <span class="stage-seg-duration" id="railDuration-pool">--</span>
        </div>
      </div>
    </div>

    <!-- Hidden compatibility elements for existing unit test assertions -->
    <div style="display: none;" id="pipelineGrid">
      <div id="stageCol-unit"><span id="stageStatus-unit"></span><span id="stageScore-unit"></span><span id="stageMeta-unit"></span></div>
      <div id="stageCol-depth"><span id="stageStatus-depth"></span><span id="stageScore-depth"></span><span id="stageMeta-depth"></span></div>
      <div id="stageCol-self"><span id="stageStatus-self"></span><span id="stageScore-self"></span><span id="stageMeta-self"></span></div>
      <div id="stageCol-pool"><span id="stageStatus-pool"></span><span id="stageScore-pool"></span><span id="stageMeta-pool"></span></div>
      <span id="headerEtaBadge"><span id="headerEtaText"></span></span>
      <span id="headerUnitPill"><span id="headerUnitDot"></span><span id="headerUnitText"></span></span>
      <span id="pipelineSubtitle"></span>
    </div>

    <!-- Stage Detail Panel (Expanded below rail, Requirement 1) -->
    <div class="stage-detail-panel" id="stageDetailPanel">
      <!-- 1. Unit Tests Detail (Requirement 2: single line) -->
      <div class="stage-panel-content" id="panel-unit" style="display: none;">
        <div class="unit-detail-line" id="unitDetailLine">
          <span id="unitDetailIcon" style="font-size: 1.1rem; color: var(--variant-color);">✓</span>
          <span id="unitDetailScore" style="font-weight: 700; color: var(--text-main);">40 / 40 Tactical Tests Passed</span>
          <span class="sep-dot">•</span>
          <span id="unitDetailDuration" style="font-family: 'JetBrains Mono', monospace; color: var(--text-muted);">Duration: 8.3s</span>
          <span class="sep-dot">•</span>
          <span id="unitDetailMeta" style="color: var(--text-muted); font-size: 0.85rem;">Pre-flight Tactical Safety Suite Clean</span>
        </div>
      </div>

      <!-- 2. Depth Benchmark Detail (Requirement 4: summary line + toggle) -->
      <div class="stage-panel-content" id="panel-depth" style="display: none;">
        <div class="depth-summary-container" id="depthCard">
          <div class="depth-summary-bar">
            <div class="depth-summary-line">
              <span>📐 Fixed Positions: <strong id="depthSummaryCount">12</strong></span>
              <span class="sep-dot">•</span>
              <span>Median Δ Depth: <strong id="depthSummaryDelta" style="color: var(--variant-color);">+0.0</strong></span>
              <span class="sep-dot">•</span>
              <span>NPS Ratio: <strong id="depthSummaryNps" style="color: #60a5fa;">1.00x</strong></span>
              <span class="sep-dot">•</span>
              <span>Move Agreement: <strong id="depthSummaryAgree">100.0%</strong></span>
              <span class="sep-dot">•</span>
              <span id="depthStatusBadge" class="matrix-col-status matrix-status-pending">AWAITING FIXTURES</span>
            </div>
            <span class="btn-toggle-expand" id="depthToggleBtn">
              <span id="depthToggleText">Show Position Breakdown ▼</span>
            </span>
          </div>

          <!-- Compatibility values for unit tests -->
          <div style="display: none;">
            <span id="depthVarValue">0.0</span>
            <span id="depthBaseValue">0.0</span>
            <span id="depthNpsRatioValue">1.00x</span>
            <span id="depthAgreeValue">100.0%</span>
            <span id="depthVarMeta"></span>
            <span id="depthNpsMeta"></span>
          </div>

          <div class="depth-fixtures-collapsible" id="depthTableContainer" style="display: none;">
            <div class="history-table-wrapper" style="margin-top: 1rem;">
              <table>
                <thead>
                  <tr>
                    <th style="text-align: left; min-width: 90px;">Mode</th>
                    <th style="min-width: 60px;">Turn</th>
                    <th style="min-width: 90px;">Base Depth</th>
                    <th style="min-width: 90px;">Var Depth</th>
                    <th style="min-width: 80px;">Δ Depth</th>
                    <th style="min-width: 90px;">Base NPS</th>
                    <th style="min-width: 90px;">Var NPS</th>
                    <th style="min-width: 80px;">NPS Ratio</th>
                    <th style="min-width: 80px;">Agree %</th>
                    <th style="min-width: 90px;">Status</th>
                  </tr>
                </thead>
                <tbody id="depthTableBody">
                  <tr>
                    <td colspan="10" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">Waiting for depth benchmark fixtures to evaluate...</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 3. Self-Play Matrix Detail (Requirement 3: centerpiece table) -->
      <div class="stage-panel-content" id="panel-self" style="display: block;">
        <div class="matrix-detail-wrap" id="matrixCard">
          <div class="table-title" style="margin-bottom: 0.75rem; display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 1.05rem; font-weight: 800; display: flex; align-items: center; gap: 0.5rem;">🏁 Self-Play Multi-Mode Matrix</span>
            <span style="font-size: 0.82rem; font-weight: 600; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;" id="matrixSubtitle">0 / 4 Modes Complete</span>
          </div>
          <!-- Live table for matrix modes (Requirement 3) -->
          <div class="history-table-wrapper">
            <table class="live-matrix-table" id="matrixTable">
              <thead>
                <tr>
                  <th style="text-align: left; min-width: 140px;">Mode</th>
                  <th style="min-width: 130px;">Games (Done / Total)</th>
                  <th style="min-width: 120px;">Current Win Share</th>
                  <th style="min-width: 90px;">Z-Score</th>
                  <th style="min-width: 110px;">Per-Mode ETA</th>
                  <th style="min-width: 95px;">Status</th>
                </tr>
              </thead>
              <tbody id="matrixTableBody">
                <!-- Dynamically populated rows for Constrictor, Royale, Duel, Standard -->
              </tbody>
            </table>
          </div>
          <div id="matrixGrid" style="display: none;"></div>
        </div>
      </div>

      <!-- 4. Zoo Pool Gate Detail (Requirement 5: table per mode) -->
      <div class="stage-panel-content" id="panel-pool" style="display: none;">
        <div class="pool-detail-wrap" id="poolOpponentsCard">
          <div class="table-title" style="margin-bottom: 0.75rem; display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 1.05rem; font-weight: 800; display: flex; align-items: center; gap: 0.5rem;">🐍 Zoo Pool Gate Multi-Mode Benchmark</span>
            <span style="font-size: 0.82rem; font-weight: 600; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;" id="poolStatusSubtitle">Awaiting Pool Evaluation</span>
          </div>

          <div class="history-table-wrapper">
            <table class="live-matrix-table" id="poolModesTable">
              <thead>
                <tr>
                  <th style="text-align: left; min-width: 140px;">Mode</th>
                  <th style="min-width: 130px;">Candidate Win Share</th>
                  <th style="min-width: 130px;">Baseline Win Share</th>
                  <th style="min-width: 100px;">Difference (Δ)</th>
                  <th style="min-width: 90px;">Z-Score</th>
                  <th style="min-width: 95px;">Status</th>
                </tr>
              </thead>
              <tbody id="poolModesTableBody">
                <!-- Populated per mode -->
              </tbody>
            </table>
          </div>

          <!-- Opponent Breakdown List (collapsible) -->
          <div style="margin-top: 1.25rem;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
              <span style="font-size: 0.88rem; font-weight: 700; color: var(--text-muted);">Opponent Head-to-Head Breakdown</span>
              <span style="font-size: 0.8rem; color: var(--text-muted);" id="poolOpponentsCount">0 Opponents</span>
            </div>
            <div class="history-table-wrapper">
              <table>
                <thead>
                  <tr>
                    <th style="text-align: left; min-width: 140px;">Opponent</th>
                    <th style="min-width: 70px;">Games</th>
                    <th style="min-width: 110px;">Candidate Win %</th>
                    <th style="min-width: 110px;">Baseline Win %</th>
                    <th style="min-width: 90px;">Delta (Δ)</th>
                  </tr>
                </thead>
                <tbody id="poolOpponentsTableBody">
                  <tr>
                    <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">Waiting for pool opponent results...</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Active Run Hero & Progress Section (Visible only during active/finished runs, hidden when idle) -->
  <div id="activeRunHero" style="display: none;">
    <!-- Hidden compatibility elements -->
    <div id="decisionBanner" style="display: none;"><span id="bannerTitle"></span><span id="bannerDesc"></span></div>

    <!-- Tug-of-War Hero Card -->
    <div class="hero-card" id="heroCard">
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
    <div class="stats-grid" id="statsGrid">
      <div class="stat-card">
        <div class="stat-header">
          <span>Run Progress</span>
          <span class="stat-icon">⚡</span>
        </div>
        <div style="display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 0.35rem;">
          <div>
            <div class="stat-value" id="runsValue">0 / 0 Games</div>
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
  </div>

  <!-- Idle State: Last Completed Run's Summary (Requirement 6, shown when no run active) -->
  <div class="last-run-card" id="idleCompletedHero" style="display: block;">
    <div class="last-run-header">
      <div style="display: flex; align-items: center; gap: 0.85rem;">
        <span style="font-size: 1.4rem;">🏁</span>
        <div>
          <div style="font-size: 0.72rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em;">Last Completed Benchmark</div>
          <div class="last-run-title" id="lastRunTitle">No previous simulation runs recorded.</div>
        </div>
      </div>
      <div style="display: flex; align-items: center; gap: 0.75rem;">
        <span class="winner-pill parity" id="lastRunDecisionBadge">🟡 PARITY</span>
        <span style="font-size: 0.8rem; font-family: 'JetBrains Mono', monospace; color: var(--text-muted);" id="lastRunTime">-</span>
      </div>
    </div>

    <div class="last-run-body" id="lastRunBody">
      <div class="last-run-metrics-grid">
        <div class="last-run-metric">
          <div class="metric-label">TOTAL GAMES</div>
          <div class="metric-value" id="lastRunGames">0</div>
          <div class="metric-sub" id="lastRunModesCount">0 Modes</div>
        </div>
        <div class="last-run-metric">
          <div class="metric-label">VARIANT WIN SHARE</div>
          <div class="metric-value" style="color: var(--variant-color);" id="lastRunVarShare">--</div>
          <div class="metric-sub" id="lastRunVarWins">0 Wins</div>
        </div>
        <div class="last-run-metric">
          <div class="metric-label">BASELINE WIN SHARE</div>
          <div class="metric-value" style="color: var(--baseline-color);" id="lastRunBaseShare">--</div>
          <div class="metric-sub" id="lastRunBaseWins">0 Wins</div>
        </div>
        <div class="last-run-metric">
          <div class="metric-label">BENCHMARK OUTCOME</div>
          <div class="metric-value" style="color: #f59e0b;" id="lastRunOutcome">--</div>
          <div class="metric-sub" id="lastRunStopReason">-</div>
        </div>
      </div>

      <!-- Per-mode chip indicators from last completed run -->
      <div class="last-run-chips" id="lastRunChips" style="margin-top: 1rem; display: flex; gap: 0.65rem; flex-wrap: wrap;">
      </div>
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
  let isUpdating = false;

  let activeStage = 'self';
  let userSelectedStage = false;
  let isDepthExpanded = false;

  function selectStage(stageId) {
    userSelectedStage = true;
    activeStage = stageId;
    ['unit', 'depth', 'self', 'pool'].forEach(id => {
      const seg = document.getElementById(`railSegment-${id}`);
      const panel = document.getElementById(`panel-${id}`);
      if (seg) {
        if (id === stageId) seg.classList.add('active');
        else seg.classList.remove('active');
      }
      if (panel) {
        panel.style.display = (id === stageId) ? 'block' : 'none';
      }
    });
  }

  function autoSelectStage(stageId) {
    if (userSelectedStage) return;
    activeStage = stageId;
    ['unit', 'depth', 'self', 'pool'].forEach(id => {
      const seg = document.getElementById(`railSegment-${id}`);
      const panel = document.getElementById(`panel-${id}`);
      if (seg) {
        if (id === stageId) seg.classList.add('active');
        else seg.classList.remove('active');
      }
      if (panel) {
        panel.style.display = (id === stageId) ? 'block' : 'none';
      }
    });
  }

  function toggleDepthTable() {
    isDepthExpanded = !isDepthExpanded;
    const tableContainer = document.getElementById('depthTableContainer');
    const toggleText = document.getElementById('depthToggleText');
    if (tableContainer) {
      tableContainer.style.display = isDepthExpanded ? 'block' : 'none';
    }
    if (toggleText) {
      toggleText.innerText = isDepthExpanded ? 'Hide Position Breakdown ▲' : 'Show Position Breakdown ▼';
    }
  }

  function updateDepthUI(data) {
    const depth = data.depth_data || {};
    const varD = (depth.median_var_depth != null) ? depth.median_var_depth : 0.0;
    const baseD = (depth.median_base_depth != null) ? depth.median_base_depth : 0.0;
    const deltaD = (depth.delta_depth != null) ? depth.delta_depth : (varD - baseD);
    const npsR = (depth.nps_ratio != null) ? depth.nps_ratio : 1.0;
    const agree = (depth.agreement_pct != null) ? depth.agreement_pct : 100.0;

    // Single summary line (Requirement 4)
    setText('depthSummaryCount', depth.total_positions || depth.completed_positions || 12);
    const deltaEl = document.getElementById('depthSummaryDelta');
    if (deltaEl) {
      deltaEl.innerText = `${deltaD >= 0 ? '+' : ''}${deltaD.toFixed(1)} (V ${varD.toFixed(1)} vs B ${baseD.toFixed(1)})`;
      deltaEl.style.color = deltaD > 0 ? 'var(--variant-color)' : (deltaD < 0 ? 'var(--danger-color)' : 'var(--text-main)');
    }
    const npsEl = document.getElementById('depthSummaryNps');
    if (npsEl) {
      npsEl.innerText = `${npsR.toFixed(2)}x`;
      npsEl.style.color = npsR >= 1.0 ? 'var(--variant-color)' : 'var(--danger-color)';
    }
    setText('depthSummaryAgree', `${agree.toFixed(1)}%`);

    // Compatibility test elements
    setText('depthVarValue', varD.toFixed(1));
    setText('depthBaseValue', baseD.toFixed(1));
    setText('depthNpsRatioValue', `${npsR.toFixed(2)}x`);
    setText('depthAgreeValue', `${agree.toFixed(1)}%`);

    const statusBadge = document.getElementById('depthStatusBadge');
    if (statusBadge) {
      if (data.status === 'FINISHED' || (depth.completed_positions && depth.completed_positions >= 12)) {
        const passed = depth.passed !== false && deltaD >= 0;
        statusBadge.className = `matrix-col-status ${passed ? 'matrix-status-passed' : 'matrix-status-failed'}`;
        statusBadge.innerText = passed ? 'PASSED (NO REGRESSION)' : 'DEPTH REGRESSION';
      } else if (data.run_type === 'depth' && data.status === 'RUNNING') {
        statusBadge.className = 'matrix-col-status matrix-status-running';
        statusBadge.innerText = `EVALUATING (${depth.completed_positions || 0}/${depth.total_positions || 12})`;
      } else {
        statusBadge.className = 'matrix-col-status matrix-status-pending';
        statusBadge.innerText = 'AWAITING FIXTURES';
      }
    }

    // Per-fixture breakdown table
    const tbody = document.getElementById('depthTableBody');
    if (tbody) {
      const fixtures = depth.fixtures || [];
      if (fixtures.length === 0) {
        setHTML(tbody, '<tr><td colspan="10" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">Waiting for depth benchmark fixtures to evaluate...</td></tr>');
      } else {
        const rowsHtml = fixtures.map(f => {
          const delta = (f.delta_depth != null) ? f.delta_depth : (f.var_med_depth - f.base_med_depth);
          const isPass = f.passed !== false && delta >= 0;
          const statusClass = isPass ? 'passed' : 'failed';
          const statusText = isPass ? 'PASS' : 'REGRESSION';
          const deltaColor = delta > 0 ? 'var(--variant-color)' : (delta < 0 ? 'var(--danger-color)' : 'var(--text-muted)');
          const npsColor = f.nps_ratio >= 1.0 ? 'var(--variant-color)' : 'var(--danger-color)';

          return `
            <tr>
              <td style="text-align: left; font-weight: 700;">${escapeHtml(f.mode.toUpperCase())}</td>
              <td>Turn ${f.turn}</td>
              <td>${f.base_med_depth.toFixed(1)}</td>
              <td style="font-weight: 700; color: var(--variant-color);">${f.var_med_depth.toFixed(1)}</td>
              <td style="color: ${deltaColor}; font-weight: 700;">${delta > 0 ? '+' : ''}${delta.toFixed(1)}</td>
              <td>${Math.round(f.base_med_nps || 0).toLocaleString()}</td>
              <td>${Math.round(f.var_med_nps || 0).toLocaleString()}</td>
              <td style="color: ${npsColor}; font-weight: 700;">${f.nps_ratio.toFixed(2)}x</td>
              <td>${f.agreement_pct.toFixed(1)}%</td>
              <td><span class="matrix-col-status matrix-status-${statusClass}">${statusText}</span></td>
            </tr>
          `;
        }).join('');
        setHTML(tbody, rowsHtml);
      }
    }
  }

  function updatePoolUI(data) {
    const pool = data.pool_data || {};
    const poolModesBody = document.getElementById('poolModesTableBody');
    const poolSubtitle = document.getElementById('poolStatusSubtitle');

    const baseModes = [
      { mode: 'constrictor', name: 'Constrictor 4P' },
      { mode: 'royale', name: 'Royale 4P' },
      { mode: 'duel', name: 'Duel 1v1' },
      { mode: 'standard', name: 'Standard 4P' },
    ];

    if (poolModesBody) {
      const modeDataMap = pool.mode_results || pool.modes || {};
      const overallDelta = pool.delta != null ? pool.delta : ((pool.cand_share || 0) - (pool.base_share || 0));
      const overallZ = pool.z_score != null ? pool.z_score : 0.0;
      const isPoolRunning = (data.run_type === 'pool' && data.status === 'RUNNING');

      const rowsHtml = baseModes.map(bm => {
        const md = modeDataMap[bm.mode] || {};
        const candShare = md.cand_share != null ? md.cand_share : (isPoolRunning && data.game_mode === bm.mode ? (pool.cand_share || 0) : null);
        const baseShare = md.base_share != null ? md.base_share : (isPoolRunning && data.game_mode === bm.mode ? (pool.base_share || 0) : null);
        const delta = md.delta != null ? md.delta : (candShare != null && baseShare != null ? (candShare - baseShare) : null);
        const z = md.z_score != null ? md.z_score : (isPoolRunning && data.game_mode === bm.mode ? overallZ : null);

        let statusClass = 'pending';
        let statusText = 'PENDING';
        if (isPoolRunning && data.game_mode === bm.mode) {
          statusClass = 'running';
          statusText = 'RUNNING';
        } else if (md.decision) {
          statusText = md.decision;
          statusClass = md.decision.toLowerCase();
        } else if (candShare != null) {
          statusClass = delta >= 0 ? 'passed' : 'failed';
          statusText = delta >= 0 ? 'PASSED' : 'FAILED';
        }

        const deltaColor = delta > 0 ? 'var(--variant-color)' : (delta < 0 ? 'var(--danger-color)' : 'var(--text-main)');

        return `
          <tr>
            <td style="font-weight: 700; color: var(--text-main);">${bm.name}</td>
            <td style="font-weight: 700; color: var(--variant-color);">${candShare != null ? candShare.toFixed(1) + '%' : '--'}</td>
            <td style="font-weight: 700; color: var(--baseline-color);">${baseShare != null ? baseShare.toFixed(1) + '%' : '--'}</td>
            <td style="font-weight: 700; color: ${delta != null ? deltaColor : 'var(--text-muted)'};">${delta != null ? (delta > 0 ? '+' : '') + delta.toFixed(1) + '%' : '--'}</td>
            <td style="font-weight: 600; color: var(--text-main);">${z != null ? (z > 0 ? '+' : '') + z.toFixed(2) : '--'}</td>
            <td><span class="matrix-col-status matrix-status-${statusClass}">${statusText}</span></td>
          </tr>
        `;
      }).join('');

      setHTML(poolModesBody, rowsHtml);
      if (poolSubtitle) {
        if (pool.total_games && pool.total_games > 0) {
          poolSubtitle.innerText = `Evaluated ${pool.total_games} Games • z = ${overallZ >= 0 ? '+' : ''}${overallZ.toFixed(2)}`;
        } else if (isPoolRunning) {
          poolSubtitle.innerText = `Evaluating Candidate vs Opponent Pool...`;
        } else {
          poolSubtitle.innerText = 'Awaiting Pool Benchmark';
        }
      }
    }

    // Update Opponents Table
    const oppTbody = document.getElementById('poolOpponentsTableBody');
    const oppCount = document.getElementById('poolOpponentsCount');
    const opponents = pool.opponents || [];
    if (oppCount) oppCount.innerText = `${opponents.length} Opponent${opponents.length === 1 ? '' : 's'}`;
    if (oppTbody) {
      if (opponents.length === 0) {
        setHTML(oppTbody, '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">Waiting for pool opponent results...</td></tr>');
      } else {
        const oppRows = opponents.map(o => {
          const d = (o.delta != null) ? o.delta : (o.cand_win_pct - o.base_win_pct);
          const dColor = d > 0 ? 'var(--variant-color)' : (d < 0 ? 'var(--danger-color)' : 'var(--text-muted)');
          return `
            <tr>
              <td style="text-align: left; font-weight: 700; color: var(--text-main);">${escapeHtml(o.opponent)}</td>
              <td>${o.games}</td>
              <td style="color: var(--variant-color); font-weight: 700;">${o.cand_win_pct.toFixed(1)}%</td>
              <td style="color: var(--baseline-color); font-weight: 700;">${o.base_win_pct.toFixed(1)}%</td>
              <td style="color: ${dColor}; font-weight: 700;">${d > 0 ? '+' : ''}${d.toFixed(1)}%</td>
            </tr>
          `;
        }).join('');
        setHTML(oppTbody, oppRows);
      }
    }
  }

  function updateStageRail(data) {
    const runType = data.run_type || 'self';
    const isRunning = (data.status === 'RUNNING' && data.runner_alive !== false);
    const isFinished = (data.status === 'FINISHED');
    const isIdle = (data.status === 'IDLE' || data.status === 'WAITING' || !data.status);
    const rec = data.recommendation || 'PENDING';

    function setRailSeg(id, status, duration) {
      const seg = document.getElementById(`railSegment-${id}`);
      const st = document.getElementById(`railStatus-${id}`);
      const dur = document.getElementById(`railDuration-${id}`);
      const statusLower = status.toLowerCase();

      if (seg) {
        const isActive = (id === activeStage);
        seg.className = `stage-segment ${statusLower}${isActive ? ' active' : ''}`;
      }
      if (st) {
        st.className = `stage-seg-status matrix-status-${statusLower}`;
        st.innerText = status;
      }
      if (dur) {
        dur.innerText = duration || '--';
      }
    }

    // Stage 1: Tactical Unit Tests
    const unitData = data.unit_tests_data || {};
    let unitStatus = unitData.status || 'PENDING';
    let unitDur = unitData.duration_sec ? `${unitData.duration_sec.toFixed(1)}s` : '--';

    if (data.stop_type === 'TEST_FAILURE' || unitStatus === 'FAILED') {
      unitStatus = 'FAILED';
      unitDur = unitData.duration_sec ? `${unitData.duration_sec.toFixed(1)}s` : 'Failed';
      autoSelectStage('unit');
    } else if (unitStatus === 'PASSED' || (data.total_played > 0) || (isFinished && data.stop_type !== 'TEST_FAILURE')) {
      unitStatus = 'DONE';
      unitDur = unitData.duration_sec ? `${unitData.duration_sec.toFixed(1)}s` : (unitData.passed ? '< 10s' : 'Passed');
    } else if (unitStatus === 'RUNNING' || (isRunning && data.total_played === 0 && runType === 'self')) {
      unitStatus = 'RUNNING';
      unitDur = formatSeconds(data.elapsed_seconds || 0);
      autoSelectStage('unit');
    }
    setRailSeg('unit', unitStatus, unitDur);

    // Update Unit Tests Single-Line Detail (Requirement 2)
    const unitScoreEl = document.getElementById('unitDetailScore');
    const unitDurEl = document.getElementById('unitDetailDuration');
    const unitIconEl = document.getElementById('unitDetailIcon');
    const unitMetaEl = document.getElementById('unitDetailMeta');
    if (unitScoreEl) {
      if (unitStatus === 'FAILED') {
        unitScoreEl.innerText = `${unitData.passed || 0} / ${unitData.total || 40} Tests Passed (FAILURE)`;
        unitScoreEl.style.color = 'var(--danger-color)';
        if (unitIconEl) { unitIconEl.innerText = '❌'; unitIconEl.style.color = 'var(--danger-color)'; }
        if (unitMetaEl) unitMetaEl.innerText = unitData.details || 'Pre-flight Tactical Safety Suite Failed';
      } else if (unitStatus === 'DONE') {
        unitScoreEl.innerText = `${unitData.passed || 40} / ${unitData.total || unitData.passed || 40} Tactical Tests Passed`;
        unitScoreEl.style.color = 'var(--variant-color)';
        if (unitIconEl) { unitIconEl.innerText = '✓'; unitIconEl.style.color = 'var(--variant-color)'; }
        if (unitMetaEl) unitMetaEl.innerText = 'Pre-flight Tactical Safety Suite Clean';
      } else if (unitStatus === 'RUNNING') {
        unitScoreEl.innerText = 'Executing Pre-flight Tactical Safety Suite...';
        unitScoreEl.style.color = '#60a5fa';
        if (unitIconEl) { unitIconEl.innerText = '⏳'; unitIconEl.style.color = '#60a5fa'; }
        if (unitMetaEl) unitMetaEl.innerText = 'Running deterministic test scenarios';
      } else {
        unitScoreEl.innerText = '40 Tactical Safety Tests';
        unitScoreEl.style.color = 'var(--text-muted)';
        if (unitIconEl) { unitIconEl.innerText = '⚪'; unitIconEl.style.color = 'var(--text-muted)'; }
        if (unitMetaEl) unitMetaEl.innerText = 'Awaiting Pre-flight Execution';
      }
    }
    if (unitDurEl) unitDurEl.innerText = `Duration: ${unitDur}`;

    // Stage 2: Depth Benchmark
    const depth = data.depth_data || {};
    let depthStatus = 'PENDING';
    let depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : '--';

    if (runType === 'depth') {
      if (isRunning) {
        depthStatus = 'RUNNING';
        depthDur = formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('depth');
      } else if (isFinished) {
        const passed = depth.passed !== false && (depth.delta_depth == null || depth.delta_depth >= 0);
        depthStatus = passed ? 'DONE' : 'FAILED';
        depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('depth');
      }
    } else if (depth.completed_positions && depth.completed_positions > 0) {
      const passed = depth.passed !== false && (depth.delta_depth == null || depth.delta_depth >= 0);
      depthStatus = passed ? 'DONE' : 'FAILED';
      depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : '--';
    }
    setRailSeg('depth', depthStatus, depthDur);

    // Stage 3: Self-Play Matrix
    let selfStatus = 'PENDING';
    const matrixList = data.matrix_progress || [];
    const completedModes = matrixList.filter(m => ['PASSED', 'PARITY', 'FAILED', 'SKIPPED'].includes(m.status)).length;
    let selfDur = '--';
    const sumMatrixSec = matrixList.reduce((acc, m) => acc + (m.duration_sec || 0), 0);

    if (runType === 'self') {
      if (isRunning) {
        selfStatus = 'RUNNING';
        selfDur = formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('self');
      } else if (isFinished) {
        if (rec === 'KEEP') selfStatus = 'DONE';
        else if (rec === 'PARITY') selfStatus = 'PARITY';
        else if (rec === 'REVERT') selfStatus = 'FAILED';
        else selfStatus = 'DONE';
        selfDur = formatSeconds(sumMatrixSec || data.elapsed_seconds || 0);
        autoSelectStage('self');
      }
    } else if (completedModes > 0) {
      selfStatus = 'DONE';
      selfDur = formatSeconds(sumMatrixSec);
    }
    setRailSeg('self', selfStatus, selfDur);

    // Stage 4: Zoo Pool Gate
    const pool = data.pool_data || {};
    let poolStatus = 'PENDING';
    let poolDur = pool.duration_sec ? formatSeconds(pool.duration_sec) : '--';

    if (runType === 'pool') {
      if (isRunning) {
        poolStatus = 'RUNNING';
        poolDur = formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('pool');
      } else if (isFinished) {
        if (rec === 'KEEP') poolStatus = 'DONE';
        else if (rec === 'PARITY') poolStatus = 'PARITY';
        else poolStatus = 'FAILED';
        poolDur = pool.duration_sec ? formatSeconds(pool.duration_sec) : formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('pool');
      }
    } else if (pool.total_games && pool.total_games > 0) {
      poolStatus = (pool.delta || 0) >= 0 ? 'DONE' : 'FAILED';
      poolDur = pool.duration_sec ? formatSeconds(pool.duration_sec) : '--';
    }
    setRailSeg('pool', poolStatus, poolDur);

    // Header Unit Tests Pill (Removed: redundant with stage rail and failure banner)
    const headerPill = document.getElementById('headerUnitPill');
    if (headerPill) headerPill.style.display = 'none';

    // Red Banner on Failure (Requirement 2)
    const failBanner = document.getElementById('unitTestFailBanner');
    const failDesc = document.getElementById('unitTestFailBannerDesc');
    if (failBanner) {
      if (data.stop_type === 'TEST_FAILURE' || unitStatus === 'FAILED') {
        failBanner.style.display = 'flex';
        if (failDesc) failDesc.innerText = data.stop_reason || unitData.details || 'Pre-flight tactical safety suite failed. All subsequent stages aborted.';
      } else {
        failBanner.style.display = 'none';
      }
    }

    // Header Total Run ETA (Removed: redundant with run progress)
    const headerEtaBadge = document.getElementById('headerEtaBadge');
    if (headerEtaBadge) headerEtaBadge.style.display = 'none';

    // Pipeline Subtitle (Removed: redundant with highlighted stage segment)
    const subEl = document.getElementById('pipelineSubtitle');
    if (subEl) subEl.style.display = 'none';
  }

  function updatePipelineUI(data) {
    updateStageRail(data);
  }

  function updateMatrixUI(data) {
    const tableBody = document.getElementById('matrixTableBody');
    const matrixSubtitle = document.getElementById('matrixSubtitle');
    if (!tableBody) return;

    const baseModes = [
      { mode: 'constrictor', name: 'Constrictor 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
      { mode: 'royale', name: 'Royale 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
      { mode: 'duel', name: 'Duel 1v1', target_share: 50.0, max_games: 200, num_snakes: 2 },
      { mode: 'standard', name: 'Standard 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
    ];

    const matrixList = (data.matrix_progress && data.matrix_progress.length > 0) ? data.matrix_progress : [];
    const modeMap = {};
    matrixList.forEach(m => { if (m.mode) modeMap[m.mode.toLowerCase()] = m; });

    const isSelfRunning = (data.run_type === 'self' && data.status === 'RUNNING' && data.runner_alive !== false);
    const activeModeClean = (data.game_mode || '').toLowerCase();

    const played = data.total_played || 0;
    const is4p = data.num_snakes === 4;
    const drawShare = is4p ? 0.25 : 0.5;
    const varW = (data.variant && data.variant.wins != null) ? data.variant.wins : 0;
    const drawsCount = data.draws ? data.draws.count : ((data.baseline && data.baseline.draws != null) ? data.baseline.draws : 0);
    const varPts = (data.variant && typeof data.variant.points === 'number') ? data.variant.points : (varW + drawShare * drawsCount);
    const varWS = (data.variant && typeof data.variant.win_share === 'number') ? data.variant.win_share : (played > 0 ? (varPts / played * 100) : (is4p ? 25.0 : 50.0));

    // Per-mode ETA lookup from data.eta_breakdown
    const etaMap = {};
    (data.eta_breakdown || []).forEach(b => {
      if (b.mode) etaMap[b.mode.toLowerCase()] = b;
    });

    let completedCount = 0;

    const rowsHtml = baseModes.map((bm) => {
      const modeKey = bm.mode;
      const m = modeMap[modeKey] || {};
      const isCurrentActive = isSelfRunning && (activeModeClean === modeKey);

      let effectiveStatus = 'PENDING';
      if (m.status && ['PASSED', 'PARITY', 'FAILED', 'SKIPPED'].includes(m.status)) {
        effectiveStatus = m.status;
        completedCount++;
      } else if (isCurrentActive) {
        effectiveStatus = 'RUNNING';
      } else if (m.status === 'RUNNING') {
        effectiveStatus = 'RUNNING';
      }

      const statusClass = effectiveStatus.toLowerCase();
      const statusBadge = `<span class="matrix-col-status matrix-status-${statusClass}">${effectiveStatus}</span>`;

      const gamesVal = isCurrentActive ? played : (m.games || 0);
      const maxGames = m.max_games || bm.max_games || 200;
      const targetVal = bm.target_share;

      let scoreVal = '--';
      let scoreColor = 'var(--text-main)';
      let zScoreText = '--';
      let etaText = '--';

      if (effectiveStatus === 'RUNNING') {
        scoreVal = `${varWS.toFixed(1)}%`;
        scoreColor = '#60a5fa';
        // Compute live z-score
        if (played >= 5) {
          const p = varWS / 100.0;
          const p0 = targetVal / 100.0;
          const se = Math.sqrt((p0 * (1.0 - p0)) / played);
          const z = se > 0 ? ((p - p0) / se) : 0.0;
          zScoreText = `${z >= 0 ? '+' : ''}${z.toFixed(2)}`;
        }
        const b = etaMap[modeKey];
        if (b && b.eta_seconds != null) {
          etaText = `~${formatSeconds(b.eta_seconds)}`;
        } else if (data.eta_seconds != null && data.eta_seconds > 0) {
          etaText = `~${formatSeconds(data.eta_seconds)}`;
        }
      } else if (['PASSED', 'PARITY', 'FAILED'].includes(effectiveStatus)) {
        const ws = (m.win_share != null ? m.win_share : varWS);
        scoreVal = `${ws.toFixed(1)}%`;
        if (effectiveStatus === 'PASSED') scoreColor = 'var(--variant-color)';
        else if (effectiveStatus === 'PARITY') scoreColor = '#f59e0b';
        else scoreColor = 'var(--danger-color)';

        if (m.z_score != null) {
          zScoreText = `${m.z_score >= 0 ? '+' : ''}${m.z_score.toFixed(2)}`;
        }
        if (m.duration_sec != null) {
          etaText = `Done (${formatSeconds(m.duration_sec)})`;
        } else {
          etaText = 'Done';
        }
      } else if (effectiveStatus === 'SKIPPED') {
        scoreVal = gamesVal > 0 ? `${(m.win_share || 0).toFixed(1)}%` : 'SKIPPED';
        scoreColor = '#fbbf24';
        etaText = 'Skipped';
      } else {
        const b = etaMap[modeKey];
        if (b && b.eta_seconds != null && b.eta_seconds > 0) {
          etaText = `~${formatSeconds(b.eta_seconds)} (Queued)`;
        } else {
          etaText = 'Queued';
        }
      }

      const rowClass = isCurrentActive ? 'active-running' : '';
      const pctDone = maxGames > 0 ? Math.min(100, (gamesVal / maxGames) * 100) : 0;

      return `
        <tr class="${rowClass}">
          <td style="font-weight: 700; color: var(--text-main);">
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span>${bm.name}</span>
              <span style="font-size: 0.72rem; color: var(--text-muted); font-weight: 500;">(Target: &gt;${targetVal.toFixed(1)}%)</span>
            </div>
          </td>
          <td>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span style="font-weight: 700;">${gamesVal} / ${maxGames}</span>
              <span style="font-size: 0.75rem; color: var(--text-muted);">(${pctDone.toFixed(0)}%)</span>
            </div>
          </td>
          <td style="font-weight: 700; color: ${scoreColor}; font-size: 0.95rem;">${scoreVal}</td>
          <td style="font-weight: 600; color: ${zScoreText !== '--' ? (parseFloat(zScoreText) >= 1.645 ? 'var(--variant-color)' : (parseFloat(zScoreText) <= -1.645 ? 'var(--danger-color)' : '#f59e0b')) : 'var(--text-muted)'};">${zScoreText}</td>
          <td style="color: var(--text-muted); font-size: 0.82rem;">${etaText}</td>
          <td>${statusBadge}</td>
        </tr>
      `;
    }).join('');

    setHTML(tableBody, rowsHtml);
    if (matrixSubtitle) matrixSubtitle.innerText = `${completedCount} / 4 Modes Complete`;
  }

  function updateHeroAndIdleState(data) {
    const isRunning = (data.status === 'RUNNING' && data.runner_alive !== false);
    const isFinished = (data.status === 'FINISHED');
    const played = data.total_played || 0;
    const isRunActive = isRunning || (isFinished && played > 0);

    const activeHero = document.getElementById('activeRunHero');
    const idleHero = document.getElementById('idleCompletedHero');

    if (isRunActive) {
      if (activeHero) activeHero.style.display = 'block';
      if (idleHero) idleHero.style.display = 'none';
      return;
    }

    // No run is active: Hide hero numbers completely, show last completed run summary!
    if (activeHero) activeHero.style.display = 'none';
    if (idleHero) idleHero.style.display = 'block';

    const historyList = data.run_history || [];
    if (historyList.length === 0) {
      setText('lastRunTitle', 'No previous simulation runs recorded.');
      setText('lastRunDecisionBadge', 'IDLE');
      setText('lastRunTime', '-');
      setText('lastRunGames', '0');
      setText('lastRunVarShare', '--');
      setText('lastRunBaseShare', '--');
      setText('lastRunOutcome', 'AWAITING RUN');
      setText('lastRunStopReason', 'Runner is currently idle.');
      setHTML('lastRunChips', '');
      return;
    }

    const lastRun = historyList[0];
    const rec = lastRun.recommendation || 'UNKNOWN';
    let pillClass = 'parity';
    let badgeText = rec;
    if (rec === 'KEEP') { pillClass = 'variant'; badgeText = '🎉 KEEP'; }
    else if (rec === 'PARITY') { pillClass = 'parity'; badgeText = '🟡 PARITY'; }
    else if (rec === 'REVERT') { pillClass = 'revert'; badgeText = '⚠️ REVERT'; }
    else if (rec === 'TEST_FAILURE') { pillClass = 'revert'; badgeText = '❌ FAILED'; }

    const badgeEl = document.getElementById('lastRunDecisionBadge');
    if (badgeEl) {
      badgeEl.className = `winner-pill ${pillClass}`;
      badgeEl.innerText = badgeText;
    }

    setText('lastRunTitle', lastRun.experiment_desc || lastRun.description || 'Unspecified Experiment');
    setText('lastRunTime', lastRun.timestamp || '-');
    setText('lastRunGames', lastRun.total_games || 0);

    const varWS = lastRun.variant ? (lastRun.variant.win_share != null ? lastRun.variant.win_share : lastRun.variant.rate) : null;
    const varW = lastRun.variant ? lastRun.variant.wins : 0;
    const baseWS = lastRun.baseline ? (lastRun.baseline.win_share != null ? lastRun.baseline.win_share : lastRun.baseline.rate) : null;
    const baseW = lastRun.baseline ? lastRun.baseline.wins : 0;

    setText('lastRunVarShare', varWS != null ? `${varWS.toFixed(1)}%` : '--');
    setText('lastRunVarWins', `${varW} Wins`);
    setText('lastRunBaseShare', baseWS != null ? `${baseWS.toFixed(1)}%` : '--');
    setText('lastRunBaseWins', `${baseW} Wins`);

    setText('lastRunOutcome', rec);
    const stopReasonEl = document.getElementById('lastRunStopReason');
    if (stopReasonEl) {
      stopReasonEl.innerText = lastRun.stop_reason || 'Benchmark completed';
      stopReasonEl.title = lastRun.stop_reason || '';
    }

    // Populate Mode Chips
    const chipsEl = document.getElementById('lastRunChips');
    if (chipsEl) {
      const matrixRes = lastRun.matrix_results || [];
      const modesCountEl = document.getElementById('lastRunModesCount');
      if (modesCountEl) modesCountEl.innerText = `${matrixRes.length || 4} Modes Tested`;

      if (matrixRes.length > 0) {
        const chipsHtml = matrixRes.map(m => {
          const dec = m.decision || 'DONE';
          const decClass = dec.toLowerCase();
          const zStr = (m.z_score != null) ? `z=${m.z_score >= 0 ? '+' : ''}${m.z_score.toFixed(2)}` : '';
          return `
            <div class="last-run-chip">
              <span class="chip-mode">${escapeHtml(m.name || m.mode)}</span>
              <span class="chip-val">${m.var_win_share != null ? m.var_win_share.toFixed(1) + '%' : '--'}</span>
              ${zStr ? `<span class="chip-z">${zStr}</span>` : ''}
              <span class="chip-badge ${decClass}">${dec}</span>
            </div>
          `;
        }).join('');
        setHTML(chipsEl, chipsHtml);
      } else {
        setHTML(chipsEl, '');
      }
    }
  }

  function updateSelfHero(data) {
    const is4p = data.num_snakes === 4;
    if (is4p) {
      setText('baselineName', 'OPPONENTS (3x BASELINE)');
    } else {
      setText('baselineName', (data.strategy_baseline || 'baseline').toUpperCase());
    }
    setText('variantName', (data.strategy_variant || 'variant').toUpperCase());

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

    setText('baselinePts', `${basePts.toFixed(1)} Pts`);
    setText('variantPts', `${varPts.toFixed(1)} Pts`);
    setText('baselineSubtext', `${baseW} Wins`);
    setText('variantSubtext', `${varW} Wins`);
    setText('drawsHeaderRate', `${drawsCount} Draws`);

    // Threshold Markers & Labels
    const labelParity = document.getElementById('labelParity');
    const threshParity = document.getElementById('threshParity');
    const labelKeep = document.getElementById('labelKeep');
    const threshKeep = document.getElementById('threshKeep');
    const labelRevert = document.getElementById('labelRevert');
    const threshRevert = document.getElementById('threshRevert');

    const highStop = data.current_early_stop_high || data.early_stop_high || (is4p ? 35.0 : 60.0);
    const lowStop = data.current_early_stop_low || data.early_stop_low || (is4p ? 18.0 : 40.0);

    if (is4p) {
      if (labelParity) { labelParity.innerText = '25% PARITY'; labelParity.style.left = '75%'; }
      if (threshParity) threshParity.style.left = '75%';
      const keepPos = 100 - highStop;
      if (labelKeep) {
        labelKeep.innerText = `${highStop.toFixed(1)}% KEEP`;
        labelKeep.style.left = `${keepPos}%`;
        labelKeep.style.transform = keepPos < 10 ? 'translateX(-15%)' : 'translateX(-50%)';
      }
      if (threshKeep) threshKeep.style.left = `${keepPos}%`;
      const revertPos = 100 - lowStop;
      if (labelRevert) {
        labelRevert.innerText = `${lowStop.toFixed(1)}% REVERT`;
        labelRevert.style.left = `${revertPos}%`;
        labelRevert.style.transform = revertPos > 88 ? 'translateX(-85%)' : 'translateX(-50%)';
      }
      if (threshRevert) threshRevert.style.left = `${revertPos}%`;
    } else {
      if (labelParity) { labelParity.innerText = '50% PARITY'; labelParity.style.left = '50%'; }
      if (threshParity) threshParity.style.left = '50%';
      const keepPos = 100 - highStop;
      if (labelKeep) {
        labelKeep.innerText = `${highStop.toFixed(1)}% KEEP`;
        labelKeep.style.left = `${keepPos}%`;
        labelKeep.style.transform = keepPos < 10 ? 'translateX(-15%)' : 'translateX(-50%)';
      }
      if (threshKeep) threshKeep.style.left = `${keepPos}%`;
      const revertPos = 100 - lowStop;
      if (labelRevert) {
        labelRevert.innerText = `${lowStop.toFixed(1)}% REVERT`;
        labelRevert.style.left = `${revertPos}%`;
        labelRevert.style.transform = revertPos > 88 ? 'translateX(-85%)' : 'translateX(-50%)';
      }
      if (threshRevert) threshRevert.style.left = `${revertPos}%`;
    }

    // Lead Text
    const leadEl = document.getElementById('leadText');
    const leadTitle = document.getElementById('leadTitle');
    if (leadTitle) leadTitle.innerText = 'WIN VALUE LEAD';
    if (leadEl) {
      if (is4p) {
        const deltaParity = varWS - 25.0;
        if (Math.abs(deltaParity) < 0.05) {
          setText(leadEl, 'EVEN');
          leadEl.style.color = 'var(--text-main)';
        } else if (deltaParity > 0) {
          setText(leadEl, `+${deltaParity.toFixed(1)}%`);
          leadEl.style.color = 'var(--variant-color)';
        } else {
          setText(leadEl, `${deltaParity.toFixed(1)}%`);
          leadEl.style.color = 'var(--danger-color)';
        }
      } else {
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

    // Tug-of-War Bar
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
              if (seg) { seg.style.display = 'none'; seg.style.width = '0%'; }
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
  }

  function updatePoolHero(data) {
    const pool = data.pool_data || {};
    const candWS = (pool.cand_share != null) ? pool.cand_share : 0.0;
    const baseWS = (pool.base_share != null) ? pool.base_share : 0.0;
    const delta = (pool.delta != null) ? pool.delta : (candWS - baseWS);
    const zScore = (pool.z_score != null) ? pool.z_score : 0.0;
    const totalG = pool.total_games || data.total_played || 0;
    const candScore = pool.cand_score || pool.cand_wins || 0.0;
    const baseScore = pool.base_score || pool.base_wins || 0.0;

    setText('baselineName', 'BASELINE vs POOL');
    setText('variantName', 'CANDIDATE vs POOL');
    setText('baselinePts', `${baseWS.toFixed(1)}%`);
    setText('variantPts', `${candWS.toFixed(1)}%`);
    setText('baselineSubtext', `${baseScore.toFixed(1)} / ${totalG} Wins`);
    setText('variantSubtext', `${candScore.toFixed(1)} / ${totalG} Wins`);

    setText('leadTitle', 'POOL WIN SHARE DELTA (Δ)');
    const leadEl = document.getElementById('leadText');
    if (leadEl) {
      leadEl.innerText = `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`;
      leadEl.style.color = delta > 0 ? 'var(--variant-color)' : (delta < 0 ? 'var(--danger-color)' : 'var(--text-main)');
    }
    const zEl = document.getElementById('drawsHeaderRate');
    if (zEl) {
      zEl.innerText = `z = ${zScore > 0 ? '+' : ''}${zScore.toFixed(2)}`;
      zEl.style.color = zScore >= 1.645 ? 'var(--variant-color)' : (zScore <= -1.645 ? 'var(--danger-color)' : '#f59e0b');
    }

    const sumWS = candWS + baseWS;
    const relVarPct = sumWS > 0 ? (candWS / sumWS * 100) : 50.0;
    const relBasePct = 100.0 - relVarPct;

    const tugBaselinesWrap = document.getElementById('tugBaselinesWrap');
    const varPtsBar = document.getElementById('variantPtsBar');
    const baseSeg1 = document.getElementById('baseSeg1');
    const baseSeg2 = document.getElementById('baseSeg2');
    const baseSeg3 = document.getElementById('baseSeg3');
    if (tugBaselinesWrap) tugBaselinesWrap.style.width = `${relBasePct}%`;
    if (varPtsBar) {
      varPtsBar.style.display = 'flex';
      varPtsBar.style.width = `${relVarPct}%`;
      setText('variantPtsBarText', `${candWS.toFixed(1)}%`);
    }
    if (baseSeg1) {
      baseSeg1.style.display = 'flex';
      baseSeg1.style.width = '100%';
      setText('baseSeg1Text', `${baseWS.toFixed(1)}%`);
    }
    if (baseSeg2) { baseSeg2.style.display = 'none'; baseSeg2.style.width = '0%'; }
    if (baseSeg3) { baseSeg3.style.display = 'none'; baseSeg3.style.width = '0%'; }

    const labelParity = document.getElementById('labelParity');
    const threshParity = document.getElementById('threshParity');
    const labelKeep = document.getElementById('labelKeep');
    const threshKeep = document.getElementById('threshKeep');
    const labelRevert = document.getElementById('labelRevert');
    const threshRevert = document.getElementById('threshRevert');

    if (labelParity) { labelParity.innerText = 'PARITY'; labelParity.style.left = '50%'; }
    if (threshParity) threshParity.style.left = '50%';
    if (labelKeep) { labelKeep.innerText = '+1.65 KEEP'; labelKeep.style.left = '40%'; }
    if (threshKeep) threshKeep.style.left = '40%';
    if (labelRevert) { labelRevert.innerText = '-1.65 REVERT'; labelRevert.style.left = '60%'; }
    if (threshRevert) threshRevert.style.left = '60%';
  }

  function updateDepthHero(data) {
    const depth = data.depth_data || {};
    const varD = (depth.median_var_depth != null) ? depth.median_var_depth : 0.0;
    const baseD = (depth.median_base_depth != null) ? depth.median_base_depth : 0.0;
    const deltaD = (depth.delta_depth != null) ? depth.delta_depth : (varD - baseD);
    const npsR = (depth.nps_ratio != null) ? depth.nps_ratio : 1.0;
    const agree = (depth.agreement_pct != null) ? depth.agreement_pct : 100.0;

    setText('baselineName', 'BASELINE SEARCH');
    setText('variantName', 'VARIANT SEARCH');
    setText('baselinePts', `${baseD.toFixed(1)}`);
    setText('variantPts', `${varD.toFixed(1)}`);
    setText('baselineSubtext', `Median Depth`);
    setText('variantSubtext', `Median Depth`);

    setText('leadTitle', 'DEPTH DELTA (Δ)');
    const leadEl = document.getElementById('leadText');
    if (leadEl) {
      leadEl.innerText = `${deltaD > 0 ? '+' : ''}${deltaD.toFixed(1)} Depth`;
      leadEl.style.color = deltaD > 0 ? 'var(--variant-color)' : (deltaD < 0 ? 'var(--danger-color)' : 'var(--text-main)');
    }
    const zEl = document.getElementById('drawsHeaderRate');
    if (zEl) {
      zEl.innerText = `${agree.toFixed(0)}% Move Agreement`;
      zEl.style.color = '#60a5fa';
    }

    const sumD = varD + baseD;
    const relVarPct = sumD > 0 ? (varD / sumD * 100) : 50.0;
    const relBasePct = 100.0 - relVarPct;

    const tugBaselinesWrap = document.getElementById('tugBaselinesWrap');
    const varPtsBar = document.getElementById('variantPtsBar');
    const baseSeg1 = document.getElementById('baseSeg1');
    const baseSeg2 = document.getElementById('baseSeg2');
    const baseSeg3 = document.getElementById('baseSeg3');
    if (tugBaselinesWrap) tugBaselinesWrap.style.width = `${relBasePct}%`;
    if (varPtsBar) {
      varPtsBar.style.display = 'flex';
      varPtsBar.style.width = `${relVarPct}%`;
      setText('variantPtsBarText', `${varD.toFixed(1)}`);
    }
    if (baseSeg1) {
      baseSeg1.style.display = 'flex';
      baseSeg1.style.width = '100%';
      setText('baseSeg1Text', `${baseD.toFixed(1)}`);
    }
    if (baseSeg2) { baseSeg2.style.display = 'none'; baseSeg2.style.width = '0%'; }
    if (baseSeg3) { baseSeg3.style.display = 'none'; baseSeg3.style.width = '0%'; }

    const labelParity = document.getElementById('labelParity');
    const threshParity = document.getElementById('threshParity');
    const labelKeep = document.getElementById('labelKeep');
    const threshKeep = document.getElementById('threshKeep');
    const labelRevert = document.getElementById('labelRevert');
    const threshRevert = document.getElementById('threshRevert');

    if (labelParity) { labelParity.innerText = 'PARITY'; labelParity.style.left = '50%'; }
    if (threshParity) threshParity.style.left = '50%';
    if (labelKeep) { labelKeep.innerText = '+0.5 KEEP'; labelKeep.style.left = '40%'; }
    if (threshKeep) threshKeep.style.left = '40%';
    if (labelRevert) { labelRevert.innerText = '-0.5 REVERT'; labelRevert.style.left = '60%'; }
    if (threshRevert) threshRevert.style.left = '60%';
  }

  function updateRunsCard(data, isRunning, modeElapsed) {
    const runType = data.run_type || 'self';
    const depth = data.depth_data || {};
    const pool = data.pool_data || {};

    let played = 0;
    let maxG = 200;

    if (runType === 'depth') {
      played = depth.completed_positions || data.total_played || 0;
      maxG = depth.total_positions || data.target_games || 12;
      setText('runsValue', `${played} / ${maxG} Positions`);
    } else if (runType === 'pool') {
      played = pool.total_games || data.total_played || 0;
      maxG = data.target_games || data.max_games || 100;
      setText('runsValue', `${played} / ${maxG} Paired Games`);
    } else {
      played = data.total_played || 0;
      maxG = data.target_games || data.max_games || 200;
      if (data.status === 'IDLE' && played === 0) {
        setText('runsValue', `0 / ${maxG} Games`);
      } else {
        setText('runsValue', `${played} / ${maxG} Games`);
      }
    }

    const pctComplete = maxG > 0 ? Math.min(100, (played / maxG) * 100).toFixed(1) : 0;
    setWidth('runsProgress', pctComplete);

    const pace = (isRunning && modeElapsed > 0) ? (played / modeElapsed) : 0;
    if (isRunning) {
      const unit = runType === 'depth' ? 'pos/sec' : 'games/sec';
      setText('paceValue', `${pace.toFixed(3)} ${unit}`);
    } else {
      setText('paceValue', `0.000 games/sec`);
    }

    const remainingGames = Math.max(0, maxG - played);

    if (data.status === 'FINISHED' || data.status === 'STOPPED' || remainingGames === 0) {
      setText('runsSubtext', `${pctComplete}% Complete (${data.status === 'STOPPED' ? 'Stopped' : 'Done'})`);
    } else if (data.status === 'DISCONNECTED') {
      setText('runsSubtext', `${pctComplete}% Complete (Disconnected)`);
    } else if (data.status === 'IDLE' || data.status === 'WAITING' || !isRunning) {
      setText('runsSubtext', `0% Complete`);
    } else if (data.eta_seconds != null && data.eta_seconds > 0 && isRunning) {
      const etaText = formatSeconds(data.eta_seconds);
      setText('runsSubtext', `${pctComplete}% Complete (~${etaText} left)`);
      const subEl = document.getElementById('runsSubtext');
      if (subEl && data.eta_breakdown && data.eta_breakdown.length > 0) {
        subEl.title = 'Total Run ETA: ' + data.eta_breakdown.map(b => `${b.mode}: ${b.games_left}G (~${formatSeconds(b.eta_seconds)})`).join(' | ');
      }
    } else if (pace > 0 && isRunning) {
      const etaSec = remainingGames / pace;
      setText('runsSubtext', `${pctComplete}% Complete (~${formatSeconds(etaSec)} left)`);
    } else {
      setText('runsSubtext', `${pctComplete}% Complete`);
    }
  }

  function updateDecisionBanner(data, isRunning, is4p, varWS, varPts, targetStr, played, maxG) {
    // Deprecated: Decision card removed in favor of top-header status chip (IDLE, RUNNING, KEEP, PARITY, REJECT)
  }

  function updateHistoryTable(data) {
    const historyBody = document.getElementById('historyTableBody');
    const historyCount = document.getElementById('historyCount');
    if (!historyBody) return;

    const historyList = data.run_history || [];
    const currentHistoryJSON = JSON.stringify(historyList);
    if (currentHistoryJSON === lastHistoryJSON) return;
    lastHistoryJSON = currentHistoryJSON;

    if (historyList.length === 0) {
      setText(historyCount, '0 runs');
      setHTML(historyBody, `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 2rem;">No previous simulation runs recorded.</td></tr>`);
      return;
    }

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

  async function updateDashboard() {
    if (isUpdating) return;
    isUpdating = true;
    try {
      const res = await fetch('/api/data');
      if (!res.ok) return;
      const data = await res.json();

      const runType = data.run_type || 'self';
      const isRunning = (data.status === 'RUNNING' && data.runner_alive !== false);

      // Header Status Chip
      const statusBadge = document.getElementById('statusBadge');
      const statusText = document.getElementById('statusText');
      if (statusBadge && statusText) {
        let chipStatus = 'idle';
        let chipLabel = 'IDLE';

        if (isRunning) {
          chipStatus = 'running';
          chipLabel = 'RUNNING';
        } else if (data.status === 'FINISHED' || data.status === 'TEST_FAILURE') {
          const rec = (data.recommendation || '').toUpperCase();
          if (rec === 'KEEP') {
            chipStatus = 'keep';
            chipLabel = 'KEEP';
          } else if (rec === 'PARITY') {
            chipStatus = 'parity';
            chipLabel = 'PARITY';
          } else {
            chipStatus = 'reject';
            chipLabel = 'REJECT';
          }
        } else if (data.status === 'DISCONNECTED') {
          chipStatus = 'reject';
          chipLabel = 'DISCONNECTED';
        } else if (data.status === 'STOPPED') {
          chipStatus = 'idle';
          chipLabel = 'STOPPED';
        } else {
          chipStatus = 'idle';
          chipLabel = 'IDLE';
        }

        statusBadge.className = `badge badge-status-${chipStatus}`;
        setText(statusText, chipLabel);
      }

      const subtext = document.getElementById('headerSubtext');
      if (subtext) {
        const expTitle = data.experiment_desc || 'Unspecified Experiment';
        const newSub = `Testing: ${expTitle}`;
        if (subtext.innerText !== newSub) subtext.innerText = newSub;
        if (subtext.title !== newSub) subtext.title = newSub;
      }

      setText('threadsCount', (data.threads != null && data.threads > 0) ? data.threads : '-');
      setText('elapsedTime', formatSeconds(data.elapsed_seconds || 0));
      const elapsedEl = document.getElementById('elapsedTime');
      const modeElapsed = data.mode_elapsed_seconds || data.elapsed_seconds || 0;
      if (elapsedEl && elapsedEl.parentElement) {
        elapsedEl.parentElement.title = `Total Suite: ${formatSeconds(data.elapsed_seconds || 0)} | Current Mode: ${formatSeconds(modeElapsed)}`;
      }

      // Shared metrics calculation
      const played = data.total_played || 0;
      const is4p = data.num_snakes === 4;
      const drawShare = is4p ? 0.25 : 0.5;
      const baseW = (data.baseline && data.baseline.wins != null) ? data.baseline.wins : 0;
      const varW = (data.variant && data.variant.wins != null) ? data.variant.wins : 0;
      const drawsCount = data.draws ? data.draws.count : ((data.baseline && data.baseline.draws != null) ? data.baseline.draws : 0);
      const basePts = (data.baseline && typeof data.baseline.points === 'number') ? data.baseline.points : (baseW + (1.0 - drawShare) * drawsCount);
      const varPts = (data.variant && typeof data.variant.points === 'number') ? data.variant.points : (varW + drawShare * drawsCount);
      const targetWS = data.target_win_share || (is4p ? 25.0 : 50.0);
      const varWS = (data.variant && typeof data.variant.win_share === 'number') ? data.variant.win_share : (played > 0 ? (varPts / played * 100) : targetWS);
      const targetStr = is4p ? '25.0% Individual Parity' : '50.0% Parity';
      const maxG = data.target_games || data.max_games || 200;

      // Stage Rail & Details
      updateStageRail(data);
      updateMatrixUI(data);
      updateDepthUI(data);
      updatePoolUI(data);

      // Hero & Idle presentation (Requirement 6)
      updateHeroAndIdleState(data);

      const isRunActive = isRunning || (data.status === 'FINISHED' && played > 0);
      if (isRunActive) {
        if (runType === 'depth') {
          updateDepthHero(data);
        } else if (runType === 'pool') {
          updatePoolHero(data);
        } else {
          updateSelfHero(data);
        }

        updateRunsCard(data, isRunning, modeElapsed);
        updateDecisionBanner(data, isRunning, is4p, varWS, varPts, targetStr, played, maxG);
      }

      updateHistoryTable(data);

    } catch (e) {
      console.error('Dashboard update error:', e);
      const statusBadge = document.getElementById('statusBadge');
      const statusText = document.getElementById('statusText');
      if (statusBadge && statusText) {
        statusBadge.className = 'badge badge-status-reject';
        setText(statusText, 'DISCONNECTED');
      }
    } finally {
      isUpdating = false;
    }
  }

  ['unit', 'depth', 'self', 'pool'].forEach(id => {
    const el = document.getElementById(`railSegment-${id}`);
    if (el) el.addEventListener('click', () => selectStage(id));
  });
  const depthToggle = document.getElementById('depthToggleBtn');
  if (depthToggle) depthToggle.addEventListener('click', toggleDepthTable);

  setInterval(updateDashboard, 500);
  updateDashboard();
</script>
</body>
</html>
"""

def stop_active_tasks(state: DashboardState) -> Dict[str, Any]:
    return {"status": "error", "message": "Method Not Allowed - Dashboard is view-only"}


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    state: Optional[DashboardState] = None

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path in ("/favicon.ico", "/favicon.svg"):
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(FAVICON_SVG.encode("utf-8"))
            return

        if parsed_path == "/api/ping":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            return

        if parsed_path == "/api/check_skip":
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

        if parsed_path in ("/api/data", "/api/state"):
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
        if self.path in ("/api/skip", "/api/cancel", "/api/stop"):
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                self.rfile.read(content_length)
            self.send_response(405)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Method Not Allowed - Dashboard is view-only"}).encode("utf-8"))
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

        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            self.rfile.read(content_length)
        self.send_response(405)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Method Not Allowed - Dashboard is view-only"}).encode("utf-8"))

    def log_message(self, format, *args):
        pass

class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def start_inactivity_watchdog(state: DashboardState, timeout_seconds: int = 600):
    if timeout_seconds <= 0:
        return

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
        except urllib.error.HTTPError as e:
            return {"status": "error", "code": e.code, "error": f"HTTP {e.code}: {e.reason}"}
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
        except urllib.error.HTTPError as e:
            return {"status": "error", "code": e.code, "error": f"HTTP {e.code}: {e.reason}"}
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
        suite_start_time: Optional[float] = None,
        run_type: str = "self",
    ):
        payload = {
            "action": "init",
            "pid": pid if pid is not None else os.getpid(),
            "run_type": run_type,
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
        if suite_start_time is not None:
            payload["suite_start_time"] = suite_start_time
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

    def update_pool_game(self, **kwargs):
        payload = {"action": "update_pool_game"}
        payload.update(kwargs)
        self.post_update(payload)

    def update_pool_mode(self, **kwargs):
        payload = {"action": "update_pool_mode"}
        payload.update(kwargs)
        self.post_update(payload, sync=True)

    def init_depth(self, **kwargs):
        payload = {"action": "init_depth"}
        payload.update(kwargs)
        self.post_update(payload, sync=True)

    def update_depth_position(self, **kwargs):
        payload = {"action": "update_depth_position"}
        payload.update(kwargs)
        self.post_update(payload)

    def update_unit_tests(self, status: str = "PASSED", passed: int = 40, total: int = 40, details: str = ""):
        self.post_update({
            "action": "update_unit_tests",
            "status": status,
            "passed": passed,
            "total": total,
            "details": details,
        }, sync=True)

    def finish_depth(self, **kwargs):
        payload = {"action": "finish_depth"}
        payload.update(kwargs)
        self.post_update(payload, sync=True)


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
    state_file = args.load_state or ("/tmp/dashboard_live_state.json" if os.path.exists("/tmp/dashboard_live_state.json") else "")
    if state_file and os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
            mtime = os.path.getmtime(state_file)
            if time.time() - mtime < 3600:
                state.handle_update({"action": "restore", "data": saved_data})
                print(f"[*] Restored dashboard state from {state_file}")
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
