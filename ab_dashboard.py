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
import datetime
import html
import http.server
import json
import math
import os
import re
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
        with p.open("r", encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, list):
                entries = d

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
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp_path, p)


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


DEFAULT_SELF_RATES = {
    "constrictor": 1.80,   # ~6 min for 200 games (fast death, short turn count)
    "royale": 14.56,       # ~48 min for 200 games (hazard ring constriction)
    "duel": 9.98,          # ~33 min for 200 games (2 snakes, fast turns)
    "standard": 26.55,     # ~88 min for 200 games (receding tails, long turn count)
}

DEFAULT_POOL_RATES = {
    "constrictor": 1.40,
    "royale": 11.65,
    "duel": 10.65,
    "standard": 22.02,
}

DEFAULT_DEPTH_DURATION = 23.3  # ~23s for 12 fixtures across parallel cores
DEFAULT_UNIT_DURATION = 5.0    # ~5s for pre-flight tactical safety suite


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
    unit_tests_data: Optional[dict] = None,
    pool_data: Optional[dict] = None,
    include_pool_stage: bool = False,
) -> Dict[str, Any]:
    """
    Computes FULL SUITE ETA across all remaining stages and modes:
    1. Stage 1 (Unit Tests): ~5s if running/pending, 0s if passed.
    2. Stage 2 (Depth Benchmark): remaining fixtures * spp (~23s if pending, 0s if completed).
    3. Stage 3 (Self-Play Matrix): remaining games * mode rate across all queued matrix modes.
    4. Stage 4 (Zoo Pool Gate): remaining games across pool modes if active pool run or queued.
    """
    timings = mode_timings if mode_timings is not None else load_mode_timings(timings_path)
    has_any_history = bool(timings and len(timings) > 0)

    curr_mode_clean = str(current_mode).strip().lower()
    effective_min_stable = 10 if run_type == "pool" else min_stable_games
    curr_live_spg = (current_mode_elapsed / current_played) if (current_played > 0 and current_mode_elapsed > 0) else None
    is_curr_stable = bool(current_played >= effective_min_stable and curr_live_spg is not None)

    # Legacy fallback when no history at all and run is not a depth test
    if not has_any_history and run_type != "depth":
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
    fallback_spg = curr_live_spg if curr_live_spg is not None else medians.get(curr_mode_clean)

    # ── Check matrix completion / activity state ──
    any_matrix_played = False
    any_matrix_running = False
    if matrix_progress:
        for m in matrix_progress:
            st = str(m.get("status", "")).upper()
            g = m.get("games") if m.get("games") is not None else (m.get("total_played") or 0)
            if g > 0 or st in ("PASSED", "PARITY", "FAILED"):
                any_matrix_played = True
            if st == "RUNNING":
                any_matrix_running = True

    # ── Stage 1: Tactical Unit Tests ──
    rem_unit_sec = 0.0
    if unit_tests_data is not None:
        unit_st = str(unit_tests_data.get("status", "PENDING")).upper()
        if unit_st in ("PASSED", "DONE", "FINISHED", "FAILED", "SKIPPED"):
            rem_unit_sec = 0.0
        elif unit_st == "RUNNING":
            rem_unit_sec = max(0.5, DEFAULT_UNIT_DURATION - current_mode_elapsed) if run_type == "unit" else 2.5
        elif run_type in ("unit", "self", "depth") and current_played == 0 and not any_matrix_played and not any_matrix_running:
            rem_unit_sec = DEFAULT_UNIT_DURATION

    # ── Stage 2: Depth Benchmark ──
    rem_depth_sec = 0.0
    depth_spg = None
    total_pos = 12
    completed_pos = 0

    if run_type == "depth":
        depth_info = depth_data or {}
        total_pos = depth_info.get("total_positions", 12) or 12
        completed_pos = depth_info.get("completed_positions", current_played) or current_played
        rem_pos = max(0, total_pos - completed_pos)
        time_ms = depth_info.get("time_ms", 450) or 450
        repeats = depth_info.get("repeats", 5) or 5
        live_spp = (current_mode_elapsed / completed_pos) if (completed_pos > 0 and current_mode_elapsed > 0) else None
        budget_spp = (repeats * 2 * time_ms) / 1000.0
        depth_spg = live_spp if live_spp is not None else budget_spp
        rem_depth_sec = rem_pos * depth_spg
        is_curr_stable = bool(completed_pos >= 2 and live_spp is not None)
    elif depth_data is not None and run_type != "pool":
        depth_info = depth_data
        total_pos = depth_info.get("total_positions", 12) or 12
        completed_pos = depth_info.get("completed_positions", 0) or 0
        depth_finished = bool(
            depth_info.get("passed") is not None
            or (completed_pos >= total_pos and completed_pos > 0)
            or depth_info.get("duration_sec")
            or any_matrix_played
            or (run_type == "self" and (current_played > 0 or any_matrix_running))
        )
        if not depth_finished:
            rem_depth_sec = DEFAULT_DEPTH_DURATION

    # ── Stage 3: Self-Play Matrix Modes / Pool Modes in matrix_progress ──
    rem_matrix_sec = 0.0
    matrix_breakdown = []
    has_valid_rate = False

    modes_to_calc = []
    if matrix_progress and isinstance(matrix_progress, list) and len(matrix_progress) > 0:
        for item in matrix_progress:
            m_name = str(item.get("mode", "")).strip().lower()
            status = str(item.get("status", "PENDING")).strip().upper()
            m_max = item.get("max_games", 200) or 200
            is_curr = (run_type in ("self", "pool") and (m_name == curr_mode_clean or status == "RUNNING"))
            if is_curr:
                m_left = max(0, (current_max_games or m_max) - current_played)
            else:
                if status in ("PASSED", "PARITY", "FAILED", "SKIPPED"):
                    m_left = 0
                else:
                    m_left = m_max
            modes_to_calc.append((m_name, m_left, is_curr, status))
    elif matrix_progress is None or (matrix_progress == [] and run_type not in ("depth", "pool")):
        for m_name in ("constrictor", "royale", "duel", "standard"):
            modes_to_calc.append((m_name, 200, False, "PENDING"))

    for m_name, m_left, is_curr, m_status in modes_to_calc:
        if m_status in ("PASSED", "PARITY", "FAILED", "SKIPPED") or m_left <= 0:
            matrix_breakdown.append({
                "mode": m_name,
                "games_left": 0,
                "rate_spg": round(medians.get(m_name, DEFAULT_SELF_RATES.get(m_name, 10.0)), 3),
                "rate_source": "done",
                "eta_seconds": 0.0,
            })
            continue

        spg = None
        source = "history"
        if is_curr and is_curr_stable:
            spg = curr_live_spg
            source = "live"
        else:
            if m_name in medians:
                spg = medians[m_name]
                source = "history"
            elif fallback_spg is not None:
                spg = fallback_spg
                source = "fallback"
            else:
                default_dict = DEFAULT_POOL_RATES if run_type == "pool" else DEFAULT_SELF_RATES
                spg = default_dict.get(m_name, 15.0)
                source = "default"

        if spg is not None and spg > 0:
            has_valid_rate = True
            mode_eta = m_left * spg
            rem_matrix_sec += mode_eta
            matrix_breakdown.append({
                "mode": m_name,
                "games_left": m_left,
                "rate_spg": round(spg, 3),
                "rate_source": source,
                "eta_seconds": round(mode_eta, 1),
            })
        else:
            matrix_breakdown.append({
                "mode": m_name,
                "games_left": m_left,
                "rate_spg": None,
                "rate_source": "none",
                "eta_seconds": None,
            })

    # ── Stage 4: Zoo Pool Gate Modes (when pool_data provided and not already in matrix_progress, or queued after self-play) ──
    rem_pool_sec = 0.0
    pool_breakdown = []
    medians_pool = get_mode_medians(timings, target_run_type="pool") if timings else {}

    if run_type == "pool" and not modes_to_calc and pool_data:
        p_modes = pool_data.get("mode_results") or pool_data.get("modes") or pool_data.get("mode_stats") or {}
        for m_name in ("constrictor", "royale", "duel", "standard"):
            pm = p_modes.get(m_name, {})
            p_dec = pm.get("decision")
            p_max = pm.get("max_games", 20) or 20
            is_curr_pool = (m_name == curr_mode_clean)
            cand_g = pm.get("cand_games", current_played if is_curr_pool else 0)
            if p_dec or cand_g >= p_max:
                p_left = 0
            elif is_curr_pool:
                p_left = max(0, p_max - current_played)
            else:
                p_left = p_max

            spg = (current_mode_elapsed / current_played) if (is_curr_pool and current_played >= 5 and current_mode_elapsed > 0) else (medians_pool.get(m_name) or DEFAULT_POOL_RATES.get(m_name, 12.0))
            m_eta = p_left * spg
            rem_pool_sec += m_eta
            pool_breakdown.append({
                "mode": m_name,
                "stage": "pool",
                "games_left": p_left,
                "rate_spg": round(spg, 3),
                "rate_source": "live" if is_curr_pool else "history",
                "eta_seconds": round(m_eta, 1),
            })
    elif include_pool_stage and run_type in ("self", "unit", "depth"):
        # Pool gate is queued for after self-play
        target_pool_modes = [str(item.get("mode", "")).strip().lower() for item in (matrix_progress or []) if item.get("mode")]
        if not target_pool_modes:
            target_pool_modes = ["constrictor", "royale", "duel", "standard"]
        for m_name in target_pool_modes:
            spg = medians_pool.get(m_name) or DEFAULT_POOL_RATES.get(m_name, 12.0)
            p_left = 50
            m_eta = p_left * spg
            rem_pool_sec += m_eta
            pool_breakdown.append({
                "mode": m_name,
                "stage": "pool",
                "games_left": p_left,
                "rate_spg": round(spg, 3),
                "rate_source": "history" if m_name in medians_pool else "default",
                "eta_seconds": round(m_eta, 1),
            })

    # ── Full Suite Total ETA ──
    total_suite_eta = rem_unit_sec + rem_depth_sec + rem_matrix_sec + rem_pool_sec
    final_eta = round(total_suite_eta, 1) if (total_suite_eta > 0 and (has_valid_rate or rem_depth_sec > 0 or rem_unit_sec > 0 or rem_pool_sec > 0)) else None

    # Combined breakdown for callers and UI
    combined_breakdown = []
    if run_type == "depth" or (depth_data is not None and rem_depth_sec > 0):
        combined_breakdown.append({
            "mode": "depth_fixtures",
            "stage": "depth",
            "games_left": max(0, total_pos - completed_pos),
            "rate_spg": round(depth_spg, 3) if depth_spg is not None else round(DEFAULT_DEPTH_DURATION / total_pos, 3),
            "rate_source": "live" if (run_type == "depth" and completed_pos >= 2 and current_mode_elapsed > 0) else "budget",
            "eta_seconds": round(rem_depth_sec, 1),
        })
    combined_breakdown.extend(matrix_breakdown)
    combined_breakdown.extend(pool_breakdown)

    return {
        "total_eta_seconds": final_eta,
        "has_history": bool(has_any_history or run_type == "depth"),
        "is_stable": is_curr_stable,
        "breakdown": combined_breakdown,
        "stage_etas": {
            "unit": round(rem_unit_sec, 1),
            "depth": round(rem_depth_sec, 1),
            "self": round(rem_matrix_sec, 1),
            "pool": round(rem_pool_sec, 1),
        },
    }

def consolidate_run_history(history: list) -> list:
    """Consolidates consecutive/recent runs that share the same experiment description
    (e.g., self-play matrix followed by external pool benchmark) into a unified entry.
    """
    if not history:
        return []
    consolidated = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        desc = entry.get("experiment_desc") or entry.get("description")
        matched_idx = None
        for i in range(len(consolidated) - 1, -1, -1):
            prev = consolidated[i]
            prev_desc = prev.get("experiment_desc") or prev.get("description")
            if desc and prev_desc == desc:
                has_mr_prev = prev.get("matrix_results") is not None
                has_mr_curr = entry.get("matrix_results") is not None
                has_pool_prev = prev.get("pool_data") is not None
                has_pool_curr = entry.get("pool_data") is not None

                if (has_mr_prev and not has_pool_prev and has_pool_curr) or \
                   (has_pool_prev and not has_mr_prev and has_mr_curr):
                    matched_idx = i
                    break

        if matched_idx is not None:
            prev = dict(consolidated[matched_idx])
            if entry.get("matrix_results") is not None and prev.get("matrix_results") is None:
                prev["matrix_results"] = entry["matrix_results"]
            if entry.get("depth_results") is not None and prev.get("depth_results") is None:
                prev["depth_results"] = entry["depth_results"]
            if entry.get("pool_data") is not None and prev.get("pool_data") is None:
                prev["pool_data"] = entry["pool_data"]
            if entry.get("timestamp"):
                prev["timestamp"] = entry["timestamp"]
            if entry.get("recommendation"):
                prev["recommendation"] = entry["recommendation"]
            if entry.get("stop_type"):
                prev["stop_type"] = entry["stop_type"]
            if entry.get("stop_reason"):
                prev["stop_reason"] = entry["stop_reason"]
            if entry.get("total_games", 0) > prev.get("total_games", 0):
                prev["total_games"] = entry["total_games"]
                if "baseline" in entry:
                    prev["baseline"] = entry["baseline"]
                if "variant" in entry:
                    prev["variant"] = entry["variant"]
            if entry.get("unit_tests") and not prev.get("unit_tests"):
                prev["unit_tests"] = entry["unit_tests"]
            consolidated[matched_idx] = prev
        else:
            consolidated.append(dict(entry))
    return consolidated


def load_run_history(max_entries: Optional[int] = 10) -> list:
    global _HISTORY_CACHE, _HISTORY_MTIME
    history_file = Path(__file__).resolve().parent.parent / "ab_results" / "ab_history.json"
    if not history_file.exists():
        history_file = Path(__file__).parent / "ab_results" / "ab_history.json"
    if history_file.exists():
        mtime = history_file.stat().st_mtime
        if _HISTORY_CACHE is not None and mtime == _HISTORY_MTIME:
            return _HISTORY_CACHE
        with history_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                consolidated = consolidate_run_history(data)
                sliced = consolidated[-max_entries:] if (max_entries is not None and len(consolidated) > max_entries) else consolidated
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
    return []


_SERVING_STATS_CACHE = None
_SERVING_STATS_MTIME = None
_SERVING_STATS_SIZE = None


def compute_serving_stats(
    metrics_path: Optional[Path] = None,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Read ab_results/move_metrics.jsonl and aggregate serving stats for today and the last 7 days:
    - games
    - moves_timed_out_server_side (and server_timeouts)
    - contention_total
    - fallback_total
    - truncated_total
    """
    global _SERVING_STATS_CACHE, _SERVING_STATS_MTIME, _SERVING_STATS_SIZE

    def empty_stats() -> Dict[str, Any]:
        return {
            "today": {
                "games": 0,
                "moves_timed_out_server_side": 0,
                "server_timeouts": 0,
                "contention_total": 0,
                "fallback_total": 0,
                "truncated_total": 0,
                "overrun_total": 0,
                "stale_dropped_total": 0,
            },
            "last_7_days": {
                "games": 0,
                "moves_timed_out_server_side": 0,
                "server_timeouts": 0,
                "contention_total": 0,
                "fallback_total": 0,
                "truncated_total": 0,
                "overrun_total": 0,
                "stale_dropped_total": 0,
            },
        }

    target = metrics_path
    is_custom = target is not None or now is not None
    if target is None:
        target = Path(__file__).resolve().parent.parent / "ab_results" / "move_metrics.jsonl"
        if not target.exists():
            target = Path(__file__).parent / "ab_results" / "move_metrics.jsonl"

    if not target.exists():
        return empty_stats()

    try:
        stat_info = target.stat()
        mtime = stat_info.st_mtime
        size = stat_info.st_size
    except OSError:
        return empty_stats()

    if not is_custom and _SERVING_STATS_CACHE is not None and mtime == _SERVING_STATS_MTIME and size == _SERVING_STATS_SIZE:
        return _SERVING_STATS_CACHE

    curr_now = now or datetime.datetime.now()
    today_date = curr_now.date()
    cutoff_7d_date = today_date - datetime.timedelta(days=6)

    stats = empty_stats()

    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Only process game summary records (sparse anomaly records have 'kind' and no 'turns')
                if "turns" not in obj or "kind" in obj:
                    continue

                raw_ts = obj.get("timestamp")
                if raw_ts is None:
                    continue
                try:
                    if isinstance(raw_ts, (int, float)):
                        dt = datetime.datetime.fromtimestamp(raw_ts)
                    else:
                        dt = datetime.datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                except Exception:
                    continue

                rec_date = dt.date()
                server_timeouts = int(obj.get("server_timeouts", 0))
                contention = int(obj.get("contention_count", obj.get("contention", 0)))
                fallback = int(obj.get("fallback_count", obj.get("fallback", 0)))
                truncated = int(obj.get("truncated_search_count", obj.get("truncated_total", obj.get("truncated", 0))))
                overrun = int(obj.get("overrun_count", obj.get("overrun_total", obj.get("overrun", 0))))
                stale_dropped = int(obj.get("stale_dropped_count", obj.get("stale_dropped_total", obj.get("stale_dropped", 0))))

                if rec_date == today_date:
                    t = stats["today"]
                    t["games"] += 1
                    t["moves_timed_out_server_side"] += server_timeouts
                    t["server_timeouts"] += server_timeouts
                    t["contention_total"] += contention
                    t["fallback_total"] += fallback
                    t["truncated_total"] += truncated
                    t["overrun_total"] += overrun
                    t["stale_dropped_total"] += stale_dropped

                if cutoff_7d_date <= rec_date <= today_date:
                    w = stats["last_7_days"]
                    w["games"] += 1
                    w["moves_timed_out_server_side"] += server_timeouts
                    w["server_timeouts"] += server_timeouts
                    w["contention_total"] += contention
                    w["fallback_total"] += fallback
                    w["truncated_total"] += truncated
                    w["overrun_total"] += overrun
                    w["stale_dropped_total"] += stale_dropped
    except Exception:
        return empty_stats()

    if not is_custom:
        _SERVING_STATS_CACHE = stats
        _SERVING_STATS_MTIME = mtime
        _SERVING_STATS_SIZE = size

    return stats


def calculate_rate_gpm(played: int, elapsed_seconds: float) -> float:
    """Calculate games per minute from played games and elapsed seconds."""
    if elapsed_seconds <= 0 or played <= 0:
        return 0.0
    return (played / elapsed_seconds) * 60.0


def format_rate_gpm(rate_gpm: float, unit: str = "games/min") -> str:
    """Format rate as 'X.X games/min' (rounded to 1 decimal place)."""
    return f"{rate_gpm:.1f} {unit}"


def format_duration(seconds: Optional[float], allow_decimal: bool = False, default: str = "--") -> str:
    """Format duration into XX:XX:XX format dropping leading zeros.
    Examples:
      - 5520s -> '1:32:00'
      - 2913s -> '48:33'
      - 358s -> '5:58'
      - 23s -> '0:23'
      - 0s -> '0:00'
      - 36005s -> '10:00:05'
    """
    if seconds is None:
        return default
    try:
        s = float(seconds)
    except (ValueError, TypeError):
        return default
    if s < 0 or math.isnan(s):
        return default

    total_sec = int(round(s))
    hours = total_sec // 3600
    mins = (total_sec % 3600) // 60
    secs = total_sec % 60

    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def format_left_duration(eta_seconds: Optional[float], default: str = "--") -> str:
    """Format LEFT ETA duration into XX:XX:XX format dropping leading zeros."""
    if eta_seconds is None:
        return default
    try:
        s = float(eta_seconds)
    except (ValueError, TypeError):
        return default
    if s <= 0 or math.isnan(s):
        return default
    return format_duration(s, default=default)


format_elapsed_clock = format_duration
format_eta_minutes = format_left_duration


def derive_ui_state(data: Optional[Dict[str, Any]]) -> str:
    """Returns one of: 'EMPTY', 'RUNNING', 'COMPLETE', 'ABORTED'

    EMPTY: no experiment / no planned games (total_planned_games == 0 or missing experiment_desc).
           DISCONNECTED must never be the state of a dashboard with no run.
    RUNNING: runner_alive == True
    COMPLETE: run ended with a verdict (stop_type in MATRIX_COMPLETE/KEEP/PARITY/REVERT/FINISHED/TEST_FAILURE
              or status in FINISHED/TEST_FAILURE or stop_reason/recommendation set).
    ABORTED: runner_alive == False and stop_type in ('DISCONNECTED', 'FAILED'), with an experiment in progress.
    """
    if not data:
        return "EMPTY"

    exp_desc = str(data.get("experiment_desc") or "").strip()
    try:
        total_planned = int(data.get("total_planned_games") or 0)
    except (ValueError, TypeError):
        total_planned = 0

    if not exp_desc or total_planned == 0:
        return "EMPTY"

    if data.get("runner_alive") is True:
        return "RUNNING"

    stop_type = str(data.get("stop_type") or "").strip().upper()
    status = str(data.get("status") or "").strip().upper()

    if stop_type in ("DISCONNECTED", "FAILED"):
        return "ABORTED"

    verdict_stop_types = {
        "MATRIX_COMPLETE",
        "KEEP",
        "PARITY",
        "REVERT",
        "FINISHED",
        "TEST_FAILURE",
        "MAXIMUM_REACHED",
        "EARLY_STOP_HIGH",
        "EARLY_STOP_LOW",
        "USER_SKIPPED",
    }
    if stop_type in verdict_stop_types or status in ("FINISHED", "TEST_FAILURE"):
        return "COMPLETE"

    if data.get("stop_reason") or data.get("recommendation"):
        return "COMPLETE"

    return "EMPTY"


def derive_status_pill(data: Optional[Dict[str, Any]], current_time: Optional[float] = None) -> Tuple[str, str, bool]:
    """Returns (status_text, status_class, is_visible).

    STATUS chip: exactly one word, derived in this order:
      (a) runner_alive true and last_game_time under 120 seconds old -> RUNNING (blue)
      (b) stop_type DISCONNECTED or FAILED, or status TEST_FAILURE -> FAILED (red)
      (c) a recommendation exists -> KEEP (green) or REJECT (red) by table:
            KEEP -> KEEP for every target
            PARITY -> KEEP for targets PARITY, BUG FIX, HARNESS, REJECT for target IMPROVEMENT
            REVERT -> REJECT
      (d) run ended with no recommendation -> FAILED (red)
    Rendered only when a run exists (no run = neither chip, ever).
    """
    if not data:
        return ("", "idle", False)

    ui_state = derive_ui_state(data)
    if ui_state == "EMPTY":
        return ("", "idle", False)

    # Normalize target
    raw_target = data.get("target")
    norm_target = str(raw_target).strip().upper() if raw_target is not None else ""
    if "/" in norm_target and "PARITY" in norm_target:
        norm_target = "PARITY"

    # (a) runner_alive true and last_game_time under 120 seconds old -> RUNNING (blue)
    runner_alive = bool(data.get("runner_alive", False))
    now_sec = current_time if current_time is not None else time.time()
    last_game_time = float(data.get("last_game_time") or 0.0)
    last_game_age = max(0.0, now_sec - last_game_time) if last_game_time > 0 else 0.0
    is_under_120 = (last_game_time <= 0 or last_game_age < 120.0)

    if runner_alive and is_under_120:
        return ("RUNNING", "running", True)

    # (b) stop_type DISCONNECTED or FAILED, or status TEST_FAILURE -> WORSE (red)
    stop_type = str(data.get("stop_type") or "").strip().upper()
    status = str(data.get("status") or "").strip().upper()
    rec = str(data.get("recommendation") or "").strip().upper()

    if stop_type in ("DISCONNECTED", "FAILED") or status == "TEST_FAILURE" or rec in ("REVERT", "REJECT", "WORSE"):
        return ("WORSE", "worse", True)

    # (c) a recommendation exists -> BETTER, EQUAL, WORSE
    if rec:
        if rec in ("KEEP", "BETTER"):
            return ("BETTER", "better", True)
        elif rec in ("PARITY", "EQUAL"):
            return ("EQUAL", "equal", True)
        elif rec in ("REVERT", "REJECT", "WORSE"):
            return ("WORSE", "worse", True)

    if stop_type in ("PARITY", "MATRIX_COMPLETE"):
        return ("EQUAL", "equal", True)

    # (d) run ended with no recommendation -> WORSE (red)
    return ("WORSE", "worse", True)


def compute_completeness_line(data: Dict[str, Any]) -> str:
    """Computes completeness summary string for aborted runs, e.g.
    'Partial results — matrix complete (800/800), pool unplayed.'
    """
    parts = []
    matrix = data.get("matrix_progress") or data.get("self_play_matrix") or []
    if matrix:
        matrix_played = sum(int(m.get("games") if m.get("games") is not None else (m.get("total_played") or 0)) for m in matrix)
        matrix_max = sum(int(m.get("max_games") or 200) for m in matrix) or 800
        done_count = sum(1 for m in matrix if str(m.get("status") or "").upper() in ("PASSED", "PARITY", "FAILED", "DONE"))
        if done_count == len(matrix) and matrix_played >= matrix_max:
            parts.append(f"matrix complete ({matrix_played}/{matrix_max})")
        elif matrix_played > 0:
            parts.append(f"matrix partial ({matrix_played}/{matrix_max})")
        else:
            parts.append("matrix unplayed")
    else:
        parts.append("matrix unplayed")

    pool = data.get("pool_data") or {}
    pool_games = int(pool.get("total_games") or 0)
    if pool_games == 0:
        parts.append("pool unplayed")
    else:
        parts.append(f"pool partial ({pool_games} games)")

    return f"Partial results — {', '.join(parts)}."


def determine_interrupted_stage(data: Dict[str, Any]) -> str:
    """Determines which stage was active/interrupted when run aborted:
    returns 'unit', 'depth', 'self', or 'pool'.
    """
    reason = str(data.get("stop_reason") or "").lower()
    if "unit" in reason or "pre-flight" in reason:
        return "unit"
    if "depth" in reason or "fixture" in reason:
        return "depth"
    if "pool" in reason or "zoo" in reason:
        return "pool"
    if "matrix" in reason or "self-play" in reason or "simulation" in reason:
        return "self"

    run_type = str(data.get("run_type") or "self")
    if run_type == "depth":
        return "depth"
    if run_type == "pool":
        return "pool"

    unit_data = data.get("unit_tests_data") or {}
    if unit_data.get("status") == "FAILED" or (unit_data.get("total") and (unit_data.get("passed", 0) < unit_data["total"])):
        return "unit"
    depth_data = data.get("depth_data") or {}
    if not depth_data.get("completed_positions") or depth_data.get("completed_positions", 0) < 12:
        return "depth"

    matrix = data.get("matrix_progress") or data.get("self_play_matrix") or []
    matrix_played = sum(int(m.get("games") if m.get("games") is not None else (m.get("total_played") or 0)) for m in matrix)
    matrix_max = sum(int(m.get("max_games") or 200) for m in matrix) or 800
    if matrix_played < matrix_max:
        return "self"
    return "pool"


def format_compact_nps(val: Optional[float]) -> str:
    """Format NPS into compact notation (e.g. 1.24M, 85.0k, 100k)."""
    if val is None or val <= 0:
        return "--"
    num = float(val)
    if num >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    if num >= 1_000:
        k = num / 1_000
        return f"{k:.1f}k" if k < 100 else f"{k:.0f}k"
    return str(int(round(num)))


def compute_mode_fixture_nps(depth_data: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """Compute the median base_med_nps and var_med_nps for each mode from depth_data.fixtures."""
    fixtures = (depth_data or {}).get("fixtures") or []
    mode_fixtures: Dict[str, List[Dict[str, Any]]] = {}
    for f in fixtures:
        m = str(f.get("mode", "")).strip().lower().replace(" 4p", "").replace(" 1v1", "")
        if m:
            mode_fixtures.setdefault(m, []).append(f)
    result = {}
    for m, fixs in mode_fixtures.items():
        b_list = [float(f["base_med_nps"]) for f in fixs if f.get("base_med_nps") is not None]
        v_list = [float(f["var_med_nps"]) for f in fixs if f.get("var_med_nps") is not None]
        b_med = float(statistics.median(b_list)) if b_list else 0.0
        v_med = float(statistics.median(v_list)) if v_list else 0.0
        result[m] = {"base_med_nps": b_med, "var_med_nps": v_med}
    return result


def render_depth_mode_summary_rows(depth_data: Optional[Dict[str, Any]]) -> str:
    """Pre-render the 4 standardized mode summary rows from depth_data.mode_summaries."""
    mode_sums = (depth_data or {}).get("mode_summaries") or {}
    mode_order = [
        ("constrictor", "Constrictor 4P"),
        ("royale", "Royale 4P"),
        ("duel", "Duel 1v1"),
        ("standard", "Standard 4P"),
    ]
    fixture_nps_map = compute_mode_fixture_nps(depth_data) if mode_sums else {}
    rows = []
    for m_key, m_name in mode_order:
        ms = mode_sums.get(m_key)
        if not ms:
            rows.append(f'''
                  <tr>
                    <td class="td-left" style="font-weight: 700;">{m_name}</td>
                    <td class="td-right">--</td>
                    <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">--</td>
                    <td class="td-right" style="color: var(--pipeline-text-muted); font-weight: 700;">--</td>
                    <td class="td-right">--</td>
                    <td class="td-right">--</td>
                    <td class="td-right" style="font-weight: 700; color: var(--pipeline-text-muted);">--</td>
                    <td class="td-right">--</td>
                    <td class="td-right"><span class="status-chip status-chip-queued matrix-col-status matrix-status-queued">QUEUED</span></td>
                  </tr>''')
            continue
        base_d = float(ms.get("base_med_depth", 0.0) or 0.0)
        var_d = float(ms.get("var_med_depth", 0.0) or 0.0)
        delta_d = float(ms.get("delta_depth", 0.0) or (var_d - base_d))
        nps_r = float(ms.get("nps_ratio", 1.0) or 1.0)
        agree = float(ms.get("agreement_pct", 100.0) or 100.0)

        mode_nps = fixture_nps_map.get(m_key) or {}
        base_nps = mode_nps.get("base_med_nps") if mode_nps.get("base_med_nps") is not None else float(ms.get("base_med_nps", 0.0) or 0.0)
        var_nps = mode_nps.get("var_med_nps") if mode_nps.get("var_med_nps") is not None else float(ms.get("var_med_nps", 0.0) or 0.0)
        base_nps_compact = format_compact_nps(base_nps)
        var_nps_compact = format_compact_nps(var_nps)
        base_nps_raw = int(round(base_nps))
        var_nps_raw = int(round(var_nps))

        is_reg = bool(ms.get("regression") or ms.get("nps_regressed"))
        if is_reg:
            nps_color = "var(--pipeline-red)"
        elif nps_r < 1.0:
            nps_color = "var(--pipeline-amber)"
        else:
            nps_color = "var(--pipeline-green)"

        is_depth_reg = bool(ms.get("depth_regressed"))
        if is_depth_reg:
            delta_color = "var(--pipeline-red)"
        elif delta_d > 0:
            delta_color = "var(--pipeline-green)"
        else:
            delta_color = "var(--pipeline-text-muted)"

        if is_reg or is_depth_reg:
            st_text = "WORSE"
            st_class = "worse"
        elif delta_d > 0:
            st_text = "BETTER"
            st_class = "better"
        else:
            st_text = "EQUAL"
            st_class = "equal"
        sign = "+" if delta_d > 0 else ""
        rows.append(f'''
                  <tr>
                    <td class="td-left" style="font-weight: 700;">{m_name}</td>
                    <td class="td-right">{base_d:.1f}</td>
                    <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">{var_d:.1f}</td>
                    <td class="td-right" style="color: {delta_color}; font-weight: 700;">{sign}{delta_d:.1f}</td>
                    <td class="td-right" title="{base_nps_raw:,}" data-nps="{base_nps_raw}">{base_nps_compact}<span class="raw-nps" style="display:none;">{base_nps_raw}</span></td>
                    <td class="td-right" title="{var_nps_raw:,}" data-nps="{var_nps_raw}">{var_nps_compact}<span class="raw-nps" style="display:none;">{var_nps_raw}</span></td>
                    <td class="td-right" style="color: {nps_color}; font-weight: 700;">{nps_r:.3f}x</td>
                    <td class="td-right">{agree:.1f}%</td>
                    <td class="td-right"><span class="status-chip status-chip-{st_class} matrix-col-status matrix-status-{st_class}">{st_text}</span></td>
                  </tr>''')
    return "".join(rows)


def render_pool_modes_rows(pool_data: Optional[Dict[str, Any]], is_running: bool = False, run_type: str = "self") -> str:
    """Pre-render the 4 mode rows for Zoo Pool Gate."""
    pool = pool_data or {}
    raw_modes = pool.get("mode_results") or pool.get("modes") or pool.get("mode_stats") or {}
    mode_map = {}
    if isinstance(raw_modes, list):
        for item in raw_modes:
            if isinstance(item, dict) and item.get("mode"):
                mode_map[item["mode"].lower()] = item
    elif isinstance(raw_modes, dict):
        mode_map = {k.lower(): v for k, v in raw_modes.items()}

    base_modes = [
        {"mode": "constrictor", "name": "Constrictor 4P"},
        {"mode": "royale", "name": "Royale 4P"},
        {"mode": "duel", "name": "Duel 1v1"},
        {"mode": "standard", "name": "Standard 4P"},
    ]
    rows = []
    for bm in base_modes:
        mkey = bm["mode"]
        pm = mode_map.get(mkey) or {}
        cand_s = pm.get("cand_share")
        base_s = pm.get("base_share")
        delta = pm.get("delta")
        z = pm.get("z_score")
        games = pm.get("games", pm.get("cand_games", 0))

        if games > 0 and cand_s is not None and base_s is not None:
            cand_text = f"{float(cand_s):.1f}%"
            base_text = f"{float(base_s):.1f}%"
        else:
            cand_text = "--"
            base_text = "--"

        if games > 0 and delta is not None:
            d_val = float(delta)
            d_sign = "+" if d_val > 0 else ""
            d_col = "var(--pipeline-green)" if d_val > 0.5 else ("var(--pipeline-red)" if d_val < -0.5 else "var(--pipeline-amber)")
            d_text = f"{d_sign}{d_val:.1f}%"
        else:
            d_text = "--"
            d_col = "var(--pipeline-text-muted)"

        if games >= 5 and z is not None:
            z_val = float(z)
            z_text = f"{z_val:+.2f}"
            z_col = "var(--pipeline-green)" if z_val >= 1.645 else ("var(--pipeline-red)" if z_val <= -1.645 else "var(--pipeline-amber)")
        else:
            z_text = "--"
            z_col = "var(--pipeline-text-muted)"

        if games == 0:
            st_class = "queued"
            st_text = "QUEUED"
        else:
            z_num = float(z) if (z is not None and str(z).strip() != "--") else 0.0
            d_num = float(delta) if (delta is not None and str(delta).strip() != "--") else 0.0
            if d_num > 0 and z_num >= 1.645:
                st_class = "better"
                st_text = "BETTER"
            elif z_num <= -1.645 or d_num < -0.5:
                st_class = "worse"
                st_text = "WORSE"
            else:
                st_class = "equal"
                st_text = "EQUAL"

        games_text = f"{games} / {pm.get('max_games', 40)}" if games > 0 else "--"

        rows.append(f'''
                  <tr>
                    <td class="td-left" style="font-weight: 700; color: var(--pipeline-text-main);">{bm["name"]}</td>
                    <td class="td-right" style="font-weight: 700;">{games_text}</td>
                    <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">{cand_text}</td>
                    <td class="td-right" style="font-weight: 700; color: var(--pipeline-blue);">{base_text}</td>
                    <td class="td-right" style="font-weight: 700; color: {d_col};">{d_text}</td>
                    <td class="td-right" style="font-weight: 600; color: {z_col};">{z_text}</td>
                    <td class="td-right"><span class="status-chip status-chip-{st_class} matrix-col-status matrix-status-{st_class}">{st_text}</span></td>
                  </tr>''')
    return "".join(rows)


def calculate_run_progress_percent(completed_games: int, planned_games: int) -> float:
    """Percent = games completed / total planned games for the run."""
    if planned_games <= 0:
        return 0.0
    pct = (completed_games / planned_games) * 100.0
    return max(0.0, min(100.0, round(pct, 1)))


def calculate_time_progress_percent(
    elapsed_sec: float,
    eta_sec: Optional[float],
    is_finished: bool = False,
) -> float:
    """Calculates run progress percentage based on time:
    progress = elapsed / (elapsed + left) * 100.
    """
    if is_finished:
        return 100.0
    if elapsed_sec <= 0:
        return 0.0
    if eta_sec is None:
        return 0.0
    try:
        e = float(eta_sec)
    except (ValueError, TypeError):
        return 0.0
    if e <= 0:
        return 100.0
    total_time = elapsed_sec + e
    if total_time <= 0:
        return 0.0
    pct = (elapsed_sec / total_time) * 100.0
    return max(0.0, min(100.0, round(pct, 1)))


def calculate_suite_progress(
    run_type: str,
    matrix_progress: Optional[list],
    self_play_matrix: Optional[list] = None,
    depth_data: Optional[dict] = None,
    current_mode: str = "standard",
    current_played: int = 0,
    target_games: int = 200,
    is_running: bool = False,
    pool_enabled: bool = True,
) -> Tuple[int, int, float]:
    """Calculates cumulative completed games, planned games, and progress percentage
    for the entire A/B validation suite.
    """
    if run_type == "depth" and depth_data:
        planned = depth_data.get("total_positions", 12) or 12
        completed = depth_data.get("completed_positions", 0) or 0
        pct = calculate_run_progress_percent(completed, planned)
        return (completed, planned, pct)

    curr_mode_clean = str(current_mode).strip().lower()
    total_completed = 0
    total_planned = 0

    # 1. Stage 3 (Self-Play Matrix) games
    # If in pool run and self_play_matrix exists, add all self-play games
    if run_type == "pool" and self_play_matrix:
        for m in self_play_matrix:
            g_val = m.get("games") if m.get("games") is not None else (m.get("total_played") or 0)
            m_status = str(m.get("status") or "").upper()
            is_comp = (m_status in ("PASSED", "PARITY", "FAILED", "KEEP", "REVERT") and g_val > 0)
            mg = g_val if is_comp else (m.get("max_games") or 200)
            total_completed += g_val
            total_planned += mg
    elif run_type == "self" and matrix_progress:
        for m in matrix_progress:
            m_mode = str(m.get("mode") or "").strip().lower()
            is_curr = (is_running and m_mode == curr_mode_clean)
            g_val = current_played if is_curr else (m.get("games") if m.get("games") is not None else (m.get("total_played") or 0))
            m_status = str(m.get("status") or "").upper()
            is_comp = (m_status in ("PASSED", "PARITY", "FAILED", "KEEP", "REVERT") and g_val > 0)
            mg = g_val if is_comp else (m.get("max_games") or 200)
            total_completed += g_val
            total_planned += mg

    # 2. Stage 4 (Zoo Pool Gate) games
    if run_type == "pool":
        pool_matrix = matrix_progress or []
        if pool_matrix:
            for m in pool_matrix:
                m_mode = str(m.get("mode") or "").strip().lower()
                is_curr = (is_running and m_mode == curr_mode_clean)
                g_val = current_played if is_curr else (m.get("games") if m.get("games") is not None else (m.get("cand_games") or m.get("total_played") or 0))
                m_status = str(m.get("status") or "").upper()
                is_comp = (m_status in ("PASSED", "PARITY", "FAILED", "KEEP", "REVERT") and g_val > 0)
                mg = g_val if is_comp else (m.get("max_games") or 50)
                total_completed += g_val
                total_planned += mg
        else:
            total_completed += current_played
            total_planned += target_games
    elif run_type == "self":
        if pool_enabled:
            # Pool gate is queued to run after self-play
            # Pool planned games for each active mode in matrix_progress
            active_count = len(matrix_progress) if matrix_progress else 4
            total_planned += (active_count * 50)

    pct = calculate_run_progress_percent(total_completed, total_planned)
    return (total_completed, total_planned, pct)


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_history_date(raw: Any) -> str:
    """Format timestamp to 'Nov 2 07:15' format."""
    if not raw or raw == "-":
        return "--"
    try:
        if isinstance(raw, (int, float)):
            dt = datetime.datetime.fromtimestamp(raw)
        else:
            s = str(raw).strip().replace("T", " ")
            parts = s.split(".")[0]
            dt = datetime.datetime.strptime(parts[:19], "%Y-%m-%d %H:%M:%S")
        month_abbr = MONTH_NAMES[dt.month - 1] if 1 <= dt.month <= 12 else dt.strftime("%b")
        return f"{month_abbr} {dt.day} {dt.strftime('%H:%M')}"
    except Exception:
        return str(raw)[:12]


def format_decisive_mode_cell(source: str, share: float, delta: Optional[float]) -> str:
    """Format single-line mode cell with decisive source tag and delta color."""
    tag = f'<span class="hist-source-tag">{source}</span>'
    s_val = f"{share:.1f}%"
    d_span = ""
    if delta is not None:
        sign = "+" if delta > 0 else ""
        d_val = f"({sign}{delta:.1f}%)"
        if delta > 0.5:
            d_col = "var(--pipeline-green)"
        elif delta < -0.5:
            d_col = "var(--pipeline-red)"
        else:
            d_col = "var(--pipeline-text-muted)"
        d_span = f' <span class="hist-delta-val" style="color: {d_col};">{d_val}</span>'
    return f'{tag}<span class="hist-share-val">{s_val}</span>{d_span}'


def record_has_matrix_games(run: dict) -> bool:
    mr = run.get("matrix_results")
    if isinstance(mr, list) and len(mr) > 0:
        for it in mr:
            if isinstance(it, dict):
                g = it.get("total_played") if it.get("total_played") is not None else it.get("games")
                if g is not None and g > 0:
                    return True
    if run.get("total_games", 0) > 0 and run.get("variant"):
        return True
    return False


def record_has_pool_games(run: dict) -> bool:
    pd = run.get("pool_data") or run.get("pool_results") or run.get("pool_matrix")
    if not pd:
        return False
    if isinstance(pd, list):
        for it in pd:
            if isinstance(it, dict):
                g = it.get("games") if it.get("games") is not None else (it.get("cand_games") if it.get("cand_games") is not None else it.get("total_games"))
                if g is not None and g > 0:
                    return True
                if g is None and (it.get("cand_win_share") is not None or it.get("cand_share") is not None):
                    return True
    elif isinstance(pd, dict):
        if pd.get("total_games", 0) > 0:
            return True
        modes = pd.get("modes") if isinstance(pd.get("modes"), list) else (pd.get("mode_results") if isinstance(pd.get("mode_results"), list) else None)
        if modes:
            for it in modes:
                if isinstance(it, dict):
                    g = it.get("games") if it.get("games") is not None else it.get("cand_games")
                    if g is not None and g > 0:
                        return True
                    if g is None and (it.get("cand_win_share") is not None or it.get("cand_share") is not None):
                        return True
        mode_stats = pd.get("mode_stats")
        if isinstance(mode_stats, dict):
            for m, stats in mode_stats.items():
                if isinstance(stats, dict) and stats.get("games", 0) > 0:
                    return True
    return False


def derive_history_verdict(run: dict) -> Tuple[str, str]:
    """
    Derives (verdict_text, chip_class) for a run history entry.
    Verdict is strictly 'KEPT' or 'REVERTED'.
    Color class is 'better' (green), 'equal' (yellow), or 'worse' (red).
    """
    v_raw = str(run.get("verdict") or "").upper().strip()
    outcome_raw = str(run.get("outcome") or "").upper().strip()
    rec = str(run.get("recommendation") or run.get("stop_type") or "").upper().strip()

    # Determine verdict text: strictly KEPT or REVERTED
    if v_raw in ("KEPT", "KEEP") or (not v_raw and rec == "KEEP"):
        verdict = "KEPT"
    else:
        verdict = "REVERTED"

    # Determine color class: better (green), equal (yellow), worse (red)
    if outcome_raw in ("BETTER", "KEEP") or (not outcome_raw and rec == "KEEP"):
        chip_cls = "better"
    elif outcome_raw in ("EQUAL", "PARITY") or (not outcome_raw and rec == "PARITY"):
        chip_cls = "equal"
    else:
        chip_cls = "worse"

    return verdict, chip_cls


def format_history_date_stacked(raw: Any) -> str:
    if not raw or raw == "-":
        return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>'
    s = str(raw).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}:\d{2})(?::\d{2})?", s)
    if not m:
        return f'<div class="hist-date-main">{html.escape(s)}</div>'
    try:
        month_idx = int(m.group(2)) - 1
        month_str = MONTH_NAMES[month_idx] if 0 <= month_idx < 12 else m.group(2)
        day = int(m.group(3))
        time_part = m.group(4)
        return f'<div class="hist-date-main">{month_str} {day}</div><div class="hist-date-time">{time_part}</div>'
    except (ValueError, IndexError):
        return f'<div class="hist-date-main">{html.escape(s)}</div>'


def get_record_active_sources(run: dict) -> List[str]:
    sources = []
    if record_has_matrix_games(run):
        sources.append("SELF")
    if record_has_pool_games(run):
        sources.append("POOL")
    return sources


def format_history_src_cell(run: dict) -> str:
    sources = get_record_active_sources(run)
    if not sources:
        return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>'
    return "".join(f'<div class="hist-src-line">{s}</div>' for s in sources)


def format_mode_stat_line(share: float, delta: Optional[float]) -> str:
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        if delta > 0.5:
            d_col = "var(--pipeline-green)"
        elif delta < -0.5:
            d_col = "var(--pipeline-red)"
        else:
            d_col = "var(--pipeline-text-muted)"
        d_span = f' <span style="color: {d_col};">({sign}{delta:.1f}%)</span>'
    else:
        d_span = ""
    return f'<div class="hist-mode-line">{share:.1f}%{d_span}</div>'


def get_history_matrix_mode(run: dict, mode_key: str) -> Optional[Tuple[float, float]]:
    m_results = run.get("matrix_results")
    if isinstance(m_results, list):
        for it in m_results:
            if isinstance(it, dict) and it.get("mode") == mode_key:
                games = it.get("total_played") if it.get("total_played") is not None else it.get("games")
                if games is not None and games > 0:
                    ws = it.get("var_win_share") if it.get("var_win_share") is not None else it.get("win_share")
                    if ws is not None:
                        tgt = float(it.get("target_share") or (50.0 if mode_key == "duel" else 25.0))
                        delta = float(ws) - tgt
                        return float(ws), delta
    if run.get("game_mode") == mode_key and run.get("total_games", 0) > 0 and run.get("variant"):
        v = run["variant"]
        ws = v.get("win_share") if v.get("win_share") is not None else v.get("rate")
        if ws is not None:
            tgt = 50.0 if mode_key == "duel" else 25.0
            delta = float(ws) - tgt
            return float(ws), delta
    return None


def get_history_pool_mode(run: dict, mode_key: str) -> Optional[Tuple[float, float]]:
    p_data = run.get("pool_data") or run.get("pool_results") or run.get("pool_matrix")
    if p_data:
        p_item = None
        if isinstance(p_data, list):
            for it in p_data:
                if isinstance(it, dict) and (it.get("mode") == mode_key or it.get("name") == mode_key):
                    p_item = it
                    break
        elif isinstance(p_data, dict):
            if isinstance(p_data.get("mode_stats"), dict) and mode_key in p_data["mode_stats"]:
                p_item = p_data["mode_stats"][mode_key]
            elif isinstance(p_data.get("modes"), list):
                for it in p_data["modes"]:
                    if isinstance(it, dict) and (it.get("mode") == mode_key or it.get("name") == mode_key):
                        p_item = it
                        break
            elif mode_key in p_data and isinstance(p_data[mode_key], dict):
                p_item = p_data[mode_key]
        if p_item:
            games = p_item.get("games") if p_item.get("games") is not None else (p_item.get("cand_games") if p_item.get("cand_games") is not None else p_item.get("total_games"))
            cand_ws = p_item.get("cand_win_share") if p_item.get("cand_win_share") is not None else p_item.get("cand_share")
            if cand_ws is None:
                cand_ws = p_item.get("var_win_share")
            if games is None and cand_ws is not None:
                games = 1
            if games is not None and games > 0 and cand_ws is not None:
                base_ws = p_item.get("base_win_share") if p_item.get("base_win_share") is not None else p_item.get("base_share")
                delta = p_item.get("delta")
                if delta is None and base_ws is not None:
                    delta = float(cand_ws) - float(base_ws)
                return float(cand_ws), (float(delta) if delta is not None else 0.0)
    return None


def get_history_mode_cell(run: dict, mode_key: str) -> str:
    sources = get_record_active_sources(run)
    lines = []
    for s in sources:
        if s == "SELF":
            st = get_history_matrix_mode(run, mode_key)
            if st is not None:
                lines.append(format_mode_stat_line(st[0], st[1]))
        elif s == "POOL":
            st = get_history_pool_mode(run, mode_key)
            if st is not None:
                lines.append(format_mode_stat_line(st[0], st[1]))
    if not lines:
        return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>'
    return "".join(lines)


def format_history_tests(run: dict) -> str:
    rec = str(run.get("recommendation") or "").upper()
    u = run.get("unit_tests") or run.get("unit_tests_data")
    if isinstance(u, dict):
        p = u.get("passed")
        t = u.get("total")
        st = str(u.get("status") or "").upper()
        if p is not None and t is not None:
            col = "var(--pipeline-green)" if (p == t and st != "FAILED") else "var(--pipeline-red)"
            return f'<span style="color: {col}; font-weight: 600;">{p}/{t}</span>'
        if st == "PASSED":
            return '<span style="color: var(--pipeline-green); font-weight: 600;">PASS</span>'
        if st == "FAILED":
            return '<span style="color: var(--pipeline-red); font-weight: 600;">FAIL</span>'
    if rec == "TEST_FAILURE":
        return '<span style="color: var(--pipeline-red); font-weight: 600;">FAIL</span>'
    return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>'


def format_history_depth(run: dict) -> str:
    d = run.get("depth_results") or run.get("depth_data")
    if isinstance(d, dict):
        ov = d.get("overall") if isinstance(d.get("overall"), dict) else d
        delta_d = ov.get("delta_depth") if ov.get("delta_depth") is not None else ov.get("total_delta_depth")
        nps_r = ov.get("nps_ratio")
        if delta_d is not None or nps_r is not None:
            d_num = float(delta_d or 0.0)
            sign = "+" if d_num >= 0 else ""
            d_str = f"{sign}{d_num:.2f}d"
            if d_num > 0.05:
                col = "var(--pipeline-green)"
            elif d_num < -0.05:
                col = "var(--pipeline-red)"
            else:
                col = "var(--pipeline-text-main)"
            nps_num = float(nps_r if nps_r is not None else 1.0)
            nps_str = f"{nps_num:.2f}x NPS"
            return f'<div class="hist-depth-delta" style="color: {col}; font-weight: 600;">{d_str}</div><div class="hist-depth-nps">{nps_str}</div>'
    return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>'


def render_history_row(run: dict, idx: int) -> str:
    entry_cls = "entry-even" if (idx % 2 == 0) else "entry-odd"
    date_html = format_history_date_stacked(run.get("timestamp"))
    raw_desc = run.get("experiment_desc") or run.get("description") or "Unspecified Experiment"
    exp_escaped = html.escape(str(raw_desc))
    tests_html = format_history_tests(run)
    depth_html = format_history_depth(run)
    src_html = format_history_src_cell(run)
    const_html = get_history_mode_cell(run, "constrictor")
    royale_html = get_history_mode_cell(run, "royale")
    duel_html = get_history_mode_cell(run, "duel")
    standard_html = get_history_mode_cell(run, "standard")
    v_text, v_chip = derive_history_verdict(run)
    verdict_badge = f'<span class="status-chip status-chip-{v_chip}">{v_text}</span>'

    return f'''        <tr class="history-row {entry_cls}" data-entry="{idx}">
          <td class="td-left">{date_html}</td>
          <td class="td-left cell-experiment"><span class="hist-exp-text" title="{exp_escaped}">{exp_escaped}</span></td>
          <td class="td-center">{tests_html}</td>
          <td class="td-center">{depth_html}</td>
          <td class="td-center">{src_html}</td>
          <td class="td-right">{const_html}</td>
          <td class="td-right">{royale_html}</td>
          <td class="td-right">{duel_html}</td>
          <td class="td-right">{standard_html}</td>
          <td class="td-right">{verdict_badge}</td>
        </tr>'''


def render_history_rows(history_list: list) -> str:
    if not history_list:
        return '<tr class="empty-row"><td colspan="10" class="empty-cell" style="text-align: center; color: var(--pipeline-text-muted); padding: 3rem;">No runs yet</td></tr>'
    all_rev = list(reversed(history_list))
    return "\n".join(render_history_row(run, idx) for idx, run in enumerate(all_rev))


def format_history_time(raw: Any) -> str:
    """Shorten timestamp to short form month, day, and time with no year.
    Example: '2026-10-21 06:05:04' -> 'Oct 21 06:05:04'
    """
    if not raw or raw == "-":
        return "-"
    s = str(raw).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](.+)$", s)
    if not m:
        return s
    try:
        month_idx = int(m.group(2)) - 1
        month_str = MONTH_NAMES[month_idx] if 0 <= month_idx < 12 else m.group(2)
        day = int(m.group(3))
        time_part = m.group(4)
        time_part = re.sub(r"^0(\d:\d{2}:\d{2})", r"\1", time_part)
        return f"{month_str} {day} {time_part}"
    except (ValueError, IndexError):
        return s


DEFAULT_UNIT_TEST_NAMES: List[str] = [
    "test_avoid_dead_end_when_boxed_in_corner",
    "test_avoid_risky_head_kill_chase",
    "test_avoid_running_into_opponent_if_open_space",
    "test_c_engine_loads_without_compiler_if_binary_exists",
    "test_c_engine_mandatory_fail_fast",
    "test_concurrent_move_requests_under_timeout_cap",
    "test_concurrent_thread_safety_no_segfault",
    "test_constrictor_7x7_turn_11_must_take_right_for_tie",
    "test_constrictor_turn_22_must_take_right",
    "test_constrictor_turn_30_must_take_down",
    "test_dont_run_into_tail_if_constrictor",
    "test_dont_run_into_tail_if_just_ate",
    "test_duel_turn_172_avoid_wall_pocket_tunnel_left",
    "test_duel_turn_208_avoid_dead_end_pocket_suicide",
    "test_duel_turn_78_must_keep_opponent_cornered",
    "test_duel_turn_79_avoid_corner_trap_wall_collision",
    "test_execute_adjacent_head_kill_on_smaller_opponent",
    "test_failure_case_turn_45_avoid_contested_head_collision",
    "test_game_de138756_turn_106_avoid_suicidal_head_threat_override",
    "test_multiplayer_turn_108_avoid_opponent_body_suicide",
    "test_multiplayer_turn_10_avoid_non_receding_tail_collision",
    "test_multiplayer_turn_116_avoid_flibba_head_to_head_trap",
    "test_multiplayer_turn_119_avoid_wall_down",
    "test_multiplayer_turn_194_pocket_trap_follow_tail",
    "test_multiplayer_turn_29_avoid_wall_left",
    "test_multiplayer_turn_31_avoid_wall_collision",
    "test_multiplayer_turn_321_cornered_down_into_tail",
    "test_multiplayer_turn_61_avoid_wall_and_opponent_body_suicide",
    "test_multiplayer_turn_61_avoid_wall_corner_pocket_trap",
    "test_multiplayer_turn_63_avoid_self_collision",
    "test_multiplayer_turn_85_avoid_wall_up",
    "test_multiplayer_turn_97_avoid_opponent_body_suicide",
    "test_never_run_into_wall",
    "test_never_run_into_yourself",
    "test_prefer_opponent_over_wall_or_self",
    "test_royale_danger_zone_low_health_must_seek_food",
    "test_trap_avoidance_dead_end_pocket",
    "test_urgent_food_eating_on_critical_health",
    "test_wrap_mode_boundary_escape",
    "test_wrap_mode_flood_fill_modulo",
]

_DEFAULT_UNIT_TESTS: Optional[List[str]] = None


def get_default_unit_tests() -> List[str]:
    global _DEFAULT_UNIT_TESTS
    if _DEFAULT_UNIT_TESTS is not None:
        return _DEFAULT_UNIT_TESTS
    _DEFAULT_UNIT_TESTS = list(DEFAULT_UNIT_TEST_NAMES)
    return _DEFAULT_UNIT_TESTS


def render_unit_tests_html(tests: List[Dict[str, Any]]) -> str:
    if not tests:
        default_names = get_default_unit_tests()
        if not default_names:
            return '<tr class="empty-row"><td colspan="3" class="empty-cell">No individual test records available.</td></tr>'
        rows = []
        for idx, name in enumerate(default_names, 1):
            chip_html = '<span class="status-chip status-chip-queued matrix-col-status matrix-status-queued">QUEUED</span>'
            rows.append(f'''        <tr>
          <td class="td-right" style="color: var(--pipeline-text-muted);">{idx}.</td>
          <td class="td-left" style="font-weight: 600; color: var(--pipeline-text-main);">{html.escape(name)}</td>
          <td class="td-right">{chip_html}</td>
        </tr>''')
        return "\n".join(rows)
    sorted_tests = sorted(
        tests,
        key=lambda t: (0 if str(t.get("status", "")).upper() in ("FAILED", "ERROR") else 1, t.get("name", ""))
    )
    rows = []
    for idx, t in enumerate(sorted_tests, 1):
        status_val = str(t.get("status", "")).upper()
        is_failed = status_val in ("FAILED", "ERROR")
        is_queued = status_val in ("QUEUED", "PENDING", "")
        if is_failed:
            st_text = "WORSE"
            st_class = "worse"
        elif is_queued:
            st_text = "QUEUED"
            st_class = "queued"
        else:
            st_text = "EQUAL"
            st_class = "equal"
        chip_html = f'<span class="status-chip status-chip-{st_class} matrix-col-status matrix-status-{st_class}">{st_text}</span>'
        name = html.escape(str(t.get("name", "unnamed_test")))
        msg = t.get("message", "")
        fail_row = f'<tr class="test-fail-row"><td colspan="3"><div class="unit-test-failure-msg">{html.escape(str(msg))}</div></td></tr>' if (is_failed and msg) else ""
        row_class = ' class="row-failed"' if is_failed else ""
        rows.append(f'''        <tr{row_class}>
          <td class="td-right" style="color: var(--pipeline-text-muted);">{idx}.</td>
          <td class="td-left" style="font-weight: 600; color: var(--pipeline-text-main);">{name}</td>
          <td class="td-right">{chip_html}</td>
        </tr>
        {fail_row}''')
    return "\n".join(rows)


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
        self.target: Optional[str] = None
        self.pool_enabled: bool = False

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
        self.matrix_duration_sec: Optional[float] = None
        self.pool_duration_sec: Optional[float] = None
        self.self_play_matrix: Optional[List[Dict[str, Any]]] = None
        self.prior_suite_elapsed: Optional[float] = None
        self.pool_start_time: Optional[float] = None
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
                new_run_type = payload.get("run_type", getattr(self, "run_type", "self"))
                new_desc = payload.get("experiment_desc", "Unspecified Experiment")
                is_same_experiment = (getattr(self, "_last_desc", None) == new_desc)

                if getattr(self, "run_type", "self") == "self" and new_run_type == "pool":
                    # Transitioning from self-play to pool: freeze self-play duration and matrix progress!
                    if getattr(self, "matrix_duration_sec", None) is None and self.start_time:
                        prev_elapsed = (self.end_time or time.time()) - (self.suite_start_time or self.start_time)
                        unit_dur = float((getattr(self, "unit_tests_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                        depth_dur = float((getattr(self, "depth_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                        self.matrix_duration_sec = max(0.0, round(prev_elapsed - unit_dur - depth_dur, 1))
                        self.prior_suite_elapsed = prev_elapsed
                    if self.matrix_progress:
                        self.self_play_matrix = list(self.matrix_progress)
                    if getattr(self, "pool_start_time", None) is None:
                        self.pool_start_time = time.time()
                elif not is_same_experiment:
                    self.matrix_duration_sec = None
                    self.pool_duration_sec = None
                    self.self_play_matrix = None
                    self.prior_suite_elapsed = None
                    self.pool_start_time = None

                self.run_type = new_run_type
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
                self.experiment_desc = new_desc
                target_val = payload.get("target")
                if not target_val:
                    raise ValueError("Missing mandatory parameter: 'target'")
                self.target = str(target_val).strip().upper()
                self.pool_enabled = bool(payload.get("pool_enabled", new_run_type == "pool"))
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
                if not getattr(self, "pool_data", None) or not is_same_experiment:
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
                        "modes": {},
                    }
                if not getattr(self, "depth_data", None) or (not is_same_experiment and not self.depth_data.get("completed_positions") and not self.depth_data.get("duration_sec")):
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
                if new_run_type == "pool" and is_same_experiment and getattr(self, "matrix_duration_sec", None) is not None:
                    prior_dur = float(getattr(self, "prior_suite_elapsed", 0.0) or 0.0)
                    if prior_dur <= 0.0:
                        unit_dur = float((getattr(self, "unit_tests_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                        depth_dur = float((getattr(self, "depth_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                        prior_dur = unit_dur + depth_dur + float(self.matrix_duration_sec or 0.0)
                    pool_start = getattr(self, "pool_start_time", None) or self.start_time
                    pool_el = max(0.0, time.time() - pool_start)
                    self.suite_start_time = time.time() - (prior_dur + pool_el)
                elif explicit_suite:
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
                self.total_played = 0
                self.wins_baseline = 0
                self.losses_baseline = 0
                self.draws_baseline = 0
                self.wins_variant = 0
                self.losses_variant = 0
                self.draws_variant = 0
                if self.matrix_progress and isinstance(self.matrix_progress, list):
                    for item in self.matrix_progress:
                        item["status"] = "PENDING"
                        item["games"] = 0
                        item["win_share"] = 0.0
                        item["decision"] = "PENDING"
                        item["reason"] = ""
                        item["z_score"] = None
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
                games = payload.get("games")
                max_games = payload.get("max_games")
                cand_games = payload.get("cand_games", games)
                base_games = payload.get("base_games", games)
                dec = payload.get("decision", "PARITY")
                status = "PASSED" if dec == "KEEP" else ("PARITY" if dec == "PARITY" else "FAILED")
                c_sh = round(float(payload.get("cand_share", 0.0)), 1)
                b_sh = round(float(payload.get("base_share", 0.0)), 1)
                d_val = round(float(payload.get("delta", 0.0)), 1)
                z_val = round(float(payload.get("z_score", 0.0)), 2)
                dur = payload.get("duration_sec")

                if self.matrix_progress:
                    for item in self.matrix_progress:
                        if item.get("mode") == m_mode:
                            item["status"] = status
                            item["cand_share"] = c_sh
                            item["base_share"] = b_sh
                            item["delta"] = d_val
                            item["z_score"] = z_val
                            item["decision"] = dec
                            if games is not None:
                                item["games"] = games
                            if max_games is not None:
                                item["max_games"] = max_games
                            if cand_games is not None:
                                item["cand_games"] = cand_games
                            if base_games is not None:
                                item["base_games"] = base_games
                            if dur is not None:
                                item["duration_sec"] = dur
                modes_dict = self.pool_data.setdefault("modes", {})
                mode_entry = {
                    "cand_share": c_sh,
                    "base_share": b_sh,
                    "delta": d_val,
                    "z_score": z_val,
                    "decision": dec,
                    "status": status,
                }
                if games is not None:
                    mode_entry["games"] = games
                if max_games is not None:
                    mode_entry["max_games"] = max_games
                if cand_games is not None:
                    mode_entry["cand_games"] = cand_games
                if base_games is not None:
                    mode_entry["base_games"] = base_games
                if dur is not None:
                    mode_entry["duration_sec"] = dur
                modes_dict[m_mode] = mode_entry
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
                    "title": payload.get("title", ""),
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
                delta_depths = [f.get("delta_depth", round(f["var_med_depth"] - f["base_med_depth"], 1)) for f in f_list]
                nps_ratios = [f["nps_ratio"] for f in f_list]
                agrees = [f["agreement_pct"] for f in f_list]
                if var_depths and base_depths:
                    import statistics
                    med_v = statistics.median(var_depths)
                    med_b = statistics.median(base_depths)
                    self.depth_data["median_var_depth"] = round(med_v, 1)
                    self.depth_data["median_base_depth"] = round(med_b, 1)
                    self.depth_data["delta_depth"] = round(med_v - med_b, 1)
                self.depth_data["total_delta_depth"] = round(sum(delta_depths), 1)
                if nps_ratios:
                    import statistics
                    self.depth_data["nps_ratio"] = round(statistics.median(nps_ratios), 2)
                if agrees:
                    import statistics
                    self.depth_data["agreement_pct"] = round(statistics.mean(agrees), 1)
                self.depth_data["passed"] = all(f.get("passed", True) for f in f_list)
            elif action == "finish_depth":
                self.depth_data["completed_positions"] = payload.get("completed_positions", self.depth_data.get("completed_positions", 12))
                self.depth_data["total_positions"] = payload.get("total_positions", self.depth_data.get("total_positions", 12))
                self.depth_data["median_base_depth"] = round(float(payload.get("overall_base_d", 0.0)), 1)
                self.depth_data["median_var_depth"] = round(float(payload.get("overall_var_d", 0.0)), 1)
                self.depth_data["delta_depth"] = round(float(payload.get("overall_delta_d", 0.0)), 1)
                f_list = self.depth_data.get("fixtures", [])
                if f_list:
                    tot_d = sum(f.get("delta_depth", round(f.get("var_med_depth", 0.0) - f.get("base_med_depth", 0.0), 1)) for f in f_list)
                    self.depth_data["total_delta_depth"] = round(tot_d, 1)
                elif "total_delta_depth" in payload:
                    self.depth_data["total_delta_depth"] = round(float(payload["total_delta_depth"]), 1)
                else:
                    self.depth_data["total_delta_depth"] = round(float(payload.get("overall_delta_d", 0.0)), 1)
                self.depth_data["nps_ratio"] = round(float(payload.get("overall_nps_r", 1.0)), 2)
                self.depth_data["agreement_pct"] = round(float(payload.get("overall_agree", 100.0)), 1)
                self.depth_data["passed"] = bool(payload.get("overall_pass", True))
                if "duration_sec" in payload:
                    self.depth_data["duration_sec"] = round(float(payload["duration_sec"]), 1)
                if "mode_summaries" in payload:
                    self.depth_data["mode_summaries"] = payload["mode_summaries"]
            elif action == "update_unit_tests":
                unit_st = payload.get("status", "PASSED")
                if unit_st == "RUNNING" and self.status == "IDLE":
                    self.status = "RUNNING"
                    self.run_type = "self"
                    self.start_time = time.time()
                    if self.suite_start_time is None:
                        self.suite_start_time = self.start_time
                tests_list = payload.get("tests")
                if not tests_list:
                    import sys
                    mod = sys.modules.get("tests.test_strategy")
                    if mod and hasattr(mod, "LAST_TEST_RECORDS") and getattr(mod, "LAST_TEST_RECORDS"):
                        tests_list = getattr(mod, "LAST_TEST_RECORDS")
                if not tests_list:
                    tests_list = []
                tot = len(tests_list) if tests_list else payload.get("total", 0)
                pas = sum(1 for t in tests_list if str(t.get("status", "")).upper() == "PASSED") if tests_list else payload.get("passed", tot)
                self.unit_tests_data = {
                    "status": payload.get("status", "PASSED"),
                    "passed": pas,
                    "total": tot,
                    "details": payload.get("details", ""),
                    "duration_sec": float(payload.get("duration_sec", 0.0)),
                    "tests": tests_list or [],
                }
            elif action == "restore":
                d = payload.get("data", {})
                for attr in [
                    "status", "stop_type", "stop_reason", "total_played", "target_games",
                    "min_games", "max_games", "check_interval", "early_stop_high",
                    "early_stop_low", "threads", "strategy_baseline", "strategy_variant",
                    "experiment_desc", "target", "recommendation", "csv_path",
                    "game_mode", "num_snakes", "target_win_share", "matrix_progress", "active_pid",
                    "run_type", "matrix_duration_sec", "pool_duration_sec", "self_play_matrix",
                    "prior_suite_elapsed", "pool_start_time"
                ]:
                    if attr in d:
                        setattr(self, attr, d[attr])
                if "suite_elapsed_seconds" in d:
                    self.suite_start_time = time.time() - float(d["suite_elapsed_seconds"])
                elif "elapsed_seconds" in d:
                    self.suite_start_time = time.time() - float(d["elapsed_seconds"])

                if "mode_elapsed_seconds" in d:
                    self.start_time = time.time() - float(d["mode_elapsed_seconds"])
                elif "elapsed_seconds" in d:
                    self.start_time = time.time() - float(d["elapsed_seconds"])

                if "unit_tests_data" in d and isinstance(d["unit_tests_data"], dict):
                    self.unit_tests_data = d["unit_tests_data"]
                if "depth_data" in d and isinstance(d["depth_data"], dict):
                    self.depth_data = d["depth_data"]
                if "pool_data" in d and d["pool_data"] is not None:
                    if isinstance(d["pool_data"], dict):
                        self.pool_data = d["pool_data"]
                    elif isinstance(d["pool_data"], list):
                        self.pool_data = {
                            "modes": {
                                item["mode"].lower(): item
                                for item in d["pool_data"]
                                if isinstance(item, dict) and item.get("mode")
                            },
                            "mode_results": d["pool_data"],
                        }

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
                self.active_pid = None
                dur = payload.get("duration_sec")
                if dur is None and self.start_time:
                    dur = max(0.0, self.end_time - (self.suite_start_time or self.start_time))
                else:
                    dur = float(dur) if dur is not None else 0.0

                unit_dur = float((getattr(self, "unit_tests_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                depth_dur = float((getattr(self, "depth_data", {}) or {}).get("duration_sec", 0.0) or 0.0)

                if getattr(self, "run_type", "self") == "self":
                    self.matrix_duration_sec = max(0.0, round(dur - unit_dur - depth_dur, 1)) if dur > 0 else 0.0
                    self.self_play_matrix = list(self.matrix_progress or [])
                elif getattr(self, "run_type", "self") == "pool":
                    if dur > 0:
                        pool_dur = max(0.0, round(dur - unit_dur - depth_dur, 1))
                    elif getattr(self, "pool_start_time", None) and self.end_time:
                        pool_dur = max(0.0, round(self.end_time - self.pool_start_time, 1))
                    else:
                        pool_dur = 0.0
                    self.pool_duration_sec = pool_dur
                    if not getattr(self, "pool_data", None) or not isinstance(self.pool_data, dict):
                        self.pool_data = {}
                    self.pool_data["duration_sec"] = self.pool_duration_sec
                    p_modes = payload.get("pool_modes")
                    if p_modes:
                        self.pool_modes = p_modes
                        self.pool_data["mode_results"] = p_modes
                        modes_dict = self.pool_data.setdefault("modes", {})
                        for pm in p_modes:
                            if isinstance(pm, dict) and pm.get("mode"):
                                modes_dict[pm["mode"].lower()] = dict(pm)
                    elif self.matrix_progress:
                        modes_dict = self.pool_data.setdefault("modes", {})
                        for mm in self.matrix_progress:
                            if isinstance(mm, dict) and mm.get("mode"):
                                m_key = mm["mode"].lower()
                                if m_key not in modes_dict:
                                    modes_dict[m_key] = dict(mm)
                                else:
                                    for k, v in mm.items():
                                        if k not in modes_dict[m_key] or modes_dict[m_key][k] is None:
                                            modes_dict[m_key][k] = v
                    if payload.get("opponents"):
                        self.pool_data["opponents"] = payload.get("opponents")

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

    def set_finished(
        self,
        stop_type: str,
        stop_reason: str,
        recommendation: str,
        csv_path: Any,
        duration_sec: Optional[float] = None,
        pool_modes: Optional[List[Dict[str, Any]]] = None,
        opponents: Optional[List[Dict[str, Any]]] = None,
    ):
        with self.lock:
            self.last_activity_time = time.time()
            self.status = "FINISHED" if stop_type != "TEST_FAILURE" else "TEST_FAILURE"
            self.stop_type = stop_type
            self.stop_reason = stop_reason
            self.recommendation = recommendation
            self.csv_path = str(csv_path)
            self.end_time = time.time()
            self.active_pid = None
            dur = duration_sec
            if dur is None and self.start_time:
                dur = max(0.0, self.end_time - (self.suite_start_time or self.start_time))
            else:
                dur = float(dur) if dur is not None else 0.0

            unit_dur = float((getattr(self, "unit_tests_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
            depth_dur = float((getattr(self, "depth_data", {}) or {}).get("duration_sec", 0.0) or 0.0)

            if getattr(self, "run_type", "self") == "self":
                self.matrix_duration_sec = max(0.0, round(dur - unit_dur - depth_dur, 1)) if dur > 0 else 0.0
                self.self_play_matrix = list(self.matrix_progress or [])
            elif getattr(self, "run_type", "self") == "pool":
                self.pool_duration_sec = max(0.0, round(dur - unit_dur - depth_dur, 1)) if dur > 0 else 0.0
                if not getattr(self, "pool_data", None) or not isinstance(self.pool_data, dict):
                    self.pool_data = {}
                self.pool_data["duration_sec"] = self.pool_duration_sec
                if pool_modes:
                    self.pool_modes = pool_modes
                    self.pool_data["mode_results"] = pool_modes
                    modes_dict = self.pool_data.setdefault("modes", {})
                    for pm in pool_modes:
                        if isinstance(pm, dict) and pm.get("mode"):
                            modes_dict[pm["mode"].lower()] = dict(pm)
                elif self.matrix_progress:
                    modes_dict = self.pool_data.setdefault("modes", {})
                    for mm in self.matrix_progress:
                        if isinstance(mm, dict) and mm.get("mode"):
                            m_key = mm["mode"].lower()
                            if m_key not in modes_dict:
                                modes_dict[m_key] = dict(mm)
                            else:
                                for k, v in mm.items():
                                    if k not in modes_dict[m_key] or modes_dict[m_key][k] is None:
                                        modes_dict[m_key][k] = v
                if opponents:
                    self.pool_data["opponents"] = opponents

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
                    if not getattr(self, "target", None):
                        try:
                            cmd_bytes = Path(f"/proc/{self.active_pid}/cmdline").read_bytes().split(b"\0")
                            cmd_args = [a.decode("utf-8", errors="ignore") for a in cmd_bytes if a]
                            for i, arg in enumerate(cmd_args):
                                if arg == "--target" and i + 1 < len(cmd_args):
                                    self.target = cmd_args[i + 1].strip().upper()
                                    break
                        except Exception:
                            pass
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
                matrix_elapsed = 0.0
                pool_elapsed = 0.0
                eta_info = {"total_eta_seconds": None, "has_history": False, "is_stable": False, "breakdown": []}
            else:
                now = self.end_time or time.time()
                unit_dur = float((getattr(self, "unit_tests_data", {}) or {}).get("duration_sec", 0.0) or 0.0)
                depth_dur = float((getattr(self, "depth_data", {}) or {}).get("duration_sec", 0.0) or 0.0)

                if getattr(self, "run_type", "self") == "pool" and getattr(self, "matrix_duration_sec", None) is not None:
                    prior_dur = unit_dur + depth_dur + float(self.matrix_duration_sec)
                    if getattr(self, "pool_duration_sec", None) is not None:
                        pool_elapsed = float(self.pool_duration_sec)
                    else:
                        pool_start = getattr(self, "pool_start_time", None) or self.start_time or now
                        pool_elapsed = max(0.0, now - pool_start)
                    suite_elapsed = prior_dur + pool_elapsed
                    mode_start = self.start_time or now
                    mode_elapsed = max(0.0, now - mode_start)
                    matrix_elapsed = float(self.matrix_duration_sec)
                else:
                    suite_start = getattr(self, "suite_start_time", None) or self.start_time or now
                    if suite_start < 100_000_000 or (now - suite_start) > 30 * 86400:
                        suite_start = self.start_time or now
                    suite_elapsed = max(0.0, now - suite_start)
                    mode_start = self.start_time or now
                    mode_elapsed = max(0.0, now - mode_start)
                    if getattr(self, "matrix_duration_sec", None) is not None:
                        matrix_elapsed = float(self.matrix_duration_sec)
                    else:
                        matrix_elapsed = max(0.0, suite_elapsed - unit_dur - depth_dur)
                    pool_elapsed = float(self.pool_duration_sec) if getattr(self, "pool_duration_sec", None) is not None else 0.0

                if self.status == "RUNNING" and (self.total_played > 0 or self.active_pid is not None or getattr(self, "run_type", "self") in ("depth", "self", "unit", "pool")):
                    eta_info = calculate_eta(
                        matrix_progress=live_matrix_progress if live_matrix_progress else self.matrix_progress,
                        current_mode=self.game_mode,
                        current_played=played,
                        current_max_games=self.target_games or self.max_games or 200,
                        current_mode_elapsed=mode_elapsed,
                        run_type=getattr(self, "run_type", "self"),
                        depth_data=getattr(self, "depth_data", None),
                        unit_tests_data=getattr(self, "unit_tests_data", None),
                        pool_data=getattr(self, "pool_data", None),
                        include_pool_stage=(bool(getattr(self, "pool_enabled", False)) and getattr(self, "run_type", "self") in ("self", "unit", "depth")),
                    )
                else:
                    eta_info = {"total_eta_seconds": None, "has_history": False, "is_stable": False, "breakdown": []}

            # Cumulative suite progress across all games & stages
            tot_completed, tot_planned, suite_games_pct = calculate_suite_progress(
                run_type=getattr(self, "run_type", "self"),
                matrix_progress=live_matrix_progress if live_matrix_progress else self.matrix_progress,
                self_play_matrix=getattr(self, "self_play_matrix", None),
                depth_data=getattr(self, "depth_data", None),
                current_mode=self.game_mode,
                current_played=played,
                target_games=self.target_games or self.max_games or 200,
                is_running=(self.status == "RUNNING"),
                pool_enabled=bool(getattr(self, "pool_enabled", False)),
            )

            is_finished = (self.status in ("FINISHED", "TEST_FAILURE") or self.stop_type in ("MATRIX_COMPLETE", "PARITY", "KEEP"))
            time_eta = eta_info.get("total_eta_seconds")
            if is_finished:
                suite_progress_pct = 100.0
            elif self.status == "RUNNING" and time_eta is not None and time_eta > 0 and suite_elapsed > 0:
                suite_progress_pct = calculate_time_progress_percent(suite_elapsed, time_eta, is_finished=False)
            else:
                suite_progress_pct = suite_games_pct

            unit_d = getattr(self, "unit_tests_data", {"status": "PENDING", "passed": 0, "total": 0, "details": ""})
            if isinstance(unit_d, dict):
                unit_d_copy = dict(unit_d)
                if not unit_d_copy.get("tests"):
                    unit_d_copy["tests"] = []
            else:
                unit_d_copy = unit_d

            pool_d = getattr(self, "pool_data", None)
            if pool_d and isinstance(pool_d, dict):
                pool_d = dict(pool_d)
                if getattr(self, "pool_duration_sec", None) is not None:
                    pool_d["duration_sec"] = float(self.pool_duration_sec)
                if not pool_d.get("modes") and self.matrix_progress and getattr(self, "run_type", "self") == "pool":
                    pool_d["modes"] = {
                        item["mode"].lower(): dict(item)
                        for item in self.matrix_progress
                        if isinstance(item, dict) and item.get("mode")
                    }

            depth_d = getattr(self, "depth_data", None)
            if depth_d and isinstance(depth_d, dict):
                depth_d_copy = dict(depth_d)
                f_list = depth_d_copy.get("fixtures") or []
                if "total_delta_depth" not in depth_d_copy:
                    if f_list:
                        tot_d = sum(f.get("delta_depth", float(f.get("var_med_depth", 0.0)) - float(f.get("base_med_depth", 0.0))) for f in f_list)
                        depth_d_copy["total_delta_depth"] = round(tot_d, 1)
                    elif depth_d_copy.get("delta_depth") is not None:
                        depth_d_copy["total_delta_depth"] = round(float(depth_d_copy["delta_depth"]), 1)
            else:
                depth_d_copy = depth_d

            res = {
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
                "matrix_elapsed_seconds": round(matrix_elapsed, 1),
                "pool_elapsed_seconds": round(pool_elapsed, 1),
                "matrix_duration_sec": getattr(self, "matrix_duration_sec", None),
                "pool_duration_sec": getattr(self, "pool_duration_sec", None),
                "self_play_matrix": getattr(self, "self_play_matrix", None),
                "progress_percent": suite_progress_pct,
                "games_progress_percent": suite_games_pct,
                "total_planned_games": tot_planned,
                "total_completed_games": tot_completed,
                "eta_seconds": eta_info.get("total_eta_seconds"),
                "eta_breakdown": eta_info.get("breakdown", []),
                "run_type": getattr(self, "run_type", "self"),
                "pool_data": pool_d,
                "depth_data": depth_d_copy,
                "unit_tests_data": unit_d_copy,
                "snakes_stats": self.snakes_stats,
                "run_history": load_run_history(),
                "experiment_desc": self.experiment_desc,
                "target": getattr(self, "target", None),
                "pool_enabled": getattr(self, "pool_enabled", getattr(self, "run_type", "self") == "pool"),
                "serving_stats": compute_serving_stats(),
            }

            now = time.time()
            if (self.status == "RUNNING" or getattr(self, "total_played", 0) > 0) and (not hasattr(self, "_last_disk_dump") or (now - self._last_disk_dump > 2.0)):
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

      /* ── Validation Pipeline Design Tokens ───────────────────────────────── */
      /* Colors */
      --pipeline-bg: rgba(0, 0, 0, 0.25);
      --pipeline-panel-bg: rgba(18, 24, 38, 0.65);
      --pipeline-border: rgba(255, 255, 255, 0.08);
      --pipeline-border-subtle: rgba(255, 255, 255, 0.05);
      --pipeline-border-header: rgba(255, 255, 255, 0.08);
      --pipeline-border-row: rgba(255, 255, 255, 0.04);
      --pipeline-strip-bg: rgba(0, 0, 0, 0.35);
      --pipeline-th-bg: rgba(15, 20, 32, 0.95);
      --pipeline-row-hover: rgba(255, 255, 255, 0.035);
      --pipeline-row-active: rgba(59, 130, 246, 0.08);

      --pipeline-text-main: #f3f4f6;
      --pipeline-text-muted: #9ca3af;

      /* Semantic Colors (Strict: ONE blue only!) */
      --pipeline-green: #10b981;  /* Success / Passed / Good */
      --pipeline-red: #ef4444;    /* Danger / Failed / Bad */
      --pipeline-amber: #f59e0b;  /* Warning / Parity / Near threshold */
      --pipeline-blue: #3b82f6;   /* ONE BLUE: Running / Info / Active */
      --pipeline-grey: #9ca3af;   /* Neutral / Pending / Empty / Skipped */

      /* Spacing Scale */
      --pipeline-space-xs: 0.3rem;
      --pipeline-space-sm: 0.5rem;
      --pipeline-space-md: 0.75rem;
      --pipeline-space-lg: 1.0rem;
      --pipeline-space-xl: 1.25rem;

      /* Radii */
      --pipeline-radius-sm: 0.35rem;
      --pipeline-radius-md: 0.5rem;
      --pipeline-radius-lg: 0.75rem;
      --pipeline-radius-pill: 9999px;

      /* Typography */
      --pipeline-font-title: 1.05rem;
      --pipeline-font-sub: 0.88rem;
      --pipeline-font-meta: 0.82rem;
      --pipeline-font-body: 0.86rem;
      --pipeline-font-sm: 0.72rem;
      --pipeline-font-chip: 0.68rem;
      --pipeline-font-mono: 'JetBrains Mono', monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Outfit', sans-serif;
      background-color: #0a0d14;
      background-color: var(--bg-dark);
      background-image: 
        radial-gradient(at 15% 15%, rgba(59, 130, 246, 0.12) 0px, transparent 45%),
        radial-gradient(at 85% 85%, rgba(16, 185, 129, 0.12) 0px, transparent 45%);
      background-attachment: fixed;
      color: #f3f4f6;
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
      flex: 1;
    }
    .logo-icon {
      font-size: 2.2rem;
      filter: drop-shadow(0 0 12px rgba(16, 185, 129, 0.5));
      flex-shrink: 0;
    }
    .logo-text {
      min-width: 0;
      flex: 1;
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
      max-width: 100%;
    }

    .header-badges {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 0.5rem;
      flex-wrap: nowrap;
      flex-shrink: 0;
      text-align: right;
    }

    .badge {
      display: inline-block;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.4rem 0.8rem;
      border-radius: 9999px;
      font-size: 0.82rem;
      font-weight: 600;
      background: #121826;
      background: var(--card-bg);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
      white-space: nowrap;
      flex-shrink: 0;
      vertical-align: middle;
      margin-left: 0.25rem;
    }

    .badge-threads {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.4rem 0.8rem;
      border-radius: 9999px;
      font-size: 0.82rem;
      font-weight: 600;
      border: 1px solid rgba(56, 189, 248, 0.4);
      background: rgba(56, 189, 248, 0.1);
      color: #38bdf8;
      backdrop-filter: blur(12px);
      white-space: nowrap;
      flex-shrink: 0;
      vertical-align: middle;
      font-family: 'JetBrains Mono', monospace;
    }

    .header-progress-wrap {
      width: 100%;
      height: 3px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 2px;
      overflow: hidden;
      margin-top: 0.35rem;
      margin-bottom: 0.65rem;
    }

    .header-progress-bar {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #3b82f6, var(--variant-color));
      transition: width 0.3s ease;
    }

    .header-stat-line {
      display: flex;
      flex-wrap: nowrap;
      align-items: center;
      gap: 1.75rem;
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.82rem;
      color: var(--text-muted);
      margin-bottom: 1.25rem;
    }

    .header-stat-item {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
    }

    .header-stat-label {
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 0.05em;
      font-size: 0.75rem;
    }

    .header-stat-val {
      color: var(--text-main);
      font-weight: 600;
      font-size: 0.85rem;
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

    .badge-status-better,
    .badge-status-keep {
      border-color: rgba(16, 185, 129, 0.5);
      background: rgba(16, 185, 129, 0.15);
      color: var(--variant-color);
      font-weight: 700;
    }
    .badge-status-better .status-dot,
    .badge-status-keep .status-dot {
      background-color: var(--variant-color);
      box-shadow: 0 0 10px var(--variant-color);
      animation: none;
    }

    .badge-status-equal,
    .badge-status-parity {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.15);
      color: #f59e0b;
      font-weight: 700;
    }
    .badge-status-equal .status-dot,
    .badge-status-parity .status-dot {
      background-color: #f59e0b;
      box-shadow: 0 0 10px #f59e0b;
      animation: none;
    }

    .badge-status-worse,
    .badge-status-reject {
      border-color: rgba(239, 68, 68, 0.5);
      background: rgba(239, 68, 68, 0.15);
      color: var(--danger-color);
      font-weight: 700;
    }
    .badge-status-worse .status-dot,
    .badge-status-reject .status-dot {
      background-color: var(--danger-color);
      box-shadow: 0 0 10px var(--danger-color);
      animation: none;
    }

    .badge-status-stalled {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.15);
      color: #f59e0b;
      font-weight: 700;
    }
    .badge-status-stalled .status-dot {
      background-color: #f59e0b;
      box-shadow: 0 0 10px #f59e0b;
      animation: pulse 1.5s infinite;
    }

    .badge-target {
      font-weight: 700;
      letter-spacing: 0.03em;
      font-size: 0.80rem;
      border-color: rgba(245, 158, 11, 0.4);
      background: rgba(245, 158, 11, 0.15);
      color: #f59e0b;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
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

    /* Run History Pipeline Redesign (Reusing Pipeline Tokens & Components) */
    #historyCard {
      height: auto;
      min-height: auto;
      max-height: none;
      overflow: visible;
    }

    #historyCard .stage-panel-body,
    #historyPanelBody {
      overflow-y: visible;
      overflow-x: auto;
      height: auto;
      max-height: none;
      flex: none;
    }

    .run-history-table {
      width: 100%;
      table-layout: fixed;
      border-collapse: separate;
      border-spacing: 0;
    }

    .run-history-table th {
      padding: var(--pipeline-space-sm) 0.5rem;
      font-size: var(--pipeline-font-meta);
      font-family: 'Outfit', sans-serif;
      font-weight: 600;
      color: var(--pipeline-text-muted);
      border-bottom: 1px solid var(--pipeline-border-header);
      vertical-align: middle;
      position: sticky;
      top: 0;
      z-index: 5;
      background: var(--pipeline-th-bg);
      text-transform: none;
      letter-spacing: normal;
      box-sizing: border-box;
      white-space: nowrap;
      overflow: hidden;
    }

    .run-history-table th.th-left,
    .run-history-table td.td-left {
      text-align: left;
    }

    .run-history-table th.th-center,
    .run-history-table td.td-center {
      text-align: center;
    }

    .run-history-table th.th-right,
    .run-history-table td.td-right {
      text-align: right;
    }

    .run-history-table tbody tr.history-row td {
      padding: 0.35rem 0.5rem;
      border-top: none;
      border-bottom: 1px solid var(--pipeline-border-row);
      vertical-align: middle;
      font-family: var(--pipeline-font-mono);
      font-size: var(--pipeline-font-body);
      box-sizing: border-box;
      transition: background 0.15s ease;
      overflow: hidden;
    }

    .run-history-table tbody tr.entry-even td {
      background: rgba(255, 255, 255, 0.028);
    }

    .run-history-table tbody tr.entry-odd td {
      background: transparent;
    }

    .run-history-table tbody tr.history-row:hover td {
      background: rgba(255, 255, 255, 0.065) !important;
    }

    .hist-date-main {
      font-weight: 600;
      color: var(--pipeline-text-main);
      line-height: 1.25;
      white-space: nowrap;
    }

    .hist-date-time {
      font-size: 0.72rem;
      color: var(--pipeline-text-muted);
      line-height: 1.25;
      white-space: nowrap;
    }

    .cell-experiment {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .hist-exp-text {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      text-transform: none !important;
      font-family: 'Outfit', sans-serif;
      font-weight: 500;
      color: var(--pipeline-text-main);
      font-size: var(--pipeline-font-body);
    }

    .hist-depth-delta {
      font-weight: 600;
      line-height: 1.25;
      white-space: nowrap;
    }

    .hist-depth-nps {
      font-size: 0.72rem;
      color: var(--pipeline-text-muted);
      line-height: 1.25;
      white-space: nowrap;
    }

    .hist-src-line {
      font-size: 0.72rem;
      color: var(--pipeline-text-muted);
      font-weight: 600;
      line-height: 1.35;
      text-align: center;
      white-space: nowrap;
    }

    .hist-mode-line {
      font-size: 0.80rem;
      line-height: 1.35;
      white-space: nowrap;
      text-align: right;
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
      flex-direction: row;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 0.75rem;
      border-radius: 0.55rem;
      cursor: pointer;
      transition: all 0.2s ease;
      border: 1px solid transparent;
      user-select: none;
      background: rgba(255, 255, 255, 0.02);
      min-width: 0;
    }

    .stage-segment:hover {
      background: rgba(255, 255, 255, 0.05);
      border-color: rgba(255, 255, 255, 0.12);
    }

    /* Base fallback selected highlight */
    .stage-segment.active {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.35);
      box-shadow: 0 0 10px rgba(255, 255, 255, 0.06);
    }

    /* Selected card keeping its status color */
    .stage-segment.running.active {
      background: rgba(59, 130, 246, 0.14);
      border-color: var(--pipeline-blue);
      box-shadow: 0 0 14px rgba(59, 130, 246, 0.25);
    }

    .stage-segment.running.active .stage-seg-index {
      background: rgba(59, 130, 246, 0.25);
      color: var(--pipeline-blue);
    }

    .stage-segment.running.active:hover {
      background: rgba(59, 130, 246, 0.18);
    }

    .stage-segment.running:hover:not(.active) {
      background: rgba(59, 130, 246, 0.06);
      border-color: rgba(59, 130, 246, 0.4);
    }

    .stage-segment.done.active,
    .stage-segment.passed.active,
    .stage-segment.pass.active {
      background: rgba(16, 185, 129, 0.14);
      border-color: var(--pipeline-green);
      box-shadow: 0 0 14px rgba(16, 185, 129, 0.22);
    }

    .stage-segment.done.active .stage-seg-index,
    .stage-segment.passed.active .stage-seg-index,
    .stage-segment.pass.active .stage-seg-index {
      background: rgba(16, 185, 129, 0.25);
      color: var(--pipeline-green);
    }

    .stage-segment.done.active:hover,
    .stage-segment.passed.active:hover,
    .stage-segment.pass.active:hover {
      background: rgba(16, 185, 129, 0.18);
    }

    .stage-segment.done:hover:not(.active),
    .stage-segment.passed:hover:not(.active),
    .stage-segment.pass:hover:not(.active) {
      background: rgba(16, 185, 129, 0.06);
      border-color: rgba(16, 185, 129, 0.4);
    }

    .stage-segment.failed.active,
    .stage-segment.revert.active {
      background: rgba(239, 68, 68, 0.14);
      border-color: var(--pipeline-red);
      box-shadow: 0 0 14px rgba(239, 68, 68, 0.22);
    }

    .stage-segment.failed.active .stage-seg-index,
    .stage-segment.revert.active .stage-seg-index {
      background: rgba(239, 68, 68, 0.25);
      color: var(--pipeline-red);
    }

    .stage-segment.failed.active:hover,
    .stage-segment.revert.active:hover {
      background: rgba(239, 68, 68, 0.18);
    }

    .stage-segment.failed:hover:not(.active),
    .stage-segment.revert:hover:not(.active) {
      background: rgba(239, 68, 68, 0.06);
      border-color: rgba(239, 68, 68, 0.4);
    }

    .stage-segment.parity.active,
    .stage-segment.warning.active {
      background: rgba(245, 158, 11, 0.14);
      border-color: var(--pipeline-amber);
      box-shadow: 0 0 14px rgba(245, 158, 11, 0.22);
    }

    .stage-segment.parity.active .stage-seg-index,
    .stage-segment.warning.active .stage-seg-index {
      background: rgba(245, 158, 11, 0.25);
      color: var(--pipeline-amber);
    }

    .stage-segment.parity.active:hover,
    .stage-segment.warning.active:hover {
      background: rgba(245, 158, 11, 0.18);
    }

    .stage-segment.parity:hover:not(.active),
    .stage-segment.warning:hover:not(.active) {
      background: rgba(245, 158, 11, 0.06);
      border-color: rgba(245, 158, 11, 0.4);
    }

    .stage-seg-index {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.08);
      font-size: 0.7rem;
      font-weight: 700;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
      flex-shrink: 0;
    }

    .stage-seg-info {
      display: flex;
      flex-direction: column;
      gap: 0.15rem;
      flex: 1;
      min-width: 0;
    }

    .stage-seg-name {
      font-size: 0.84rem;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--pipeline-text-main);
      line-height: 1.2;
    }

    .stage-seg-duration {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.72rem;
      color: var(--text-muted);
      font-weight: 600;
      line-height: 1;
    }

    .stage-seg-status {
      font-size: var(--pipeline-font-chip);
      font-weight: 800;
      padding: 0.18rem 0.48rem;
      border-radius: var(--pipeline-radius-pill);
      text-transform: uppercase;
      font-family: var(--pipeline-font-mono);
      letter-spacing: 0.03em;
      flex-shrink: 0;
      line-height: 1.2;
    }

    .stage-detail-panel {
      margin-top: 1rem;
    }

    /* ── Shared Validation Pipeline Components ─────────────────────────── */

    /* 1. StagePanel */
    .stage-panel {
      display: flex;
      flex-direction: column;
      background: var(--pipeline-panel-bg);
      border: 1px solid var(--pipeline-border);
      border-radius: var(--pipeline-radius-lg);
      padding: var(--pipeline-space-md) var(--pipeline-space-lg);
      box-sizing: border-box;
      overflow: hidden;
      position: relative;
    }

    .stage-panel-body {
      height: 192px;
      max-height: 192px;
      overflow-y: auto;
      overflow-x: hidden;
      box-sizing: border-box;
    }

    /* Card 1 (Unit Tests): scrollable through all ~40 tests, normalized to exact height of Cards 2–4 */
    #unitCard .stage-panel-body {
      height: 192px;
      max-height: 192px;
      overflow-y: auto;
    }

    /* Cards 2–4: exactly 4 data rows — no scroll, no trailing gap */
    #depthCard .stage-panel-body,
    #matrixCard .stage-panel-body,
    #poolCard .stage-panel-body {
      height: 192px;
      max-height: 192px;
      overflow-y: hidden;
    }

    .card-toggle-segmented {
      display: inline-flex;
      background: rgba(255, 255, 255, 0.06);
      border-radius: var(--pipeline-radius-pill);
      padding: 2px;
      border: 1px solid var(--pipeline-border);
      gap: 2px;
    }

    .card-toggle-segmented .btn-seg {
      background: transparent;
      border: none;
      color: var(--pipeline-text-muted);
      padding: 0.2rem 0.65rem;
      border-radius: var(--pipeline-radius-pill);
      font-size: var(--pipeline-font-sm);
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      font-family: inherit;
    }

    .card-toggle-segmented .btn-seg.active {
      background: var(--pipeline-blue);
      color: #fff;
    }

    /* 2. PanelHeader */
    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: var(--pipeline-space-xs);
      flex-shrink: 0;
    }

    .panel-header-title {
      font-size: var(--pipeline-font-title);
      font-weight: 800;
      color: var(--pipeline-text-main);
      display: flex;
      align-items: center;
      gap: var(--pipeline-space-sm);
    }

    .panel-header-icon {
      font-size: 1.1rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 1.4rem;
    }

    .panel-meta {
      font-size: var(--pipeline-font-meta);
      font-family: var(--pipeline-font-mono);
      font-weight: 600;
      color: var(--pipeline-text-muted);
    }

    /* Subheader (for Stage 4 Breakdown) */
    .panel-subheader {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: var(--pipeline-space-md);
      margin-bottom: var(--pipeline-space-xs);
      padding: var(--pipeline-space-xs) 0;
      border-top: 1px solid var(--pipeline-border-header);
    }

    .panel-subheader-title {
      font-size: var(--pipeline-font-sub);
      font-weight: 700;
      color: var(--pipeline-text-muted);
      display: flex;
      align-items: center;
      gap: var(--pipeline-space-xs);
    }

    .panel-subheader-icon {
      font-size: 0.95rem;
    }

    /* 3. StatusChip */
    .status-chip,
    .matrix-col-status {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0.2rem 0.55rem;
      font-size: var(--pipeline-font-chip);
      font-weight: 700;
      border-radius: var(--pipeline-radius-pill);
      text-transform: uppercase;
      font-family: var(--pipeline-font-mono);
      letter-spacing: 0.03em;
      line-height: 1.2;
      box-sizing: border-box;
      white-space: nowrap;
    }

    .status-chip-neutral,
    .status-chip-pending,
    .matrix-status-pending,
    .status-chip-queued,
    .matrix-status-queued,
    .status-chip-idle,
    .matrix-status-idle,
    .status-chip-not-run,
    .matrix-status-not-run,
    .status-chip-skipped,
    .matrix-status-skipped {
      background: rgba(156, 163, 175, 0.15);
      color: var(--pipeline-grey);
      border: 1px solid rgba(156, 163, 175, 0.25);
    }

    .status-chip-running,
    .matrix-status-running {
      background: rgba(59, 130, 246, 0.15);
      color: var(--pipeline-blue);
      border: 1px solid rgba(59, 130, 246, 0.35);
    }

    .status-chip-better,
    .matrix-status-better,
    .status-chip-passed,
    .status-chip-pass,
    .status-chip-done,
    .matrix-status-passed,
    .matrix-status-pass,
    .matrix-status-done {
      background: rgba(16, 185, 129, 0.15);
      color: var(--pipeline-green);
      border: 1px solid rgba(16, 185, 129, 0.35);
    }

    .status-chip-worse,
    .matrix-status-worse,
    .status-chip-failed,
    .matrix-status-failed {
      background: rgba(239, 68, 68, 0.15);
      color: var(--pipeline-red);
      border: 1px solid rgba(239, 68, 68, 0.35);
    }

    .status-chip-equal,
    .matrix-status-equal,
    .status-chip-warning,
    .status-chip-parity,
    .matrix-status-parity {
      background: rgba(245, 158, 11, 0.15);
      color: var(--pipeline-amber);
      border: 1px solid rgba(245, 158, 11, 0.35);
    }

    /* 5. DataTable */
    .data-table {
      width: 100%;
      border-collapse: collapse;
      font-family: var(--pipeline-font-mono);
      font-variant-numeric: tabular-nums;
    }

    .data-table th {
      background: var(--pipeline-th-bg);
      color: var(--pipeline-text-muted);
      font-size: var(--pipeline-font-sm);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      padding: 0.6rem 0.75rem;
      border-bottom: 1px solid var(--pipeline-border-header);
      position: sticky;
      top: 0;
      z-index: 2;
    }

    .data-table td {
      padding: 0.55rem 0.75rem;
      border-bottom: 1px solid var(--pipeline-border-row);
      font-size: var(--pipeline-font-body);
      vertical-align: middle;
    }

    .data-table tr:last-child td {
      border-bottom: none;
    }

    .data-table tr:hover td {
      background: var(--pipeline-row-hover);
    }

    .data-table tr.active-running td {
      background: var(--pipeline-row-active);
    }

    .th-left, .td-left {
      text-align: left !important;
    }

    .th-right, .td-right {
      text-align: right !important;
    }

    td.empty-cell {
      text-align: center !important;
      color: var(--pipeline-text-muted);
      font-style: italic;
      padding: 2.5rem 1rem !important;
    }

    .test-fail-row td {
      background: rgba(239, 68, 68, 0.08) !important;
      padding: 0.4rem 0.75rem !important;
    }

    .unit-test-failure-msg {
      padding: 0.5rem 0.65rem;
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.25);
      border-radius: var(--pipeline-radius-sm);
      color: #fca5a5;
      font-size: 0.75rem;
      white-space: pre-wrap;
      word-break: break-all;
      font-family: var(--pipeline-font-mono);
    }

    /* Compatibility classes for legacy references */
    .matrix-detail-wrap, .pool-detail-wrap, .depth-summary-container {
      position: relative;
    }

    .live-matrix-table {
      width: 100%;
      border-collapse: collapse;
    }

    .sep-dot {
      color: var(--pipeline-text-muted);
      opacity: 0.6;
    }

    .btn-toggle-expand {
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: var(--pipeline-text-main);
      padding: 0.4rem 0.85rem;
      border-radius: var(--pipeline-radius-sm);
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
    .chip-badge.pending, .chip-badge.skipped { background: rgba(156, 163, 175, 0.15); color: var(--text-muted); }

    .matrix-col-status {
      font-size: 0.68rem;
      font-weight: 800;
      padding: 0.18rem 0.45rem;
      border-radius: 9999px;
      text-transform: uppercase;
      font-family: 'JetBrains Mono', monospace;
      letter-spacing: 0.03em;
    }

    .matrix-status-pending, .matrix-status-not-run, .matrix-status-skipped { background: rgba(156, 163, 175, 0.15); color: var(--text-muted); }
    .matrix-status-running { background: rgba(59, 130, 246, 0.25); color: #60a5fa; }
    .matrix-status-passed, .matrix-status-pass, .matrix-status-done { background: rgba(16, 185, 129, 0.25); color: #34d399; }
    .matrix-status-parity { background: rgba(245, 158, 11, 0.25); color: #f59e0b; }
    .matrix-status-failed { background: rgba(239, 68, 68, 0.25); color: #f87171; }

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
      <div class="badge badge-target" id="targetBadge" style="display: none;">
        <span id="targetText">🎯 IMPROVEMENT</span>
      </div>
      <div class="badge" id="statusBadge">
        <span class="status-dot" id="statusDot"></span>
        <span id="statusText">IDLE</span>
      </div>
      <span id="headerNoData" style="display: none; color: var(--text-muted); font-size: 0.95rem; font-weight: 600; padding: 4px 8px;">No data.</span>
    </div>
  </header>

  <!-- ABORTED Banner directly under the header -->
  <div id="abortedBanner" class="banner revert" style="display: none; margin-bottom: 1.5rem; border: 2px solid var(--pipeline-amber); background: rgba(245, 158, 11, 0.12);">
    <div>
      <div class="banner-title" style="color: var(--pipeline-amber); font-size: 1.05rem; font-weight: 800; margin-bottom: 0.35rem;">⚠️ SIMULATION ABORTED</div>
      <div class="banner-desc" id="abortedBannerReason" style="font-weight: 600; font-family: 'JetBrains Mono', monospace; margin-bottom: 0.25rem;"></div>
      <div class="banner-desc" id="abortedBannerDetail" style="color: var(--text-muted); font-size: 0.85rem;"></div>
    </div>
  </div>

  <!-- Slim progress bar directly under the header title row -->
  <div class="header-progress-wrap" id="headerProgressWrap" title="Overall Run Progress">
    <div class="header-progress-bar" id="headerProgressBar" style="width: 0%;"></div>
  </div>

  <!-- Header Stat Line (Requirement 1) -->
  <div class="header-stat-line" id="headerStatLine">
    <div class="header-stat-item" id="headerStatItemElapsed">
      <span class="header-stat-label">ELAPSED</span>
      <strong class="header-stat-val" id="headerStatElapsed">--</strong>
    </div>
    <div class="header-stat-item" id="headerStatItemLeft">
      <span class="header-stat-label">LEFT</span>
      <strong class="header-stat-val" id="headerStatLeft">--</strong>
    </div>
    <div class="header-stat-item" id="headerStatItemRate">
      <span class="header-stat-label">RATE</span>
      <strong class="header-stat-val" id="headerStatRate">--</strong>
    </div>
    <div class="header-stat-item" id="headerStatItemThreads">
      <span class="header-stat-label">THREADS</span>
      <strong class="header-stat-val" id="threadsCount">--</strong>
    </div>
  </div>

  <!-- Horizontal Stage Rail (Requirement 1) -->
  <div class="stage-rail-card" id="pipelineCard">
    <div class="stage-rail-header">
      <span class="pipeline-title">⚡ Validation Pipeline</span>
    </div>
    <div class="stage-rail" id="stageRail">
      <div class="stage-segment pending" id="railSegment-unit">
        <span class="stage-seg-index">1</span>
        <div class="stage-seg-info">
          <span class="stage-seg-name">Unit Tests</span>
          <span class="stage-seg-duration" id="railDuration-unit">--</span>
        </div>
        <span class="stage-seg-status matrix-status-queued" id="railStatus-unit">QUEUED</span>
      </div>

      <div class="stage-segment pending" id="railSegment-depth">
        <span class="stage-seg-index">2</span>
        <div class="stage-seg-info">
          <span class="stage-seg-name">Depth Benchmark</span>
          <span class="stage-seg-duration" id="railDuration-depth">--</span>
        </div>
        <span class="stage-seg-status matrix-status-queued" id="railStatus-depth">QUEUED</span>
      </div>

      <div class="stage-segment active pending" id="railSegment-self">
        <span class="stage-seg-index">3</span>
        <div class="stage-seg-info">
          <span class="stage-seg-name">Self-Play Matrix</span>
          <span class="stage-seg-duration" id="railDuration-self">--</span>
        </div>
        <span class="stage-seg-status matrix-status-queued" id="railStatus-self">QUEUED</span>
      </div>

      <div class="stage-segment pending" id="railSegment-pool">
        <span class="stage-seg-index">4</span>
        <div class="stage-seg-info">
          <span class="stage-seg-name">Zoo Pool Gate</span>
          <span class="stage-seg-duration" id="railDuration-pool">--</span>
        </div>
        <span class="stage-seg-status matrix-status-queued" id="railStatus-pool">QUEUED</span>
      </div>
    </div>

    <!-- Stage Detail Panel (Expanded below rail, Requirement 1) -->
    <div class="stage-detail-panel" id="stageDetailPanel">
      <!-- 1. Unit Tests Detail (Requirement 2) -->
      <div class="stage-panel-content" id="panel-unit" style="display: none;">
        <div class="stage-panel" id="unitCard">
          <div class="panel-header" id="unitPanelHeader">
            <div class="panel-header-title">
              <span class="panel-header-icon">⚡</span>
              <span>Tactical Unit Tests</span>
            </div>
          </div>

          <div class="stage-panel-body">
            <div id="unitEmptyWrap" style="display: none; text-align: center; color: var(--pipeline-text-muted); padding: 2.5rem; font-size: 0.95rem;">No data.</div>
            <div id="unitAbortedWrap" style="display: none; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: 'JetBrains Mono', monospace;"></div>
            <div id="unitTableWrap">
              <table class="data-table">
                <thead>
                  <tr>
                    <th class="th-right" style="width: 50px;">#</th>
                    <th class="th-left">Test Name</th>
                    <th class="th-right" style="width: 110px;">Status</th>
                  </tr>
                </thead>
                <tbody id="unitTestsList">
                  <!-- Dynamically populated test rows -->
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. Depth Benchmark Detail -->
      <div class="stage-panel-content" id="panel-depth" style="display: none;">
        <div class="stage-panel" id="depthCard">
          <div class="panel-header" id="depthPanelHeader">
            <div class="panel-header-title">
              <span class="panel-header-icon">📐</span>
              <span>Fixed Search Depth Benchmark</span>
            </div>
          </div>

          <!-- Compatibility values for unit tests -->
          <div style="display: none;">
            <span id="depthVarValue">0.0</span>
            <span id="depthBaseValue">0.0</span>
            <span id="depthNpsRatioValue">1.00x</span>
            <span id="depthAgreeValue">100.0%</span>
            <span id="depthVarMeta"></span>
            <span id="depthNpsMeta"></span>
            <button class="btn-toggle-expand" id="btnToggleDepth" onclick="toggleDepthTable()"></button>
          </div>

          <div class="stage-panel-body" id="depthTableContainer">
            <div id="depthEmptyWrap" style="display: none; text-align: center; color: var(--pipeline-text-muted); padding: 2.5rem; font-size: 0.95rem;">No data.</div>
            <div id="depthAbortedWrap" style="display: none; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: 'JetBrains Mono', monospace;"></div>
            <div id="depthTableWrap">
              <table class="data-table" id="depthModesTable">
                <thead>
                  <tr>
                    <th class="th-left">Mode</th>
                    <th class="th-right">Base Depth</th>
                    <th class="th-right">Var Depth</th>
                    <th class="th-right">Δ Depth</th>
                    <th class="th-right">Base NPS</th>
                    <th class="th-right">Var NPS</th>
                    <th class="th-right">NPS Ratio</th>
                    <th class="th-right">Agree %</th>
                    <th class="th-right">Status</th>
                  </tr>
                </thead>
                <tbody id="depthTableBody">
                  <!-- Dynamically populated depth mode rows -->
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 3. Self-Play Matrix Detail (Requirement 3: centerpiece table) -->
      <div class="stage-panel-content" id="panel-self" style="display: block;">
        <div class="stage-panel" id="matrixCard">
          <div class="panel-header" id="matrixPanelHeader">
            <div class="panel-header-title">
              <span class="panel-header-icon">🏁</span>
              <span>Self-Play Multi-Mode Matrix</span>
            </div>
          </div>

          <div class="stage-panel-body">
            <div id="matrixEmptyWrap" style="display: none; text-align: center; color: var(--pipeline-text-muted); padding: 2.5rem; font-size: 0.95rem;">No data.</div>
            <div id="matrixAbortedWrap" style="display: none; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: 'JetBrains Mono', monospace;"></div>
            <div id="matrixTableWrap">
              <table class="data-table live-matrix-table" id="matrixTable">
                <thead>
                  <tr>
                    <th class="th-left">Mode</th>
                    <th class="th-right">Games</th>
                    <th class="th-right">Win Share</th>
                    <th class="th-right">Δ (Delta)</th>
                    <th class="th-right">Z-Score</th>
                    <th class="th-right">Time</th>
                    <th class="th-right">Status</th>
                  </tr>
                </thead>
                <tbody id="matrixTableBody">
                  <!-- Dynamically populated rows for Constrictor, Royale, Duel, Standard -->
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <!-- 4. Zoo Pool Gate Detail -->
      <div class="stage-panel-content" id="panel-pool" style="display: none;">
        <div class="stage-panel" id="poolCard">
          <div class="panel-header" id="poolPanelHeader">
            <div class="panel-header-title">
              <span class="panel-header-icon">🐍</span>
              <span>Zoo Pool Gate</span>
            </div>
          </div>

          <div class="stage-panel-body">
            <div id="poolEmptyWrap" style="display: none; text-align: center; color: var(--pipeline-text-muted); padding: 2.5rem; font-size: 0.95rem;">No data.</div>
            <div id="poolAbortedWrap" style="display: none; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: 'JetBrains Mono', monospace;"></div>
            <div id="poolTableWrap">
              <table class="data-table live-matrix-table" id="poolModesTable">
                <thead>
                  <tr>
                    <th class="th-left">Mode</th>
                    <th class="th-right">Games</th>
                    <th class="th-right">Var Share</th>
                    <th class="th-right">Base Share</th>
                    <th class="th-right">Δ (Delta)</th>
                    <th class="th-right">Z-Score</th>
                    <th class="th-right">Status</th>
                  </tr>
                </thead>
                <tbody id="poolModesTableBody">
                  <!-- Dynamically populated rows for Pool Modes -->
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- A/B Test Simulation Run History (Reusing Pipeline Components) -->

  <!-- A/B Test Simulation Run History (Reusing Pipeline Components) -->
  <div class="stage-panel" id="historyCard" style="margin-top: 1.5rem; margin-bottom: 1.5rem;">
    <div class="panel-header" id="historyPanelHeader">
      <div class="panel-header-title">
        <span class="panel-header-icon">📜</span>
        <span>Run History</span>
      </div>
      <span class="panel-meta" id="historyCount">0 Runs</span>
    </div>
    <div class="stage-panel-body" id="historyPanelBody">
      <table class="data-table run-history-table" id="historyTable" style="table-layout: fixed; width: 100%;">
        <colgroup>
          <col style="width: 7%;">
          <col style="width: 23%;">
          <col style="width: 6%;">
          <col style="width: 8%;">
          <col style="width: 4%;">
          <col style="width: 11%;">
          <col style="width: 11%;">
          <col style="width: 11%;">
          <col style="width: 11%;">
          <col style="width: 8%;">
        </colgroup>
        <thead>
          <tr>
            <th class="th-left" style="width: 7%;">Date</th>
            <th class="th-left" style="width: 23%;">Experiment</th>
            <th class="th-center" style="width: 6%;">Tests</th>
            <th class="th-center" style="width: 8%;">Depth</th>
            <th class="th-center" style="width: 4%;">Src</th>
            <th class="th-right" style="width: 11%;">Constrictor</th>
            <th class="th-right" style="width: 11%;">Royale</th>
            <th class="th-right" style="width: 11%;">Duel</th>
            <th class="th-right" style="width: 11%;">Standard</th>
            <th class="th-right" style="width: 8%;">Verdict</th>
          </tr>
        </thead>
        <tbody id="historyTableBody">
          <tr class="empty-row">
            <td colspan="10" class="empty-cell" style="text-align: center; color: var(--pipeline-text-muted); padding: 3rem;">No runs yet</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
  function formatDuration(sec, allowDecimal = false, defaultVal = '--') {
    if (sec == null || isNaN(sec)) return defaultVal;
    const s = Number(sec);
    if (s < 0) return defaultVal;

    const totalSec = Math.round(s);
    const hours = Math.floor(totalSec / 3600);
    const mins = Math.floor((totalSec % 3600) / 60);
    const secs = totalSec % 60;

    if (hours > 0) {
      const mStr = String(mins).padStart(2, '0');
      const sStr = String(secs).padStart(2, '0');
      return `${hours}:${mStr}:${sStr}`;
    }
    const sStr = String(secs).padStart(2, '0');
    return `${mins}:${sStr}`;
  }

  function formatLeftDuration(sec, defaultVal = '--') {
    if (sec == null || isNaN(sec)) return defaultVal;
    const s = Number(sec);
    if (s <= 0) return defaultVal;
    return formatDuration(s, false, defaultVal);
  }

  const formatSeconds = formatDuration;
  const formatElapsedClock = formatDuration;
  const formatEtaMinutes = formatLeftDuration;

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatHistoryTime(raw) {
    if (!raw || raw === '-') return '-';
    const str = String(raw).trim();
    const m = str.match(/^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})[ T](.+)$/);
    if (!m) return str;
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const monthIdx = parseInt(m[2], 10) - 1;
    const monthStr = (monthIdx >= 0 && monthIdx < 12) ? months[monthIdx] : m[2];
    const day = parseInt(m[3], 10);
    const timePart = m[4].replace(/^0(\\d:\\d{2}:\\d{2})/, '$1');
    return `${monthStr} ${day} ${timePart}`;
  }

  const DEFAULT_UNIT_TEST_NAMES = [
    "test_avoid_dead_end_when_boxed_in_corner",
    "test_avoid_risky_head_kill_chase",
    "test_avoid_running_into_opponent_if_open_space",
    "test_c_engine_loads_without_compiler_if_binary_exists",
    "test_c_engine_mandatory_fail_fast",
    "test_concurrent_move_requests_under_timeout_cap",
    "test_concurrent_thread_safety_no_segfault",
    "test_constrictor_7x7_turn_11_must_take_right_for_tie",
    "test_constrictor_turn_22_must_take_right",
    "test_constrictor_turn_30_must_take_down",
    "test_dont_run_into_tail_if_constrictor",
    "test_dont_run_into_tail_if_just_ate",
    "test_duel_turn_172_avoid_wall_pocket_tunnel_left",
    "test_duel_turn_208_avoid_dead_end_pocket_suicide",
    "test_duel_turn_78_must_keep_opponent_cornered",
    "test_duel_turn_79_avoid_corner_trap_wall_collision",
    "test_execute_adjacent_head_kill_on_smaller_opponent",
    "test_failure_case_turn_45_avoid_contested_head_collision",
    "test_game_de138756_turn_106_avoid_suicidal_head_threat_override",
    "test_multiplayer_turn_108_avoid_opponent_body_suicide",
    "test_multiplayer_turn_10_avoid_non_receding_tail_collision",
    "test_multiplayer_turn_116_avoid_flibba_head_to_head_trap",
    "test_multiplayer_turn_119_avoid_wall_down",
    "test_multiplayer_turn_194_pocket_trap_follow_tail",
    "test_multiplayer_turn_29_avoid_wall_left",
    "test_multiplayer_turn_31_avoid_wall_collision",
    "test_multiplayer_turn_321_cornered_down_into_tail",
    "test_multiplayer_turn_61_avoid_wall_and_opponent_body_suicide",
    "test_multiplayer_turn_61_avoid_wall_corner_pocket_trap",
    "test_multiplayer_turn_63_avoid_self_collision",
    "test_multiplayer_turn_85_avoid_wall_up",
    "test_multiplayer_turn_97_avoid_opponent_body_suicide",
    "test_never_run_into_wall",
    "test_never_run_into_yourself",
    "test_prefer_opponent_over_wall_or_self",
    "test_royale_danger_zone_low_health_must_seek_food",
    "test_trap_avoidance_dead_end_pocket",
    "test_urgent_food_eating_on_critical_health",
    "test_wrap_mode_boundary_escape",
    "test_wrap_mode_flood_fill_modulo"
  ];

  function renderUnitTestsList(tests) {
    if (!tests || tests.length === 0) {
      return DEFAULT_UNIT_TEST_NAMES.map((name, idx) => {
        const num = idx + 1;
        const chipHtml = '<span class="status-chip status-chip-queued matrix-col-status matrix-status-queued">QUEUED</span>';
        const escName = escapeHtml(name);
        return `
        <tr>
          <td class="td-right" style="color: var(--pipeline-text-muted);">${num}.</td>
          <td class="td-left" style="font-weight: 600; color: var(--pipeline-text-main);">${escName}</td>
          <td class="td-right">${chipHtml}</td>
        </tr>`;
      }).join('');
    }
    const sorted = [...tests].sort((a, b) => {
      const aFail = (a.status === 'FAILED' || a.status === 'ERROR') ? 0 : 1;
      const bFail = (b.status === 'FAILED' || b.status === 'ERROR') ? 0 : 1;
      if (aFail !== bFail) return aFail - bFail;
      return (a.name || '').localeCompare(b.name || '');
    });

    return sorted.map((t, idx) => {
      const num = idx + 1;
      const st = String(t.status || '').toUpperCase();
      const isFailed = (st === 'FAILED' || st === 'ERROR');
      const isQueued = (st === 'QUEUED' || st === 'PENDING' || st === '');
      let statusText = 'EQUAL';
      let statusClass = 'equal';
      if (isFailed) {
        statusText = 'WORSE';
        statusClass = 'worse';
      } else if (isQueued) {
        statusText = 'QUEUED';
        statusClass = 'queued';
      }
      const chipHtml = `<span class="status-chip status-chip-${statusClass} matrix-col-status matrix-status-${statusClass}">${statusText}</span>`;
      const escName = escapeHtml(t.name || 'unnamed_test');
      const failRow = (isFailed && t.message)
        ? `<tr class="test-fail-row"><td colspan="3"><div class="unit-test-failure-msg">${escapeHtml(t.message)}</div></td></tr>`
        : '';

      return `
        <tr class="${isFailed ? 'row-failed' : ''}">
          <td class="td-right" style="color: var(--pipeline-text-muted);">${num}.</td>
          <td class="td-left" style="font-weight: 600; color: var(--pipeline-text-main);">${escName}</td>
          <td class="td-right">${chipHtml}</td>
        </tr>
        ${failRow}
      `;
    }).join('');
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

  function deriveUiState(data) {
    if (!data) return 'EMPTY';
    const expDesc = (data.experiment_desc || '').trim();
    const totalPlanned = Number(data.total_planned_games) || 0;
    if (!expDesc || totalPlanned === 0) {
      return 'EMPTY';
    }
    if (data.runner_alive === true) {
      return 'RUNNING';
    }
    const stopType = String(data.stop_type || '').toUpperCase();
    const status = String(data.status || '').toUpperCase();
    if (stopType === 'DISCONNECTED' || stopType === 'FAILED') {
      return 'ABORTED';
    }
    if (['MATRIX_COMPLETE', 'KEEP', 'PARITY', 'REVERT', 'FINISHED', 'TEST_FAILURE', 'MAXIMUM_REACHED', 'EARLY_STOP_HIGH', 'EARLY_STOP_LOW', 'USER_SKIPPED'].includes(stopType) || status === 'FINISHED' || status === 'TEST_FAILURE') {
      return 'COMPLETE';
    }
    if (data.stop_reason || data.recommendation) {
      return 'COMPLETE';
    }
    return 'EMPTY';
  }

  function computeCompletenessLine(data) {
    const parts = [];
    const matrix = data.matrix_progress || data.self_play_matrix || [];
    if (matrix.length > 0) {
      const matrixPlayed = matrix.reduce((acc, m) => acc + (Number(m.games || m.total_played) || 0), 0);
      const matrixMax = matrix.reduce((acc, m) => acc + (Number(m.max_games) || 200), 0) || 800;
      const doneCount = matrix.filter(m => ['PASSED', 'PARITY', 'FAILED', 'DONE'].includes(m.status)).length;
      if (doneCount === matrix.length && matrixPlayed >= matrixMax) {
        parts.push(`matrix complete (${matrixPlayed}/${matrixMax})`);
      } else if (matrixPlayed > 0) {
        parts.push(`matrix partial (${matrixPlayed}/${matrixMax})`);
      } else {
        parts.push('matrix unplayed');
      }
    } else {
      parts.push('matrix unplayed');
    }

    const pool = data.pool_data || {};
    const poolGames = Number(pool.total_games) || 0;
    if (poolGames === 0) {
      parts.push('pool unplayed');
    } else {
      parts.push(`pool partial (${poolGames} games)`);
    }

    return `Partial results — ${parts.join(', ')}.`;
  }

  function determineInterruptedStage(data) {
    const reason = (data.stop_reason || '').toLowerCase();
    if (reason.includes('unit') || reason.includes('pre-flight')) return 'unit';
    if (reason.includes('depth') || reason.includes('fixture')) return 'depth';
    if (reason.includes('pool') || reason.includes('zoo')) return 'pool';
    if (reason.includes('matrix') || reason.includes('self-play') || reason.includes('simulation')) return 'self';

    const runType = data.run_type || 'self';
    if (runType === 'depth') return 'depth';
    if (runType === 'pool') return 'pool';

    const unitData = data.unit_tests_data || {};
    if (unitData.status === 'FAILED' || (unitData.total && (unitData.passed || 0) < unitData.total)) return 'unit';
    const depthData = data.depth_data || {};
    if (!depthData.completed_positions || depthData.completed_positions < 12) return 'depth';

    const matrix = data.matrix_progress || data.self_play_matrix || [];
    const matrixPlayed = matrix.reduce((acc, m) => acc + (m.games || m.total_played || 0), 0);
    const matrixMax = matrix.reduce((acc, m) => acc + (m.max_games || 200), 0) || 800;
    if (matrixPlayed < matrixMax) return 'self';
    return 'pool';
  }

  function updateStageCardBody(stageId, uiState, isInterrupted, stopReason, customEmptyText) {
    const tableWrap = document.getElementById(stageId + 'TableWrap');
    const emptyWrap = document.getElementById(stageId + 'EmptyWrap');
    const abortedWrap = document.getElementById(stageId + 'AbortedWrap');
    if (!emptyWrap || !abortedWrap) return;

    if (uiState === 'EMPTY') {
      if (tableWrap) tableWrap.style.display = 'block';
      abortedWrap.style.display = 'none';
      emptyWrap.style.display = 'none';
    } else if (uiState === 'ABORTED' && isInterrupted) {
      if (tableWrap) tableWrap.style.display = 'none';
      emptyWrap.style.display = 'none';
      abortedWrap.style.display = 'block';
      abortedWrap.innerText = stopReason || 'Simulation aborted.';
    } else if (customEmptyText) {
      if (tableWrap) tableWrap.style.display = 'none';
      abortedWrap.style.display = 'none';
      emptyWrap.style.display = 'block';
      emptyWrap.innerText = customEmptyText;
    } else {
      if (tableWrap) tableWrap.style.display = 'block';
      emptyWrap.style.display = 'none';
      abortedWrap.style.display = 'none';
    }
  }

  // ── Centralized Unified Timer Architecture ───────────────────────────────────
  // Centralizes clock and tick management across the dashboard to eliminate visual jitter.
  // Instead of separate drifting intervals or alternating frames, all visible timers derive
  // their seconds functionally from wall-clock timestamps snapped to a shared 1000ms epoch boundary.

  const TimerStore = {
    isRunning: false,
    runType: 'self',
    activeModeKey: null,

    // Snapped baseline reference points (multiples of 1000ms aligned to shared epoch)
    epochNow: 0,
    suiteStartMs: 0,
    modeStartMs: 0,
    matrixStartMs: 0,
    poolStartMs: 0,
    totalEtaTargetMs: null,
    modeEtaTargets: {},

    // Static durations for non-running states (IDLE, FINISHED, etc.)
    staticDurations: {
      suiteSec: 0,
      modeSec: 0,
      matrixSec: 0,
      poolSec: 0,
    },

    lastTickSecond: null,
    latestData: null,
    forceRender: true,

    // Sync authoritative state from server poll
    sync(data) {
      if (!data) return;
      this.latestData = data;
      const running = (deriveUiState(data) === 'RUNNING');
      this.isRunning = running;
      this.runType = data.run_type || 'self';

      const newMode = data.game_mode ? String(data.game_mode).toLowerCase() : null;
      this.activeModeKey = newMode;

      const nowWall = Date.now();
      // Snap epoch to whole second boundary
      const epochNow = Math.floor(nowWall / 1000) * 1000;
      this.epochNow = epochNow;

      const rawSuiteSec = Math.max(0, Number(data.elapsed_seconds) || 0);
      const rawModeSec = Math.max(0, Number(data.mode_elapsed_seconds != null ? data.mode_elapsed_seconds : data.elapsed_seconds) || 0);

      const unitData = data.unit_tests_data || {};
      const depthData = data.depth_data || {};
      const unitSec = (unitData.duration_sec && unitData.duration_sec > 0) ? Number(unitData.duration_sec) : (data.total_played > 0 ? 8.3 : 0);
      const depthSec = (depthData.duration_sec && depthData.duration_sec > 0) ? Number(depthData.duration_sec) : 0;

      const rawMatrixSec = (data.matrix_duration_sec != null)
        ? Math.max(0, Number(data.matrix_duration_sec))
        : ((data.matrix_elapsed_seconds != null)
            ? Math.max(0, Number(data.matrix_elapsed_seconds))
            : Math.max(0, rawSuiteSec - unitSec - depthSec));

      const poolData = data.pool_data || {};
      const rawPoolSec = (data.pool_duration_sec != null)
        ? Math.max(0, Number(data.pool_duration_sec))
        : (data.pool_elapsed_seconds != null
            ? Math.max(0, Number(data.pool_elapsed_seconds))
            : (poolData.duration_sec ? Number(poolData.duration_sec) : Math.max(0, rawSuiteSec - unitSec - depthSec - rawMatrixSec)));

      this.staticDurations = {
        suiteSec: Math.round(rawSuiteSec),
        modeSec: Math.round(rawModeSec),
        matrixSec: Math.round(rawMatrixSec),
        poolSec: Math.round(rawPoolSec),
      };

      if (running) {
        // Snap start references to multiples of 1000ms from the shared epochNow
        this.suiteStartMs = epochNow - (Math.round(rawSuiteSec) * 1000);
        this.modeStartMs = epochNow - (Math.round(rawModeSec) * 1000);
        this.matrixStartMs = epochNow - (Math.round(rawMatrixSec) * 1000);
        this.poolStartMs = epochNow - (Math.round(rawPoolSec) * 1000);

        if (data.eta_seconds != null && data.eta_seconds > 0) {
          this.totalEtaTargetMs = epochNow + (Math.round(Number(data.eta_seconds)) * 1000);
        } else {
          this.totalEtaTargetMs = null;
        }

        this.modeEtaTargets = {};
        if (Array.isArray(data.eta_breakdown)) {
          data.eta_breakdown.forEach(b => {
            if (b && b.mode && b.eta_seconds != null && b.eta_seconds > 0) {
              const mKey = b.mode.toLowerCase();
              this.modeEtaTargets[mKey] = epochNow + (Math.round(Number(b.eta_seconds)) * 1000);
            }
          });
        }
      } else {
        this.totalEtaTargetMs = null;
        this.modeEtaTargets = {};
      }

      this.forceRender = true;
      this.tick();
    },

    // Functionally evaluate snapshot from wall-clock timestamps
    getSnapshot(nowMs) {
      const t = nowMs || Date.now();
      if (!this.isRunning) {
        return {
          suiteElapsed: this.staticDurations.suiteSec,
          modeElapsed: this.staticDurations.modeSec,
          matrixElapsed: this.staticDurations.matrixSec,
          poolElapsed: this.staticDurations.poolSec,
          totalEta: null,
          modeEtas: {},
        };
      }

      // Snapped start timestamps ensure (t - startMs) crosses integer seconds on the exact same millisecond
      const suiteElapsed = Math.max(0, Math.floor((t - this.suiteStartMs) / 1000));
      const modeElapsed = Math.max(0, Math.floor((t - this.modeStartMs) / 1000));
      const matrixElapsed = Math.max(0, Math.floor((t - this.matrixStartMs) / 1000));
      const poolElapsed = Math.max(0, Math.floor((t - this.poolStartMs) / 1000));

      let totalEta = null;
      if (this.totalEtaTargetMs != null) {
        totalEta = Math.max(0, Math.ceil((this.totalEtaTargetMs - t) / 1000));
      }

      const modeEtas = {};
      for (const [mKey, targetMs] of Object.entries(this.modeEtaTargets)) {
        if (mKey === this.activeModeKey) {
          modeEtas[mKey] = Math.max(0, Math.ceil((targetMs - t) / 1000));
        } else {
          modeEtas[mKey] = Math.max(0, Math.round((targetMs - this.epochNow) / 1000));
        }
      }

      return {
        suiteElapsed,
        modeElapsed,
        matrixElapsed,
        poolElapsed,
        totalEta,
        modeEtas,
      };
    },

    // Centralized tick called at 100ms
    tick() {
      const nowMs = Date.now();
      const currentSecond = Math.floor(nowMs / 1000);
      if (!this.forceRender && currentSecond === this.lastTickSecond) {
        return;
      }
      this.lastTickSecond = currentSecond;
      this.forceRender = false;

      if (!this.latestData) return;
      const snapshot = this.getSnapshot(nowMs);
      this.renderAllTimers(this.latestData, snapshot);
    },

    renderAllTimers(data, snapshot) {
      const uiState = deriveUiState(data);
      const isRunning = (uiState === 'RUNNING');
      const isEmpty = (uiState === 'EMPTY');
      const isComplete = (uiState === 'COMPLETE');
      const isAborted = (uiState === 'ABORTED');
      const runType = this.runType;

      const statLineEl = document.getElementById('headerStatLine');
      const elItem = document.getElementById('headerStatItemElapsed');
      const leftItem = document.getElementById('headerStatItemLeft');
      const rateItem = document.getElementById('headerStatItemRate');
      const thItem = document.getElementById('headerStatItemThreads');
      const wrapEl = document.getElementById('headerProgressWrap');

      if (isEmpty) {
        if (statLineEl) statLineEl.style.display = 'flex';
        if (elItem) elItem.style.display = 'flex';
        if (leftItem) leftItem.style.display = 'flex';
        if (rateItem) rateItem.style.display = 'flex';
        if (thItem) thItem.style.display = 'flex';
        if (wrapEl) wrapEl.style.display = 'block';

        setText('headerStatElapsed', '--');
        setText('headerStatLeft', '--');
        setText('headerStatRate', '--');
        setText('threadsCount', '--');
        setWidth('headerProgressBar', 0);
        if (statLineEl) statLineEl.title = 'No active benchmark running';
        if (wrapEl) wrapEl.title = 'Overall Run Progress: --';
        return;
      }

      if (statLineEl) statLineEl.style.display = 'flex';
      if (elItem) elItem.style.display = 'flex';

      if (isAborted) {
        // ABORTED: Header keeps ELAPSED, RATE, THREADS
        if (leftItem) leftItem.style.display = 'none';
        if (rateItem) rateItem.style.display = 'flex';
        if (thItem) thItem.style.display = 'flex';
        if (wrapEl) wrapEl.style.display = 'none';
      } else if (isComplete) {
        // COMPLETE: all stats shown (ELAPSED, LEFT, RATE, THREADS)
        if (leftItem) leftItem.style.display = 'flex';
        if (rateItem) rateItem.style.display = 'flex';
        if (thItem) thItem.style.display = 'flex';
        if (wrapEl) wrapEl.style.display = 'block';
      } else {
        // RUNNING: all pills visible
        if (leftItem) leftItem.style.display = 'flex';
        if (rateItem) rateItem.style.display = 'flex';
        if (thItem) thItem.style.display = 'flex';
        if (wrapEl) wrapEl.style.display = 'block';
      }

      // 1. Header Stat Line: ELAPSED, LEFT, RATE
      const isIdle = (data.status === 'IDLE' || data.status === 'WAITING' || !data.status || (data.total_played === 0 && !isRunning && (snapshot.suiteElapsed || 0) === 0));
      const elapsedClock = formatElapsedClock(snapshot.suiteElapsed);
      setText('headerStatElapsed', elapsedClock);

      let leftStr = '--';
      if (isRunning && snapshot.totalEta != null && snapshot.totalEta > 0) {
        leftStr = formatEtaMinutes(snapshot.totalEta);
      }
      setText('headerStatLeft', leftStr);

      const played = data.total_played || 0;
      const pace = (isRunning && snapshot.modeElapsed > 0) ? (played / snapshot.modeElapsed) : 0;
      const rateGpm = pace * 60;
      const rateUnit = (runType === 'depth') ? 'pos/min' : 'games/min';
      const rateStr = isRunning ? `${rateGpm.toFixed(1)} ${rateUnit}` : (isEmpty ? '--' : `0.0 ${rateUnit}`);
      setText('headerStatRate', rateStr);

      if (statLineEl) {
        if (isIdle || (!isRunning && snapshot.suiteElapsed === 0)) {
          statLineEl.title = 'No active benchmark running';
        } else {
          statLineEl.title = `Total Suite: ${formatSeconds(snapshot.suiteElapsed)} | Current Mode: ${formatSeconds(snapshot.modeElapsed)}`;
        }
      }

      // 2. Thread Count beside Rate
      const thCount = (!isEmpty && data.threads != null && data.threads > 0) ? data.threads : (isRunning ? 0 : '--');
      setText('threadsCount', thCount);

      // 3. Header Slim Progress Bar (Percent = games completed / total planned games across entire suite)
      let totalPlannedGames = 0;
      let totalCompletedGames = 0;

      const selfMatrix = data.self_play_matrix || (runType === 'self' ? data.matrix_progress : null);
      if (selfMatrix && Array.isArray(selfMatrix) && selfMatrix.length > 0) {
        selfMatrix.forEach(m => {
          const isCurr = isRunning && (runType === 'self') && (this.activeModeKey === (m.mode || '').toLowerCase());
          const gVal = isCurr ? played : (m.games != null ? m.games : (m.total_played != null ? m.total_played : 0));
          const isCompleted = ['PASSED', 'PARITY', 'FAILED', 'KEEP', 'REVERT'].includes(m.status || m.decision) && gVal > 0;
          const maxG = isCompleted ? gVal : (m.max_games || 200);
          totalPlannedGames += maxG;
          totalCompletedGames += gVal;
        });
      }

      if (runType === 'pool') {
        const poolMatrix = data.matrix_progress || [];
        if (poolMatrix.length > 0) {
          poolMatrix.forEach(m => {
            const isCurr = isRunning && (this.activeModeKey === (m.mode || '').toLowerCase());
            const gVal = isCurr ? played : (m.games != null ? m.games : (m.cand_games != null ? m.cand_games : 0));
            const isCompleted = ['PASSED', 'PARITY', 'FAILED', 'KEEP', 'REVERT'].includes(m.status || m.decision) && gVal > 0;
            const maxG = isCompleted ? gVal : (m.max_games || 50);
            totalPlannedGames += maxG;
            totalCompletedGames += gVal;
          });
        } else {
          totalPlannedGames += (data.target_games || data.max_games || 50);
          totalCompletedGames += played;
        }
      } else if (runType === 'self') {
        const activeCount = (selfMatrix && selfMatrix.length > 0) ? selfMatrix.length : 4;
        totalPlannedGames += (activeCount * 50);
      } else if (runType === 'depth' && data.depth_data) {
        totalPlannedGames = data.depth_data.total_positions || 12;
        totalCompletedGames = data.depth_data.completed_positions || 0;
      }

      // Time-based progress: elapsed / (elapsed + left)
      const isFinished = (data.status === 'FINISHED' || data.stop_type === 'MATRIX_COMPLETE' || data.stop_type === 'PARITY' || data.stop_type === 'KEEP' || data.status === 'DONE' || data.status === 'PASS');
      const suiteElapsed = snapshot.suiteElapsed || 0;
      const totalEta = (snapshot.totalEta != null && snapshot.totalEta > 0) ? snapshot.totalEta : (snapshot.totalEta === 0 ? 0 : null);

      let runProgressPct = 0;
      if (isFinished) {
        runProgressPct = 100;
      } else if (suiteElapsed > 0) {
        if (totalEta != null && totalEta > 0) {
          const totalEstimatedTime = suiteElapsed + totalEta;
          runProgressPct = totalEstimatedTime > 0
            ? Math.min(100, Math.max(0, (suiteElapsed / totalEstimatedTime) * 100)).toFixed(1)
            : 0;
        } else if (totalEta === 0) {
          runProgressPct = 100;
        } else if (totalPlannedGames > 0 && totalCompletedGames > 0) {
          runProgressPct = Math.min(100, Math.max(0, (totalCompletedGames / totalPlannedGames) * 100)).toFixed(1);
        }
      }

      setWidth('headerProgressBar', runProgressPct);
      if (wrapEl) {
        if (totalEta != null && totalEta > 0) {
          wrapEl.title = `Overall Run Progress: ${runProgressPct}% (${formatSeconds(suiteElapsed)} / ${formatSeconds(suiteElapsed + totalEta)}) • ${totalCompletedGames} / ${totalPlannedGames} Games`;
        } else {
          wrapEl.title = `Overall Run Progress: ${runProgressPct}% (${formatSeconds(suiteElapsed)}) • ${totalCompletedGames} / ${totalPlannedGames} Games`;
        }
      }

      // 3. Stage Rail Duration Displays for Running Stage
      if (isRunning) {
        if (runType === 'self') {
          const selfDurEl = document.getElementById('railDuration-self');
          if (selfDurEl) selfDurEl.innerText = formatDuration(snapshot.matrixElapsed);
        } else if (runType === 'depth') {
          const depthDurEl = document.getElementById('railDuration-depth');
          if (depthDurEl) depthDurEl.innerText = formatDuration(snapshot.suiteElapsed);
        } else if (runType === 'pool') {
          const poolDurEl = document.getElementById('railDuration-pool');
          if (poolDurEl) poolDurEl.innerText = formatDuration(snapshot.poolElapsed);
        } else {
          const unitDurEl = document.getElementById('railDuration-unit');
          if (unitDurEl) unitDurEl.innerText = formatDuration(snapshot.suiteElapsed, true);
        }
      }

      // 4. Self-Play Matrix & Zoo Pool Table Active Mode Row Time (Current running duration)
      if (isRunning && this.activeModeKey) {
        const activeEtaEl = runType === 'pool'
          ? document.getElementById(`pool-eta-${this.activeModeKey}`)
          : document.getElementById(`matrix-eta-${this.activeModeKey}`);
        if (activeEtaEl) {
          activeEtaEl.innerText = formatDuration(snapshot.modeElapsed);
        }
      }

      // 5. Runs Card Subtext (Compatibility / when visible)
      const subEl = document.getElementById('runsSubtext');
      if (subEl && isRunning) {
        const played = data.total_played || 0;
        const maxG = data.target_games || data.max_games || 200;
        const pct = maxG > 0 ? Math.min(100, Math.round((played / maxG) * 100)) : 0;
        subEl.innerText = `${pct}% Complete`;
      }
    }
  };

  let lastHistoryJSON = '';
  let isUpdating = false;

  let activeStage = 'self';
  let userSelectedStage = false;
  let lastSeenRunType = null;
  let lastSeenDesc = null;

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

  function formatCompactNps(val) {
    if (val == null || val <= 0) return '--';
    const num = Number(val);
    if (num >= 1000000) return `${(num / 1000000).toFixed(2)}M`;
    if (num >= 1000) {
      const k = num / 1000;
      return k < 100 ? `${k.toFixed(1)}k` : `${Math.round(k)}k`;
    }
    return String(Math.round(num));
  }

  function computeModeFixtureNps(depthData) {
    const fixtures = (depthData && depthData.fixtures) ? depthData.fixtures : [];
    const modeFixtures = {};
    fixtures.forEach(f => {
      const m = String(f.mode || '').trim().toLowerCase().replace(' 4p', '').replace(' 1v1', '');
      if (m) {
        if (!modeFixtures[m]) modeFixtures[m] = [];
        modeFixtures[m].push(f);
      }
    });
    const result = {};
    Object.keys(modeFixtures).forEach(m => {
      const fixs = modeFixtures[m];
      const bList = fixs.map(f => Number(f.base_med_nps)).filter(v => !isNaN(v)).sort((a, b) => a - b);
      const vList = fixs.map(f => Number(f.var_med_nps)).filter(v => !isNaN(v)).sort((a, b) => a - b);
      const median = (arr) => {
        if (!arr.length) return 0.0;
        const mid = Math.floor(arr.length / 2);
        return (arr.length % 2 !== 0) ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2.0;
      };
      result[m] = { base_med_nps: median(bList), var_med_nps: median(vList) };
    });
    return result;
  }

  function toggleDepthTable() {}

  function updateDepthUI(data) {
    const uiState = deriveUiState(data);
    const isRunning = (uiState === 'RUNNING');
    const isEmpty = (uiState === 'EMPTY');
    const depth = data.depth_data || {};
    const fixtures = depth.fixtures || [];
    let totalDelta = 0.0;
    if (depth.total_delta_depth != null) {
      totalDelta = Number(depth.total_delta_depth);
    } else if (fixtures.length > 0) {
      totalDelta = fixtures.reduce((acc, f) => {
        const d = (f.delta_depth != null) ? f.delta_depth : ((f.var_med_depth || 0) - (f.base_med_depth || 0));
        return acc + Number(d);
      }, 0.0);
    } else if (depth.delta_depth != null) {
      totalDelta = Number(depth.delta_depth);
    }
    totalDelta = Math.round(totalDelta * 10) / 10;

    const varD = (depth.median_var_depth != null) ? depth.median_var_depth : 0.0;
    const baseD = (depth.median_base_depth != null) ? depth.median_base_depth : 0.0;
    const npsR = (depth.nps_ratio != null) ? depth.nps_ratio : 1.0;
    const agree = (depth.agreement_pct != null) ? depth.agreement_pct : 100.0;



    // Compatibility test elements
    setText('depthVarValue', varD.toFixed(1));
    setText('depthBaseValue', baseD.toFixed(1));
    setText('depthNpsRatioValue', `${npsR.toFixed(2)}x`);
    setText('depthAgreeValue', `${agree.toFixed(1)}%`);

    // Per-mode breakdown table from mode_summaries (exactly 4 rows)
    const tbody = document.getElementById('depthTableBody');
    if (tbody) {
      const modeSummaries = depth.mode_summaries || {};
      if (Object.keys(modeSummaries).length === 0) {
        if (isEmpty) {
          const modeOrder = [
            { key: 'constrictor', name: 'Constrictor 4P' },
            { key: 'royale', name: 'Royale 4P' },
            { key: 'duel', name: 'Duel 1v1' },
            { key: 'standard', name: 'Standard 4P' }
          ];
          const rowsHtml = modeOrder.map(m => `
            <tr>
              <td class="td-left" style="font-weight: 700;">${m.name}</td>
              <td class="td-right">--</td>
              <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">--</td>
              <td class="td-right" style="color: var(--pipeline-text-muted); font-weight: 700;">--</td>
              <td class="td-right">--</td>
              <td class="td-right">--</td>
              <td class="td-right" style="font-weight: 700; color: var(--pipeline-text-muted);">--</td>
              <td class="td-right">--</td>
              <td class="td-right"><span class="status-chip status-chip-queued matrix-col-status matrix-status-queued">QUEUED</span></td>
            </tr>
          `).join('');
          setHTML(tbody, rowsHtml);
        } else {
          const emptyMsg = isRunning ? 'Running benchmark...' : 'Benchmark queued.';
          setHTML(tbody, `<tr><td colspan="9" class="empty-cell">${emptyMsg}</td></tr>`);
        }
      } else {
        const modeOrder = [
          { key: 'constrictor', name: 'Constrictor 4P' },
          { key: 'royale', name: 'Royale 4P' },
          { key: 'duel', name: 'Duel 1v1' },
          { key: 'standard', name: 'Standard 4P' }
        ];
        const fixtureNpsMap = computeModeFixtureNps(depth);
        const rowsHtml = modeOrder.map(m => {
          const s = modeSummaries[m.key] || {};
          const baseD = s.base_med_depth != null ? Number(s.base_med_depth) : 0.0;
          const varD = s.var_med_depth != null ? Number(s.var_med_depth) : 0.0;
          const deltaD = s.delta_depth != null ? Number(s.delta_depth) : (varD - baseD);
          const npsR = s.nps_ratio != null ? Number(s.nps_ratio) : 1.0;
          const agree = s.agreement_pct != null ? Number(s.agreement_pct) : 100.0;

          const modeNps = fixtureNpsMap[m.key] || {};
          const baseNps = modeNps.base_med_nps != null ? modeNps.base_med_nps : (s.base_med_nps || 0);
          const varNps = modeNps.var_med_nps != null ? modeNps.var_med_nps : (s.var_med_nps || 0);
          const baseNpsCompact = formatCompactNps(baseNps);
          const varNpsCompact = formatCompactNps(varNps);
          const baseNpsRaw = Math.round(baseNps);
          const varNpsRaw = Math.round(varNps);

          const isReg = Boolean(s.regression || s.nps_regressed);
          let npsColor;
          if (isReg) {
            npsColor = 'var(--pipeline-red)';
          } else if (npsR < 1.0) {
            npsColor = 'var(--pipeline-amber)';
          } else {
            npsColor = 'var(--pipeline-green)';
          }

          const isDepthReg = Boolean(s.depth_regressed);
          let deltaColor;
          if (isDepthReg) {
            deltaColor = 'var(--pipeline-red)';
          } else if (deltaD > 0) {
            deltaColor = 'var(--pipeline-green)';
          } else {
            deltaColor = 'var(--pipeline-text-muted)';
          }

          let stClass = 'equal';
          let stText = 'EQUAL';
          if (isReg || isDepthReg) {
            stClass = 'worse';
            stText = 'WORSE';
          } else if (deltaD > 0) {
            stClass = 'better';
            stText = 'BETTER';
          }
          const sign = deltaD > 0 ? '+' : '';
          return `
            <tr>
              <td class="td-left" style="font-weight: 700;">${m.name}</td>
              <td class="td-right">${baseD.toFixed(1)}</td>
              <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">${varD.toFixed(1)}</td>
              <td class="td-right" style="color: ${deltaColor}; font-weight: 700;">${sign}${deltaD.toFixed(1)}</td>
              <td class="td-right" title="${baseNpsRaw.toLocaleString()}" data-nps="${baseNpsRaw}">${baseNpsCompact}<span class="raw-nps" style="display:none;">${baseNpsRaw}</span></td>
              <td class="td-right" title="${varNpsRaw.toLocaleString()}" data-nps="${varNpsRaw}">${varNpsCompact}<span class="raw-nps" style="display:none;">${varNpsRaw}</span></td>
              <td class="td-right" style="color: ${npsColor}; font-weight: 700;">${npsR.toFixed(3)}x</td>
              <td class="td-right">${agree.toFixed(1)}%</td>
              <td class="td-right"><span class="status-chip status-chip-${stClass} matrix-col-status matrix-status-${stClass}">${stText}</span></td>
            </tr>
          `;
        }).join('');
        setHTML(tbody, rowsHtml);
      }
    }
  }

  function updatePoolUI(data) {
    const uiState = deriveUiState(data);
    const isRunning = (uiState === 'RUNNING');
    const isEmpty = (uiState === 'EMPTY');
    const pool = data.pool_data || {};
    const poolModesBody = document.getElementById('poolModesTableBody');

    const baseModes = [
      { mode: 'constrictor', name: 'Constrictor 4P', max_games: 200 },
      { mode: 'royale', name: 'Royale 4P', max_games: 200 },
      { mode: 'duel', name: 'Duel 1v1', max_games: 200 },
      { mode: 'standard', name: 'Standard 4P', max_games: 200 },
    ];

    if (poolModesBody) {
      let rawModes = pool.mode_results || pool.modes || pool.mode_stats || {};
      const modeDataMap = {};
      if (Array.isArray(rawModes)) {
        rawModes.forEach(item => {
          if (item && item.mode) modeDataMap[item.mode.toLowerCase()] = item;
        });
      } else if (rawModes && typeof rawModes === 'object') {
        Object.keys(rawModes).forEach(k => {
          modeDataMap[k.toLowerCase()] = rawModes[k];
        });
      }

      const matrixList = (data.matrix_progress && data.matrix_progress.length > 0) ? data.matrix_progress : [];
      const modeMap = {};
      matrixList.forEach(m => { if (m.mode) modeMap[m.mode.toLowerCase()] = m; });

      const overallDelta = pool.delta != null ? pool.delta : ((pool.cand_share || 0) - (pool.base_share || 0));
      const overallZ = pool.z_score != null ? pool.z_score : 0.0;
      const isPoolRunning = (data.run_type === 'pool' && isRunning);
      const activeModeClean = (data.game_mode || '').toLowerCase();

      let completedPoolModes = 0;
      let totalPoolPlayed = 0;
      let totalPoolMax = 0;
      let poolModesCount = 0;

      const rowsHtml = baseModes.map(bm => {
        const modeKey = bm.mode.toLowerCase();
        const md = modeDataMap[modeKey] || {};
        const m = modeMap[modeKey] || {};
        const isCurrentActive = isPoolRunning && (activeModeClean === modeKey);

        const isOmitted = (matrixList.length > 0 && !modeMap[modeKey] && !modeDataMap[modeKey]);
        const isExplicitSkipped = (m.status === 'SKIPPED' || md.status === 'SKIPPED' || m.decision === 'SKIPPED' || md.decision === 'SKIPPED');
        const isSkipped = isOmitted || isExplicitSkipped;

        const candShare = isOmitted ? null : (md.cand_share != null ? md.cand_share : (isCurrentActive ? (pool.cand_share || 0) : null));
        const baseShare = isOmitted ? null : (md.base_share != null ? md.base_share : (isCurrentActive ? (pool.base_share || 0) : null));
        const delta = isOmitted ? null : (md.delta != null ? md.delta : (candShare != null && baseShare != null ? (candShare - baseShare) : null));
        const z = isOmitted ? null : (md.z_score != null ? md.z_score : (isCurrentActive ? overallZ : null));

        const defaultPoolMax = pool.max_games || (data.run_type === 'pool' ? (data.target_games || 40) : 40);
        const maxGames = md.max_games || defaultPoolMax;
        const candGames = md.cand_games != null ? md.cand_games : (isCurrentActive ? (pool.cand_games || data.total_played || 0) : (md.games != null ? md.games : 0));
        const baseGames = md.base_games != null ? md.base_games : (isCurrentActive ? (pool.base_games || data.total_played || 0) : (md.games != null ? md.games : 0));
        const gamesActive = Math.max(candGames, baseGames);

        let statusClass = 'queued';
        let statusText = 'QUEUED';
        if (isCurrentActive) {
          statusClass = 'running';
          statusText = 'RUNNING';
        } else if (isSkipped) {
          statusClass = 'queued';
          statusText = 'QUEUED';
        } else if (gamesActive > 0) {
          const zNum = Number(z != null ? z : 0);
          const dNum = Number(delta != null ? delta : 0);
          if (dNum > 0 && zNum >= 1.645) {
            statusText = 'BETTER';
            statusClass = 'better';
            completedPoolModes++;
          } else if (zNum <= -1.645 || dNum < -0.5) {
            statusText = 'WORSE';
            statusClass = 'worse';
          } else {
            statusText = 'EQUAL';
            statusClass = 'equal';
            completedPoolModes++;
          }
        }

        // Data presence rule: games == 0 is QUEUED, never render zero-filled defaults
        if (gamesActive === 0 && !isCurrentActive) {
          if (!isSkipped) {
            statusText = 'QUEUED';
            statusClass = 'queued';
          }
        }

        // 1. Mode: Name only
        // 2. Games: "cand / max · base / max" (or "--" if skipped without games)
        const isCompleted = ['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED', 'KEEP', 'REVERT'].includes(statusText) && gamesActive > 0;
        const effMaxGames = isCompleted ? gamesActive : maxGames;
        if (!isSkipped) {
          poolModesCount++;
          totalPoolPlayed += gamesActive;
          totalPoolMax += effMaxGames;
        }
        const gamesText = (gamesActive > 0)
          ? `${candGames} / ${effMaxGames} · ${baseGames} / ${effMaxGames}`
          : (isSkipped ? '--' : `0 / ${maxGames} · 0 / ${maxGames}`);

        // 3. Win Share: combined "Var: variant% · Base: baseline%" with clear labels
        const winShareHtml = (gamesActive > 0 && candShare != null && baseShare != null)
          ? `<span style="font-weight: 700; color: var(--pipeline-green);">Var: ${candShare.toFixed(1)}%</span> <span style="color: var(--pipeline-text-muted);">·</span> <span style="font-weight: 700; color: var(--pipeline-blue);">Base: ${baseShare.toFixed(1)}%</span>`
          : `<span style="color: var(--pipeline-text-muted);">--</span>`;

        // 4. Delta: variant minus baseline
        const deltaColor = delta > 0.5 ? 'var(--pipeline-green)' : (delta < -0.5 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        const deltaText = (gamesActive > 0 && delta != null) ? `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%` : '--';

        // 5. Z-Score
        let zScoreText = '--';
        let zColor = 'var(--pipeline-text-muted)';
        if (gamesActive >= 5 && z != null) {
          const zNum = Number(z);
          zScoreText = `${zNum >= 0 ? '+' : ''}${zNum.toFixed(2)}`;
          zColor = zNum >= 1.645 ? 'var(--pipeline-green)' : (zNum <= -1.645 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        }

        // 6. Time (Finished: duration, Running: current duration, Not started: "--")
        let etaText = '--';
        const dur = md.duration_sec;
        if (isSkipped) {
          etaText = '--';
        } else if (isCurrentActive) {
          const unitData = data.unit_tests_data || {};
          const unitSec = (unitData && unitData.duration_sec && unitData.duration_sec > 0) ? unitData.duration_sec : 0;
          const depth = data.depth_data || {};
          const depthSec = (depth && depth.duration_sec && depth.duration_sec > 0) ? depth.duration_sec : 0;
          const poolElapsed = (data.mode_elapsed_seconds != null && data.mode_elapsed_seconds > 0)
            ? data.mode_elapsed_seconds
            : (dur != null && dur > 0 ? dur : Math.max(0, (data.elapsed_seconds || 0) - unitSec - depthSec));
          etaText = (poolElapsed != null && poolElapsed > 0) ? formatDuration(poolElapsed) : '0:00';
        } else if (['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED', 'KEEP', 'REVERT'].includes(statusText) || dur != null) {
          etaText = (dur != null && dur > 0) ? formatDuration(dur) : '--';
        } else {
          etaText = '--';
        }

        const rowClass = isCurrentActive ? 'active-running' : '';

        const candText = (gamesActive > 0 && candShare != null) ? `${candShare.toFixed(1)}%` : '--';
        const baseText = (gamesActive > 0 && baseShare != null) ? `${baseShare.toFixed(1)}%` : '--';

        return `
          <tr class="${rowClass}">
            <td class="td-left" style="font-weight: 700; color: var(--pipeline-text-main);">${bm.name}</td>
            <td class="td-right" style="font-weight: 700;">${gamesText}</td>
            <td class="td-right" style="font-weight: 700; color: var(--pipeline-green);">${candText}</td>
            <td class="td-right" style="font-weight: 700; color: var(--pipeline-blue);">${baseText}</td>
            <td class="td-right" style="font-weight: 700; color: ${(gamesActive > 0 && delta != null) ? deltaColor : 'var(--pipeline-text-muted)'};">${deltaText}</td>
            <td class="td-right" style="font-weight: 600; color: ${zColor};">${zScoreText}</td>
            <td class="td-right"><span class="status-chip status-chip-${statusClass} matrix-col-status matrix-status-${statusClass}">${statusText}</span></td>
          </tr>
        `;
      }).join('');

      setHTML(poolModesBody, rowsHtml);

      const hasPoolGames = (pool.total_games && pool.total_games > 0) || isPoolRunning || (totalPoolPlayed > 0);
      if (!hasPoolGames && !isEmpty) {
        setHTML(poolModesBody, '<tr><td colspan="7" class="empty-cell" style="padding: 1.5rem; color: var(--pipeline-text-muted);">No pool games yet</td></tr>');
      }
    }

  }

  function updateStageRail(data) {
    const runType = data.run_type || 'self';
    const uiState = deriveUiState(data);
    const isRunning = (uiState === 'RUNNING');
    const isEmpty = (uiState === 'EMPTY');
    const isComplete = (uiState === 'COMPLETE');
    const isAborted = (uiState === 'ABORTED');
    const interruptedStage = isAborted ? determineInterruptedStage(data) : null;
    const rec = (data.recommendation || 'PENDING').toUpperCase();

    function setRailSeg(id, status, duration) {
      const seg = document.getElementById(`railSegment-${id}`);
      const st = document.getElementById(`railStatus-${id}`);
      const dur = document.getElementById(`railDuration-${id}`);
      let statusLower = status.toLowerCase().replace(/ /g, '-');
      if (statusLower === 'pass') statusLower = 'passed';

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

    // Helper to derive depth status: WORSE, BETTER, or EQUAL
    function deriveDepthStatus(d) {
      if (d.regression === true || d.passed === false) return 'WORSE';
      const deltaD = d.delta_depth != null ? Number(d.delta_depth) : (d.overall_delta_d != null ? Number(d.overall_delta_d) : 0);
      return deltaD > 0 ? 'BETTER' : 'EQUAL';
    }

    // Helper to derive matrix status: WORSE, BETTER, or EQUAL
    function deriveSelfStatus() {
      if (interruptedStage === 'self') {
        if (rec === 'REVERT' || rec === 'REJECT' || rec === 'WORSE') return 'WORSE';
        const sType = (data.stop_type || '').toUpperCase();
        if (['REVERT', 'FAILED', 'DISCONNECTED'].includes(sType)) return 'WORSE';
      }
      const failedModes = matrixList.filter(m => ['FAILED', 'WORSE', 'REVERT'].includes(m.status)).length;
      if (failedModes > 0) return 'WORSE';
      const betterModes = matrixList.filter(m => ['BETTER', 'PASSED', 'KEEP'].includes(m.status)).length;
      const equalModes = matrixList.filter(m => ['EQUAL', 'PARITY'].includes(m.status)).length;
      if (betterModes > 0 && equalModes === 0) return 'BETTER';
      if (equalModes > 0 || completedModes > 0) return 'EQUAL';
      if (rec === 'KEEP' || rec === 'BETTER') return 'BETTER';
      if (rec === 'PARITY' || rec === 'EQUAL') return 'EQUAL';
      return 'EQUAL';
    }

    // Helper to derive pool status: WORSE, BETTER, or EQUAL
    function derivePoolStatus() {
      if (rec === 'REVERT' || rec === 'REJECT' || rec === 'WORSE') return 'WORSE';
      const pDec = (pool.decision || '').toUpperCase();
      if (['FAILED', 'REVERT', 'WORSE'].includes(pDec)) return 'WORSE';
      if (['KEEP', 'BETTER'].includes(pDec) || ['KEEP', 'BETTER'].includes(rec)) return 'BETTER';
      if (['PARITY', 'EQUAL'].includes(pDec) || ['PARITY', 'EQUAL'].includes(rec)) return 'EQUAL';
      const pDelta = pool.delta != null ? pool.delta : ((pool.cand_share || 0) - (pool.base_share || 0));
      const pZ = pool.z_score != null ? pool.z_score : 0.0;
      if (pDelta > 0 && pZ >= 1.645) return 'BETTER';
      if (pDelta >= 0 || pZ > -1.645) return 'EQUAL';
      return 'WORSE';
    }

    // Stage 1: Tactical Unit Tests
    const unitData = data.unit_tests_data || {};
    let unitStatus = 'QUEUED';
    let unitDur = (unitData.duration_sec && unitData.duration_sec > 0) ? formatDuration(unitData.duration_sec, true) : '--';

    if (isEmpty) {
      unitStatus = 'QUEUED';
    } else if (data.stop_type === 'TEST_FAILURE' || unitData.status === 'FAILED' || (isAborted && interruptedStage === 'unit')) {
      unitStatus = 'WORSE';
      autoSelectStage('unit');
    } else if (unitData.status === 'PASSED' || unitData.status === 'DONE' || unitData.status === 'PASS' || (data.total_played > 0) || (isComplete && data.stop_type !== 'TEST_FAILURE')) {
      unitStatus = 'EQUAL';
      if (!unitData.duration_sec || unitData.duration_sec <= 0) {
        unitDur = '8.3s';
      }
    } else if (unitData.status === 'RUNNING' || (isRunning && data.total_played === 0 && !depth.completed_positions)) {
      unitStatus = 'RUNNING';
      unitDur = formatDuration(data.elapsed_seconds || 0, true);
      autoSelectStage('unit');
    } else {
      unitStatus = 'QUEUED';
    }
    setRailSeg('unit', unitStatus, unitDur);

    // Update Unit Tests Detail (Requirement 2)
    const unitListEl = document.getElementById('unitTestsList');

    if (isEmpty) {
      if (unitListEl) {
        unitListEl.innerHTML = renderUnitTestsList([]);
      }
    } else {
      if (unitListEl && (unitData.tests || unitStatus === 'DONE' || unitStatus === 'PASS' || unitStatus === 'EQUAL' || unitStatus === 'FAILED' || unitStatus === 'WORSE')) {
        unitListEl.innerHTML = renderUnitTestsList(unitData.tests || []);
      }
    }

    // Stage 2: Depth Benchmark
    const depth = data.depth_data || {};
    let depthStatus = 'QUEUED';
    let depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : '--';

    if (isEmpty) {
      depthStatus = 'QUEUED';
    } else if (isAborted && interruptedStage === 'depth') {
      depthStatus = 'WORSE';
      autoSelectStage('depth');
    } else if (depth.completed_positions && depth.completed_positions > 0) {
      depthStatus = deriveDepthStatus(depth);
      depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : '--';
    } else if (runType === 'depth') {
      if (isRunning) {
        depthStatus = 'RUNNING';
        depthDur = formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('depth');
      } else if (isComplete) {
        depthStatus = deriveDepthStatus(depth);
        depthDur = depth.duration_sec ? formatSeconds(depth.duration_sec) : formatSeconds(data.elapsed_seconds || 0);
        autoSelectStage('depth');
      } else {
        depthStatus = 'QUEUED';
      }
    } else if (isRunning && (unitStatus === 'DONE' || unitStatus === 'PASS' || unitStatus === 'EQUAL') && (!data.total_played || data.total_played === 0) && (!depth.completed_positions || depth.completed_positions < 12)) {
      depthStatus = 'RUNNING';
      depthDur = formatSeconds(data.elapsed_seconds || 0);
    } else if (isComplete && (depth.completed_positions >= 12 || depth.duration_sec)) {
      depthStatus = deriveDepthStatus(depth);
    } else {
      depthStatus = 'QUEUED';
    }
    setRailSeg('depth', depthStatus, depthDur);

    // Stage 3: Self-Play Matrix
    let selfStatus = 'QUEUED';
    const matrixList = (runType === 'pool') ? (data.self_play_matrix || []) : (data.matrix_progress || []);
    const hasSelfProgress = (data.self_play_matrix && data.self_play_matrix.length > 0) || (runType === 'self' && (data.matrix_progress && data.matrix_progress.length > 0));
    const completedModes = matrixList.filter(m => ['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED', 'SKIPPED'].includes(m.status)).length;
    let selfDur = '--';
    const sumMatrixSec = matrixList.reduce((acc, m) => acc + (m.duration_sec || 0), 0);

    const unitTestSec = (unitData && unitData.duration_sec && unitData.duration_sec > 0) ? unitData.duration_sec : ((unitStatus === 'DONE' || unitStatus === 'PASS' || unitStatus === 'EQUAL') ? 8.3 : 0);
    const depthSec = (depth && depth.duration_sec && depth.duration_sec > 0) ? depth.duration_sec : 0;
    const computedMatrixElapsed = (data.matrix_duration_sec != null && data.matrix_duration_sec > 0)
      ? data.matrix_duration_sec
      : (data.matrix_elapsed_seconds != null ? data.matrix_elapsed_seconds : Math.max(0, (data.elapsed_seconds || 0) - unitTestSec - depthSec));

    if (isEmpty) {
      selfStatus = 'QUEUED';
    } else if (isAborted && interruptedStage === 'self') {
      selfStatus = 'WORSE';
      autoSelectStage('self');
    } else if (isAborted && interruptedStage === 'pool' && (completedModes > 0 || hasSelfProgress)) {
      selfStatus = deriveSelfStatus();
      const finalSec = (data.matrix_duration_sec != null && data.matrix_duration_sec > 0)
        ? data.matrix_duration_sec
        : (sumMatrixSec > 0 ? sumMatrixSec : computedMatrixElapsed);
      selfDur = formatSeconds(finalSec);
    } else if (runType === 'self') {
      if (isRunning) {
        selfStatus = 'RUNNING';
        selfDur = formatSeconds(computedMatrixElapsed);
        autoSelectStage('self');
      } else if (isComplete || completedModes > 0 || hasSelfProgress) {
        selfStatus = deriveSelfStatus();
        const finalSec = (data.matrix_duration_sec != null && data.matrix_duration_sec > 0)
          ? data.matrix_duration_sec
          : (sumMatrixSec > 0 ? sumMatrixSec : computedMatrixElapsed);
        selfDur = formatSeconds(finalSec);
        if (isComplete) autoSelectStage('self');
      } else {
        selfStatus = 'QUEUED';
      }
    } else if (runType === 'pool') {
      if (data.matrix_duration_sec != null && data.matrix_duration_sec > 0) {
        selfStatus = deriveSelfStatus();
        selfDur = formatSeconds(data.matrix_duration_sec);
      } else if (hasSelfProgress && sumMatrixSec > 0) {
        selfStatus = deriveSelfStatus();
        selfDur = formatSeconds(sumMatrixSec);
      } else {
        selfStatus = 'SKIPPED';
        selfDur = '--';
      }
    } else if ((isComplete || isAborted) && completedModes > 0) {
      selfStatus = deriveSelfStatus();
      selfDur = formatSeconds(data.matrix_duration_sec != null ? data.matrix_duration_sec : (sumMatrixSec > 0 ? sumMatrixSec : computedMatrixElapsed));
    } else if (isRunning && (unitStatus === 'DONE' || unitStatus === 'PASS' || unitStatus === 'EQUAL') && (!depth.duration_sec || depthStatus === 'DONE' || depthStatus === 'PASS' || depthStatus === 'PARITY' || depthStatus === 'BETTER' || depthStatus === 'EQUAL') && (completedModes < matrixList.length)) {
      selfStatus = 'RUNNING';
      selfDur = formatSeconds(computedMatrixElapsed);
    } else {
      selfStatus = 'QUEUED';
    }
    setRailSeg('self', selfStatus, selfDur);

    // Stage 4: Zoo Pool Gate
    const pool = data.pool_data || {};
    let poolStatus = 'QUEUED';
    const poolDurSec = (data.pool_duration_sec != null && data.pool_duration_sec > 0)
      ? data.pool_duration_sec
      : (pool.duration_sec && pool.duration_sec > 0 ? pool.duration_sec : null);
    let poolDur = poolDurSec ? formatSeconds(poolDurSec) : '--';

    if (isEmpty) {
      poolStatus = 'QUEUED';
    } else if (isAborted && interruptedStage === 'pool') {
      poolStatus = 'WORSE';
      autoSelectStage('pool');
    } else if (pool.total_games && pool.total_games > 0) {
      if (isRunning && pool.total_games < (pool.max_games || 160)) {
        poolStatus = 'RUNNING';
        const poolElapsed = poolDurSec || Math.max(0, (data.elapsed_seconds || 0) - unitTestSec - depthSec - (data.matrix_duration_sec || 0));
        poolDur = formatSeconds(poolElapsed);
        autoSelectStage('pool');
      } else {
        poolStatus = derivePoolStatus();
        poolDur = poolDurSec ? formatSeconds(poolDurSec) : '--';
      }
    } else if (isRunning && (matrixList.length > 0 && completedModes >= matrixList.length)) {
      poolStatus = 'RUNNING';
      const poolElapsed = poolDurSec || Math.max(0, (data.elapsed_seconds || 0) - unitTestSec - depthSec - (data.matrix_duration_sec || 0));
      poolDur = formatSeconds(poolElapsed);
      autoSelectStage('pool');
    } else {
      poolStatus = 'QUEUED';
    }
    setRailSeg('pool', poolStatus, poolDur);

    // Strict pipeline order invariant while running:
    if (isRunning) {
      if (unitStatus === 'RUNNING' || unitStatus === 'QUEUED') {
        depthStatus = 'QUEUED';
        depthDur = '--';
        selfStatus = 'QUEUED';
        selfDur = '--';
        poolStatus = 'QUEUED';
        poolDur = '--';
      } else if (depthStatus === 'RUNNING') {
        selfStatus = 'QUEUED';
        selfDur = '--';
        poolStatus = 'QUEUED';
        poolDur = '--';
      } else if (selfStatus === 'RUNNING') {
        poolStatus = 'QUEUED';
        poolDur = '--';
      }
      setRailSeg('depth', depthStatus, depthDur);
      setRailSeg('self', selfStatus, selfDur);
      setRailSeg('pool', poolStatus, poolDur);
    }

    // Update Stage Card Bodies across all 4 stages
    updateStageCardBody('unit', uiState, interruptedStage === 'unit', data.stop_reason);
    updateStageCardBody('depth', uiState, interruptedStage === 'depth', data.stop_reason);
    updateStageCardBody('matrix', uiState, interruptedStage === 'self', data.stop_reason);
    updateStageCardBody('pool', uiState, interruptedStage === 'pool', data.stop_reason);

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

  function updateMatrixUI(data) {
    const tableBody = document.getElementById('matrixTableBody');
    if (!tableBody) return;

    const uiState = deriveUiState(data);
    const isRunning = (uiState === 'RUNNING');
    const isEmpty = (uiState === 'EMPTY');

    const baseModes = [
      { mode: 'constrictor', name: 'Constrictor 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
      { mode: 'royale', name: 'Royale 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
      { mode: 'duel', name: 'Duel 1v1', target_share: 50.0, max_games: 200, num_snakes: 2 },
      { mode: 'standard', name: 'Standard 4P', target_share: 25.0, max_games: 200, num_snakes: 4 },
    ];

    const matrixList = (data.run_type === 'pool')
      ? (data.self_play_matrix || [])
      : ((data.matrix_progress && data.matrix_progress.length > 0) ? data.matrix_progress : []);
    const modeMap = {};
    matrixList.forEach(m => { if (m.mode) modeMap[m.mode.toLowerCase()] = m; });

    const isSelfRunning = (data.run_type === 'self' && isRunning);
    const activeModeClean = (data.game_mode || '').toLowerCase();

    const played = data.total_played || 0;
    const is4p = data.num_snakes === 4;
    const drawShare = is4p ? 0.25 : 0.5;
    const varW = (data.variant && data.variant.wins != null) ? data.variant.wins : 0;
    const drawsCount = data.draws ? data.draws.count : ((data.baseline && data.baseline.draws != null) ? data.baseline.draws : 0);
    const varPts = (data.variant && typeof data.variant.points === 'number') ? data.variant.points : (varW + drawShare * drawsCount);
    const varWS = (data.variant && typeof data.variant.win_share === 'number') ? data.variant.win_share : (played > 0 ? (varPts / played * 100) : null);

    let completedCount = 0;

    const rowsHtml = baseModes.map((bm) => {
      const modeKey = bm.mode;
      const m = modeMap[modeKey] || {};
      const isCurrentActive = isSelfRunning && (activeModeClean === modeKey);
      const gamesVal = isCurrentActive ? played : (m.games != null ? m.games : (m.total_played != null ? m.total_played : 0));

      const isOmitted = (data.run_type === 'pool' && !data.self_play_matrix) || (matrixList.length > 0 && !modeMap[modeKey]);
      const isExplicitSkipped = (m.status === 'SKIPPED' || m.decision === 'SKIPPED');
      const isSkipped = isOmitted || isExplicitSkipped;

      let effectiveStatus = isRunning ? 'QUEUED' : 'QUEUED';
      if (isCurrentActive) {
        effectiveStatus = 'RUNNING';
      } else if (isSkipped) {
        effectiveStatus = 'QUEUED';
      } else if (gamesVal > 0) {
        const rawStat = (m.decision || m.status || '').toUpperCase();
        if (['KEEP', 'BETTER'].includes(rawStat)) {
          effectiveStatus = 'BETTER';
          completedCount++;
        } else if (['REVERT', 'FAILED', 'WORSE'].includes(rawStat)) {
          effectiveStatus = 'WORSE';
        } else {
          effectiveStatus = 'EQUAL';
          completedCount++;
        }
      } else if (isSelfRunning && (m.status === 'RUNNING' || m.status === 'running')) {
        effectiveStatus = 'RUNNING';
      }

      // Data presence rule: games == 0 is QUEUED, never render zero-filled defaults
      if (gamesVal === 0 && !isCurrentActive) {
        if (!isSkipped) {
          effectiveStatus = 'QUEUED';
        }
      }

      const statusClass = effectiveStatus.toLowerCase().replace(/ /g, '-');
      const statusBadge = `<span class="status-chip status-chip-${statusClass} matrix-col-status matrix-status-${statusClass}">${effectiveStatus}</span>`;

      const isCompleted = ['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED'].includes(effectiveStatus) && gamesVal > 0;
      const maxGames = isCompleted ? gamesVal : (m.max_games || bm.max_games || 200);
      const targetVal = bm.target_share;

      let scoreVal = '--';
      let scoreColor = 'var(--pipeline-text-main)';
      let deltaText = '--';
      let deltaColor = 'var(--pipeline-text-muted)';
      let zScoreText = '--';
      let zScoreColor = 'var(--pipeline-text-muted)';
      let etaText = '--';

      if (effectiveStatus === 'RUNNING') {
        scoreVal = (varWS != null) ? `${varWS.toFixed(1)}%` : '--';
        scoreColor = 'var(--pipeline-blue)';
        if (varWS != null) {
          const delta = varWS - targetVal;
          deltaText = `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`;
          deltaColor = delta > 0.5 ? 'var(--pipeline-green)' : (delta < -0.5 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        }
        // Compute live z-score
        if (played >= 5 && varWS != null) {
          const p = varWS / 100.0;
          const p0 = targetVal / 100.0;
          const se = Math.sqrt((p0 * (1.0 - p0)) / played);
          const z = se > 0 ? ((p - p0) / se) : 0.0;
          zScoreText = `${z >= 0 ? '+' : ''}${z.toFixed(2)}`;
          zScoreColor = z >= 1.645 ? 'var(--pipeline-green)' : (z <= -1.645 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        }
        const unitData = data.unit_tests_data || {};
        const unitSec = (unitData && unitData.duration_sec && unitData.duration_sec > 0) ? unitData.duration_sec : 0;
        const depth = data.depth_data || {};
        const depthSec = (depth && depth.duration_sec && depth.duration_sec > 0) ? depth.duration_sec : 0;
        const modeElapsed = (data.mode_elapsed_seconds != null && data.mode_elapsed_seconds > 0)
          ? data.mode_elapsed_seconds
          : (m.duration_sec != null && m.duration_sec > 0
            ? m.duration_sec
            : (data.matrix_elapsed_seconds != null ? data.matrix_elapsed_seconds : Math.max(0, (data.elapsed_seconds || 0) - unitSec - depthSec)));
        etaText = (modeElapsed != null && modeElapsed > 0) ? formatDuration(modeElapsed) : '0:00';
      } else if (gamesVal > 0 && ['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED'].includes(effectiveStatus)) {
        const ws = (m.win_share != null ? m.win_share : (m.var_win_share != null ? m.var_win_share : varWS));
        scoreVal = (ws != null) ? `${ws.toFixed(1)}%` : '--';
        if (effectiveStatus === 'BETTER' || effectiveStatus === 'PASSED') scoreColor = 'var(--pipeline-green)';
        else if (effectiveStatus === 'EQUAL' || effectiveStatus === 'PARITY') scoreColor = 'var(--pipeline-amber)';
        else scoreColor = 'var(--pipeline-red)';

        if (ws != null) {
          const delta = ws - targetVal;
          deltaText = `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`;
          deltaColor = delta > 0.5 ? 'var(--pipeline-green)' : (delta < -0.5 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        }

        if (m.z_score != null) {
          const zNum = Number(m.z_score);
          zScoreText = `${zNum >= 0 ? '+' : ''}${zNum.toFixed(2)}`;
          zScoreColor = zNum >= 1.645 ? 'var(--pipeline-green)' : (zNum <= -1.645 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        } else if (gamesVal >= 5 && ws != null) {
          const p = ws / 100.0;
          const p0 = targetVal / 100.0;
          const se = Math.sqrt((p0 * (1.0 - p0)) / gamesVal);
          const z = se > 0 ? ((p - p0) / se) : 0.0;
          zScoreText = `${z >= 0 ? '+' : ''}${z.toFixed(2)}`;
          zScoreColor = z >= 1.645 ? 'var(--pipeline-green)' : (z <= -1.645 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        }
        if (m.duration_sec != null && m.duration_sec > 0) {
          etaText = formatDuration(m.duration_sec);
        } else {
          etaText = '--';
        }
      } else if (effectiveStatus === 'SKIPPED') {
        scoreVal = (gamesVal > 0 && m.win_share != null) ? `${Number(m.win_share).toFixed(1)}%` : '--';
        scoreColor = 'var(--pipeline-text-muted)';
        if (gamesVal > 0 && m.win_share != null) {
          const delta = Number(m.win_share) - targetVal;
          deltaText = `${delta > 0 ? '+' : ''}${delta.toFixed(1)}%`;
          deltaColor = delta > 0.5 ? 'var(--pipeline-green)' : (delta < -0.5 ? 'var(--pipeline-red)' : 'var(--pipeline-amber)');
        } else {
          deltaText = '--';
          deltaColor = 'var(--pipeline-text-muted)';
        }
        zScoreText = '--';
        etaText = '--';
      } else {
        scoreVal = '--';
        scoreColor = 'var(--pipeline-text-muted)';
        deltaText = '--';
        deltaColor = 'var(--pipeline-text-muted)';
        zScoreText = '--';
        etaText = '--';
      }

      const rowClass = isCurrentActive ? 'active-running' : '';
      const gamesText = (effectiveStatus === 'SKIPPED' && gamesVal === 0) ? '--' : `${gamesVal} / ${maxGames}`;

      return `
        <tr class="${rowClass}">
          <td class="td-left" style="font-weight: 700; color: var(--pipeline-text-main);">
            <span>${bm.name}</span>
          </td>
          <td class="td-right" style="font-weight: 700;">${gamesText}</td>
          <td class="td-right" style="font-weight: 700; color: ${scoreColor}; font-size: 0.92rem;">${scoreVal}</td>
          <td class="td-right" style="font-weight: 700; color: ${deltaColor};">${deltaText}</td>
          <td class="td-right" style="font-weight: 600; color: ${zScoreColor};">${zScoreText}</td>
          <td class="td-right" style="color: var(--pipeline-text-muted); font-size: 0.82rem;" id="matrix-eta-${modeKey}">${etaText}</td>
          <td class="td-right">${statusBadge}</td>
        </tr>
      `;
    }).join('');

    let totalMatrixPlayed = 0;
    let totalMatrixMax = 0;
    let matrixModesCount = 0;
    let totalVarPoints = 0;
    let totalExpectedPoints = 0;
    let totalVariance = 0;

    baseModes.forEach((bm) => {
      const modeKey = bm.mode;
      const m = modeMap[modeKey] || {};
      const isOmitted = (data.run_type === 'pool' && !data.self_play_matrix) || (matrixList.length > 0 && !modeMap[modeKey]);
      const isSkipped = isOmitted || (m.status === 'SKIPPED' || m.decision === 'SKIPPED');
      if (!isSkipped) {
        matrixModesCount++;
        const isCurrentActive = isSelfRunning && (activeModeClean === modeKey);
        const gamesVal = isCurrentActive ? played : (m.games != null ? m.games : (m.total_played != null ? m.total_played : 0));
        const isCompleted = ['BETTER', 'EQUAL', 'WORSE', 'PASSED', 'PARITY', 'FAILED', 'KEEP', 'REVERT'].includes(m.status || m.decision) && gamesVal > 0;
        const maxGames = isCompleted ? gamesVal : (m.max_games || bm.max_games || 200);
        totalMatrixPlayed += gamesVal;
        totalMatrixMax += maxGames;

        if (gamesVal > 0) {
          const p0 = bm.target_share / 100.0;
          let modeVarPts = 0;
          if (isCurrentActive) {
            modeVarPts = varPts;
          } else if (m.points != null) {
            modeVarPts = m.points;
          } else if (m.var_points != null) {
            modeVarPts = m.var_points;
          } else {
            const ws = (m.win_share != null ? m.win_share : (m.var_win_share != null ? m.var_win_share : null));
            if (ws != null) {
              modeVarPts = (ws / 100.0) * gamesVal;
            }
          }
          totalVarPoints += modeVarPts;
          totalExpectedPoints += gamesVal * p0;
          totalVariance += gamesVal * p0 * (1.0 - p0);
        }
      }
    });
    if (matrixModesCount === 0) matrixModesCount = 4;
    setHTML(tableBody, rowsHtml);
  }

  function getPipelineDeltaColor(delta, threshold = 0.5) {
    if (delta == null || isNaN(delta)) return 'var(--pipeline-grey)';
    const d = Number(delta);
    if (d > threshold) return 'var(--pipeline-green)';
    if (d < -threshold) return 'var(--pipeline-red)';
    if (Math.abs(d) < 0.01) return 'var(--pipeline-grey)';
    return 'var(--pipeline-amber)';
  }

  function formatHistoryDate(raw) {
    if (!raw) return '--';
    try {
      let d;
      if (typeof raw === 'number') {
        d = new Date(raw * (raw < 1e11 ? 1000 : 1));
      } else {
        const s = String(raw).replace(' ', 'T');
        d = new Date(s);
      }
      if (isNaN(d.getTime())) return String(raw).slice(0, 12);
      const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const month = months[d.getMonth()];
      const day = d.getDate();
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      return `${month} ${day} ${hh}:${mm}`;
    } catch(e) {
      return String(raw).slice(0, 12);
    }
  }

  function formatHistoryDateParts(raw) {
    if (!raw || raw === '-') return { date: '-', time: '' };
    const s = String(raw).trim();
    const m = s.match(/^(\\d{4})-(\\d{1,2})-(\\d{1,2})[ T](\\d{1,2}:\\d{2})(?::\\d{2})?/);
    if (!m) return { date: s, time: '' };
    const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const monthIdx = parseInt(m[2], 10) - 1;
    const monthStr = (monthIdx >= 0 && monthIdx < 12) ? monthNames[monthIdx] : m[2];
    const day = parseInt(m[3], 10);
    const timePart = m[4];
    return { date: `${monthStr} ${day}`, time: timePart };
  }

  function formatHistoryDateStacked(raw) {
    if (!raw || raw === '-') return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>';
    const parts = formatHistoryDateParts(raw);
    if (!parts.time) return `<div class="hist-date-main">${escapeHtml(parts.date)}</div>`;
    return `<div class="hist-date-main">${escapeHtml(parts.date)}</div><div class="hist-date-time">${escapeHtml(parts.time)}</div>`;
  }

  function formatHistoryDepth(run) {
    const d = run.depth_results || run.depth_data;
    if (d && typeof d === 'object') {
      const ov = (d.overall && typeof d.overall === 'object') ? d.overall : d;
      const deltaD = ov.delta_depth != null ? ov.delta_depth : ov.total_delta_depth;
      const npsR = ov.nps_ratio;
      if (deltaD != null || npsR != null) {
        const dNum = Number(deltaD || 0.0);
        const sign = dNum >= 0 ? '+' : '';
        const dStr = `${sign}${dNum.toFixed(2)}d`;
        let col = 'var(--pipeline-text-main)';
        if (dNum > 0.05) col = 'var(--pipeline-green)';
        else if (dNum < -0.05) col = 'var(--pipeline-red)';
        const npsNum = Number(npsR != null ? npsR : 1.0);
        const npsStr = `${npsNum.toFixed(2)}x NPS`;
        return `<div class="hist-depth-delta" style="color: ${col}; font-weight: 600;">${dStr}</div><div class="hist-depth-nps">${npsStr}</div>`;
      }
    }
    return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>';
  }

  function getRecordActiveSources(run) {
    const sources = [];
    if (recordHasMatrixGames(run)) sources.push('SELF');
    if (recordHasPoolGames(run)) sources.push('POOL');
    return sources;
  }

  function formatHistorySrcCell(run) {
    const sources = getRecordActiveSources(run);
    if (!sources.length) return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>';
    return sources.map(s => `<div class="hist-src-line">${s}</div>`).join('');
  }

  function formatModeStatLine(share, delta) {
    let dSpan = '';
    if (delta != null && !isNaN(delta)) {
      const sign = delta >= 0 ? '+' : '';
      let dCol = 'var(--pipeline-text-muted)';
      if (delta > 0.5) dCol = 'var(--pipeline-green)';
      else if (delta < -0.5) dCol = 'var(--pipeline-red)';
      dSpan = ` <span style="color: ${dCol};">(${sign}${delta.toFixed(1)}%)</span>`;
    }
    return `<div class="hist-mode-line">${share.toFixed(1)}%${dSpan}</div>`;
  }

  function getHistoryMatrixMode(run, modeKey) {
    if (Array.isArray(run.matrix_results)) {
      const mItem = run.matrix_results.find(it => it && it.mode === modeKey);
      if (mItem) {
        const games = mItem.total_played != null ? mItem.total_played : mItem.games;
        if (games != null && games > 0) {
          const ws = mItem.var_win_share != null ? mItem.var_win_share : mItem.win_share;
          if (ws != null) {
            const tgt = Number(mItem.target_share || (modeKey === 'duel' ? 50.0 : 25.0));
            return { share: Number(ws), delta: Number(ws) - tgt };
          }
        }
      }
    }
    if (run.game_mode === modeKey && (run.total_games || 0) > 0 && run.variant) {
      const ws = run.variant.win_share != null ? run.variant.win_share : run.variant.rate;
      if (ws != null) {
        const tgt = modeKey === 'duel' ? 50.0 : 25.0;
        return { share: Number(ws), delta: Number(ws) - tgt };
      }
    }
    return null;
  }

  function getHistoryPoolMode(run, modeKey) {
    const pData = run.pool_data || run.pool_results || run.pool_matrix;
    if (pData) {
      let pItem = null;
      if (Array.isArray(pData)) {
        pItem = pData.find(it => it && (it.mode === modeKey || it.name === modeKey));
      } else if (typeof pData === 'object') {
        if (pData.mode_stats && typeof pData.mode_stats === 'object' && pData.mode_stats[modeKey]) {
          pItem = pData.mode_stats[modeKey];
        } else if (Array.isArray(pData.modes)) {
          pItem = pData.modes.find(it => it && (it.mode === modeKey || it.name === modeKey));
        } else if (pData[modeKey] && typeof pData[modeKey] === 'object') {
          pItem = pData[modeKey];
        }
      }
      if (pItem) {
        let games = pItem.games != null ? pItem.games : (pItem.cand_games != null ? pItem.cand_games : pItem.total_games);
        let candWs = pItem.cand_win_share != null ? pItem.cand_win_share : (pItem.cand_share != null ? pItem.cand_share : pItem.var_win_share);
        if (games == null && candWs != null) games = 1;
        if (games != null && games > 0 && candWs != null) {
          const baseWs = pItem.base_win_share != null ? pItem.base_win_share : pItem.base_share;
          let delta = pItem.delta;
          if (delta == null && baseWs != null) {
            delta = Number(candWs) - Number(baseWs);
          }
          return { share: Number(candWs), delta: delta != null ? Number(delta) : 0.0 };
        }
      }
    }
    return null;
  }

  function getHistoryModeCell(run, modeKey) {
    const sources = getRecordActiveSources(run);
    const lines = [];
    sources.forEach(s => {
      if (s === 'SELF') {
        const st = getHistoryMatrixMode(run, modeKey);
        if (st) lines.push(formatModeStatLine(st.share, st.delta));
      } else if (s === 'POOL') {
        const st = getHistoryPoolMode(run, modeKey);
        if (st) lines.push(formatModeStatLine(st.share, st.delta));
      }
    });
    if (!lines.length) return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>';
    return lines.join('');
  }

  function recordHasMatrixGames(run) {
    if (Array.isArray(run.matrix_results) && run.matrix_results.length > 0) {
      for (const it of run.matrix_results) {
        if (!it) continue;
        const g = it.total_played != null ? it.total_played : it.games;
        if (g != null && g > 0) return true;
      }
    }
    if ((run.total_games || 0) > 0 && run.variant) return true;
    return false;
  }

  function recordHasPoolGames(run) {
    const pd = run.pool_data || run.pool_results || run.pool_matrix;
    if (!pd) return false;
    if (Array.isArray(pd)) {
      for (const it of pd) {
        if (!it) continue;
        const g = it.games != null ? it.games : (it.cand_games != null ? it.cand_games : it.total_games);
        if (g != null && g > 0) return true;
        if (g == null && (it.cand_win_share != null || it.cand_share != null)) return true;
      }
    } else if (typeof pd === 'object') {
      if ((pd.total_games || 0) > 0) return true;
      const modes = Array.isArray(pd.modes) ? pd.modes : (Array.isArray(pd.mode_results) ? pd.mode_results : null);
      if (modes) {
        for (const it of modes) {
          if (!it) continue;
          const g = it.games != null ? it.games : it.cand_games;
          if (g != null && g > 0) return true;
          if (g == null && (it.cand_win_share != null || it.cand_share != null)) return true;
        }
      }
      if (pd.mode_stats && typeof pd.mode_stats === 'object') {
        for (const k in pd.mode_stats) {
          const s = pd.mode_stats[k];
          if (s && s.games > 0) return true;
        }
      }
    }
    return false;
  }

  function deriveHistoryVerdict(run) {
    const vRaw = String(run.verdict || '').toUpperCase().trim();
    const outcomeRaw = String(run.outcome || '').toUpperCase().trim();
    const rec = String(run.recommendation || run.stop_type || '').toUpperCase().trim();

    let text = 'REVERTED';
    if (vRaw === 'KEPT' || vRaw === 'KEEP' || (!vRaw && rec === 'KEEP')) {
      text = 'KEPT';
    } else {
      text = 'REVERTED';
    }

    let chipCls = 'worse';
    if (outcomeRaw === 'BETTER' || outcomeRaw === 'KEEP' || (!outcomeRaw && rec === 'KEEP')) {
      chipCls = 'better';
    } else if (outcomeRaw === 'EQUAL' || outcomeRaw === 'PARITY' || (!outcomeRaw && rec === 'PARITY')) {
      chipCls = 'equal';
    } else {
      chipCls = 'worse';
    }

    return { text, chipCls };
  }

  function formatHistoryTests(run) {
    const rec = String(run.recommendation || '').toUpperCase();
    const uData = run.unit_tests || run.unit_tests_data;
    if (uData && typeof uData === 'object') {
      if (typeof uData.passed === 'number' && typeof uData.total === 'number') {
        const isPass = (uData.passed === uData.total && String(uData.status || '').toUpperCase() !== 'FAILED');
        const col = isPass ? 'var(--pipeline-green)' : 'var(--pipeline-red)';
        return `<span style="color: ${col}; font-weight: 600;">${uData.passed}/${uData.total}</span>`;
      }
      const st = String(uData.status || '').toUpperCase();
      if (st === 'PASSED') return '<span style="color: var(--pipeline-green); font-weight: 600;">PASS</span>';
      if (st === 'FAILED') return '<span style="color: var(--pipeline-red); font-weight: 600;">FAIL</span>';
    }
    if (rec === 'TEST_FAILURE') return '<span style="color: var(--pipeline-red); font-weight: 600;">FAIL</span>';
    return '<span style="color: var(--pipeline-text-muted);">&mdash;</span>';
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
      setText(historyCount, '0 Runs');
      setHTML(historyBody, '<tr class="empty-row"><td colspan="10" class="empty-cell" style="text-align: center; color: var(--pipeline-text-muted); padding: 3rem;">No runs yet</td></tr>');
      return;
    }

    const allHistory = [...historyList].reverse();
    const countText = `${historyList.length} Run${historyList.length === 1 ? '' : 's'}`;
    setText(historyCount, countText);

    const rowsHtml = allHistory.map((run, idx) => {
      const isEven = (idx % 2 === 0);
      const entryClass = isEven ? 'entry-even' : 'entry-odd';
      const rawDesc = run.experiment_desc || run.description || 'Unspecified Experiment';
      const expDesc = escapeHtml(rawDesc);
      const dateHtml = formatHistoryDateStacked(run.timestamp);
      const testsHtml = formatHistoryTests(run);
      const depthHtml = formatHistoryDepth(run);
      const srcHtml = formatHistorySrcCell(run);
      const constHtml = getHistoryModeCell(run, 'constrictor');
      const royaleHtml = getHistoryModeCell(run, 'royale');
      const duelHtml = getHistoryModeCell(run, 'duel');
      const standardHtml = getHistoryModeCell(run, 'standard');
      const verdict = deriveHistoryVerdict(run);
      const verdictBadge = `<span class="status-chip status-chip-${verdict.chipCls}">${verdict.text}</span>`;

      return `
        <tr class="history-row ${entryClass}" data-entry="${idx}">
          <td class="td-left">${dateHtml}</td>
          <td class="td-left cell-experiment"><span class="hist-exp-text" title="${expDesc}">${expDesc}</span></td>
          <td class="td-center">${testsHtml}</td>
          <td class="td-center">${depthHtml}</td>
          <td class="td-center">${srcHtml}</td>
          <td class="td-right">${constHtml}</td>
          <td class="td-right">${royaleHtml}</td>
          <td class="td-right">${duelHtml}</td>
          <td class="td-right">${standardHtml}</td>
          <td class="td-right">${verdictBadge}</td>
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
      const uiState = deriveUiState(data);
      const isRunning = (uiState === 'RUNNING');
      const isEmpty = (uiState === 'EMPTY');
      const isComplete = (uiState === 'COMPLETE');
      const isAborted = (uiState === 'ABORTED');

      if (lastSeenRunType !== null && (lastSeenRunType !== runType || (data.experiment_desc && lastSeenDesc !== null && data.experiment_desc !== lastSeenDesc))) {
        userSelectedStage = false;
      }
      let currentHash = '';
      try {
        const urlParams = new URLSearchParams(window.location.search);
        currentHash = urlParams.get('stage') || (window.location.hash || '').replace('#', '');
      } catch (err) {}
      if (['unit', 'depth', 'self', 'pool'].includes(currentHash)) {
        selectStage(currentHash);
      }
      lastSeenRunType = runType;
      lastSeenDesc = data.experiment_desc || '';

      // Header Status & Target Chips
      const statusBadge = document.getElementById('statusBadge');
      const statusText = document.getElementById('statusText');
      const headerNoData = document.getElementById('headerNoData');
      const abortedBanner = document.getElementById('abortedBanner');
      const targetBadge = document.getElementById('targetBadge');
      const targetText = document.getElementById('targetText');

      if (isEmpty) {
        if (statusBadge) statusBadge.style.display = 'none';
        if (targetBadge) targetBadge.style.display = 'none';
        if (headerNoData) headerNoData.style.display = 'inline-flex';
        if (abortedBanner) abortedBanner.style.display = 'none';
      } else {
        if (headerNoData) headerNoData.style.display = 'none';

        // 1. Target Chip
        if (targetBadge && targetText) {
          const rawTarget = data.target;
          let normTarget = (rawTarget != null) ? String(rawTarget).trim().toUpperCase() : '';
          if (normTarget.includes('/') && normTarget.includes('PARITY')) normTarget = 'PARITY';

          let targetDisplay = '🎯 NOT SET';
          if (normTarget && normTarget !== 'NONE' && normTarget !== 'NULL') {
            targetDisplay = `🎯 ${normTarget}`;
          }
          targetBadge.className = 'badge badge-target';
          targetBadge.style.display = 'inline-flex';
          setText(targetText, targetDisplay);
        }

        // 2. Status Chip
        if (statusBadge && statusText) {
          const rawTarget = data.target;
          let normTarget = (rawTarget != null) ? String(rawTarget).trim().toUpperCase() : '';
          if (normTarget.includes('/') && normTarget.includes('PARITY')) normTarget = 'PARITY';

          const nowSec = Date.now() / 1000;
          const lastGameTime = data.last_game_time ? Number(data.last_game_time) : 0;
          const lastGameAge = lastGameTime > 0 ? Math.max(0, nowSec - lastGameTime) : 0;
          const isUnder120 = (lastGameTime <= 0 || lastGameAge < 120);

          const stopType = (data.stop_type || '').toUpperCase().trim();
          const statusVal = (data.status || '').toUpperCase().trim();
          const rec = (data.recommendation || '').toUpperCase().trim();

          let chipText = 'IDLE';
          let chipClass = 'idle';

          // (a) runner_alive true and last_game_time under 120 seconds old -> RUNNING (blue)
          if (data.runner_alive === true && isUnder120) {
            chipText = 'RUNNING';
            chipClass = 'running';
          }
          // (b) stop_type DISCONNECTED or FAILED, or status TEST_FAILURE, or rec WORSE/REVERT -> WORSE (red)
          else if (['DISCONNECTED', 'FAILED'].includes(stopType) || statusVal === 'TEST_FAILURE' || ['REVERT', 'REJECT', 'WORSE'].includes(rec)) {
            chipText = 'WORSE';
            chipClass = 'worse';
          }
          // (c) a recommendation exists -> BETTER, EQUAL, WORSE
          else if (rec) {
            if (['KEEP', 'BETTER'].includes(rec)) {
              chipText = 'BETTER';
              chipClass = 'better';
            } else if (['PARITY', 'EQUAL'].includes(rec)) {
              chipText = 'EQUAL';
              chipClass = 'equal';
            } else {
              chipText = 'WORSE';
              chipClass = 'worse';
            }
          } else if (['PARITY', 'MATRIX_COMPLETE'].includes(stopType)) {
            chipText = 'EQUAL';
            chipClass = 'equal';
          } else {
            chipText = 'WORSE';
            chipClass = 'worse';
          }

          statusBadge.style.display = 'inline-flex';
          statusBadge.className = `badge badge-status-${chipClass}`;
          setText(statusText, chipText);
        }

        if (isAborted && abortedBanner) {
          abortedBanner.style.display = 'block';
          setText('abortedBannerReason', data.stop_reason || 'Simulation runner terminated unexpectedly.');
          setText('abortedBannerDetail', computeCompletenessLine(data));
        } else if (abortedBanner) {
          abortedBanner.style.display = 'none';
        }
      }

      const subtext = document.getElementById('headerSubtext');
      if (subtext) {
        if (isEmpty) {
          const emptyText = 'No data.';
          if (subtext.innerText !== emptyText) subtext.innerText = emptyText;
          if (subtext.title !== emptyText) subtext.title = emptyText;
        } else {
          const expTitle = data.experiment_desc || 'Unspecified Experiment';
          const newSub = `Testing: ${expTitle}`;
          if (subtext.innerText !== newSub) subtext.innerText = newSub;
          if (subtext.title !== newSub) subtext.title = newSub;
        }
      }

      const thCount = (!isEmpty && data.threads != null && data.threads > 0) ? data.threads : (isRunning ? 0 : '--');
      setText('threadsCount', thCount);

      // Sync and tick unified timers
      TimerStore.sync(data);

      const modeElapsed = TimerStore.staticDurations.modeSec || 0;

      // Header Current Mode Live Rate Chip (Requirement 4)
      let currentModeLabel = 'Mode';
      if (runType === 'depth') {
        currentModeLabel = 'Depth';
      } else if (data.game_mode) {
        const rawM = String(data.game_mode).toLowerCase();
        if (rawM === 'duel') currentModeLabel = 'Duel';
        else if (rawM === 'standard') currentModeLabel = 'Standard';
        else if (rawM === 'constrictor') currentModeLabel = 'Constrictor';
        else if (rawM === 'royale') currentModeLabel = 'Royale';
        else currentModeLabel = rawM.charAt(0).toUpperCase() + rawM.slice(1);
      }

      const played = data.total_played || 0;
      const pace = (isRunning && modeElapsed > 0) ? (played / modeElapsed) : 0;
      const rateGpm = pace * 60;
      const rateUnit = (runType === 'depth') ? 'pos/min' : 'games/min';
      const rateStr = isRunning ? `${rateGpm.toFixed(1)} ${rateUnit}` : (isEmpty ? '--' : `0.0 ${rateUnit}`);
      setText('headerStatRate', rateStr);

      // Shared metrics calculation
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


      updateHistoryTable(data);

      // Explicitly trigger render on synchronized data
      TimerStore.renderAllTimers(data, TimerStore.getSnapshot());

    } catch (err) {
      if (err && (err.name === 'AbortError' || err.message === 'The user aborted a request.')) {
        return;
      }
      console.error('Dashboard update error:', err);
      if (lastSeenDesc && lastSeenDesc.trim() !== '') {
        const statusBadge = document.getElementById('statusBadge');
        const statusText = document.getElementById('statusText');
        if (statusBadge && statusText) {
          statusBadge.style.display = 'inline-flex';
          statusBadge.className = 'badge badge-status-worse';
          setText(statusText, 'WORSE');
        }
      }
    } finally {
      isUpdating = false;
    }
  }

  ['unit', 'depth', 'self', 'pool'].forEach(id => {
    const el = document.getElementById(`railSegment-${id}`);
    if (el) el.addEventListener('click', () => selectStage(id));
  });

  let centralTickTimerId = null;
  let dashboardPollTimerId = null;

  function startUnifiedTimers() {
    stopUnifiedTimers();
    // Centralized tick driver at 100ms: evaluates and re-renders all timers in unison
    centralTickTimerId = setInterval(() => {
      TimerStore.tick();
    }, 100);
    // Centralized poll interval at 500ms
    dashboardPollTimerId = setInterval(updateDashboard, 500);
  }

  function stopUnifiedTimers() {
    if (centralTickTimerId) {
      clearInterval(centralTickTimerId);
      centralTickTimerId = null;
    }
    if (dashboardPollTimerId) {
      clearInterval(dashboardPollTimerId);
      dashboardPollTimerId = null;
    }
  }

  window.addEventListener('beforeunload', stopUnifiedTimers);
  window.addEventListener('unload', stopUnifiedTimers);

  const initialHash = (window.location.hash || '').replace('#', '');
  if (['unit', 'depth', 'self', 'pool'].includes(initialHash)) {
    selectStage(initialHash);
  }

  updateDashboard();
  startUnifiedTimers();
</script>
</body>
</html>
"""

def py_format_seconds(sec: float) -> str:
    return format_duration(sec, default="0:00")


def render_html_with_state(template_html: str, data: Optional[Dict[str, Any]] = None, requested_stage: Optional[str] = None) -> str:
    if data is None:
        data = {}

    status = data.get("status", "IDLE")
    ui_state = derive_ui_state(data)
    is_running = (ui_state == "RUNNING")
    is_empty = (ui_state == "EMPTY")
    is_complete = (ui_state == "COMPLETE")
    is_aborted = (ui_state == "ABORTED")
    interrupted_stage = determine_interrupted_stage(data) if is_aborted else None
    elapsed_sec = float(data.get("elapsed_seconds") or 0.0)
    mode_elapsed = float(data.get("mode_elapsed_seconds") or elapsed_sec)
    eta_sec = data.get("eta_seconds")
    played = int(data.get("total_played") or 0)
    game_mode = str(data.get("game_mode") or "standard").lower()
    run_type = str(data.get("run_type") or "self")
    exp_desc = data.get("experiment_desc") or "Unspecified Experiment"
    threads_val = data.get("threads")
    threads_display = "--" if is_empty else (str(threads_val) if (threads_val is not None and threads_val > 0) else ("0" if is_running else "--"))

    # Header elapsed clock & LEFT ETA (Requirement 1)
    elapsed_str = py_format_seconds(elapsed_sec)
    elapsed_clock_display = "--" if is_empty else (format_duration(elapsed_sec) if (is_running or elapsed_sec > 0) else "0:00")
    left_display = "--" if is_empty else (format_left_duration(float(eta_sec)) if (is_running and eta_sec and float(eta_sec) > 0) else "--")

    # Header live rate in games per minute (Requirement 1 unit rule)
    rate_unit = "pos/min" if run_type == "depth" else "games/min"
    rate_gpm = calculate_rate_gpm(played, mode_elapsed) if is_running else 0.0
    rate_display = "--" if is_empty else format_rate_gpm(rate_gpm, rate_unit)

    # Calculate suite progress across entire run
    total_completed_run, total_planned_run, games_progress_pct = calculate_suite_progress(
        run_type=run_type,
        matrix_progress=data.get("matrix_progress"),
        self_play_matrix=data.get("self_play_matrix"),
        depth_data=data.get("depth_data"),
        current_mode=game_mode,
        current_played=played,
        target_games=data.get("target_games") or data.get("max_games") or 200,
        is_running=is_running,
    )

    is_finished = (status in ("FINISHED", "TEST_FAILURE") or data.get("stop_type") in ("MATRIX_COMPLETE", "PARITY", "KEEP") or is_complete)
    if is_finished:
        run_progress_pct = 100.0
    elif is_running and eta_sec is not None and float(eta_sec) > 0 and elapsed_sec > 0:
        run_progress_pct = calculate_time_progress_percent(elapsed_sec, float(eta_sec), is_finished=False)
    else:
        run_progress_pct = games_progress_pct

    # Pre-render Unit Tests detail
    unit_data = data.get("unit_tests_data") or {}
    depth_data = data.get("depth_data") or {}
    unit_status = str(unit_data.get("status") or ("RUNNING" if is_running else ("FAILED" if (is_aborted and interrupted_stage == "unit") else ("PASS" if is_complete else "PENDING")))).upper()
    unit_dur = unit_data.get("duration_sec")
    unit_dur_str = format_duration(float(unit_dur), allow_decimal=True) if unit_dur else "--"
    tests_list = unit_data.get("tests") or []
    unit_total = len(tests_list) if tests_list else unit_data.get("total", 0)
    unit_passed = sum(1 for t in tests_list if str(t.get("status", "")).upper() == "PASSED") if tests_list else unit_data.get("passed", unit_total)
    unit_tests_html = render_unit_tests_html(tests_list or [])

    # Header Status Chip
    status_chip_text, status_chip_class, status_is_visible = derive_status_pill(data)

    # Pre-render Self-Play Matrix rows & overall progress
    base_modes = [
        {"mode": "constrictor", "name": "Constrictor 4P", "target_share": 25.0, "max_games": 200, "num_snakes": 4},
        {"mode": "royale", "name": "Royale 4P", "target_share": 25.0, "max_games": 200, "num_snakes": 4},
        {"mode": "duel", "name": "Duel 1v1", "target_share": 50.0, "max_games": 200, "num_snakes": 2},
        {"mode": "standard", "name": "Standard 4P", "target_share": 25.0, "max_games": 200, "num_snakes": 4},
    ]
    matrix_list = data.get("matrix_progress") or []
    has_matrix = len(matrix_list) > 0
    mode_map = {}
    for m in matrix_list:
        if m.get("mode"):
            mode_map[m["mode"].lower()] = m

    total_matrix_played = 0
    total_matrix_max = 0
    matrix_modes_count = 0
    completed_modes = 0
    total_var_pts = 0.0
    total_exp_pts = 0.0
    total_matrix_var = 0.0
    rows = []

    for bm in base_modes:
        mkey = bm["mode"]
        m = mode_map.get(mkey, {})
        is_curr = (is_running and game_mode == mkey and run_type == "self")
        is_omitted = has_matrix and (mkey not in mode_map)
        is_skipped = is_omitted or (m.get("status") == "SKIPPED") or (m.get("decision") == "SKIPPED")

        m_status = str(m.get("status") or m.get("decision") or "PENDING").upper()
        if is_curr:
            eff_status = "RUNNING"
        elif is_skipped:
            eff_status = "QUEUED"
        elif m_status in ("KEEP", "BETTER", "PASSED"):
            eff_status = "BETTER"
            completed_modes += 1
        elif m_status in ("REVERT", "FAILED", "WORSE"):
            eff_status = "WORSE"
        elif m_status in ("PARITY", "EQUAL"):
            eff_status = "EQUAL"
            completed_modes += 1
        else:
            eff_status = "QUEUED"

        g_val = played if is_curr else (m.get("games") if m.get("games") is not None else (m.get("total_played") or 0))
        # Data presence rule: games == 0 is QUEUED, never render zero-filled defaults
        if g_val == 0 and not is_curr and not is_skipped:
            eff_status = "QUEUED"
        is_completed = (eff_status in ("BETTER", "EQUAL", "WORSE", "PASSED", "PARITY", "FAILED") and g_val > 0)
        max_g = g_val if is_completed else (m.get("max_games") or bm["max_games"])
        target_val = bm["target_share"]
        var_data = data.get("variant") or {}
        var_ws = var_data.get("win_share") if is_curr else m.get("win_share")

        if not is_skipped:
            matrix_modes_count += 1
            total_matrix_played += g_val
            total_matrix_max += max_g
            if g_val > 0:
                p0 = target_val / 100.0
                if is_curr:
                    var_w = float(var_data.get("wins", 0.0) or 0.0)
                    draws_data = data.get("draws") or {}
                    draws_cnt = float(draws_data.get("count", 0.0) or 0.0)
                    draw_share = 0.25 if (bm.get("num_snakes") == 4) else 0.5
                    m_pts = float(var_data.get("points", var_w + draw_share * draws_cnt) or 0.0)
                elif m.get("points") is not None:
                    m_pts = float(m["points"])
                elif m.get("var_points") is not None:
                    m_pts = float(m["var_points"])
                else:
                    m_pts = (float(var_ws) / 100.0) * g_val if var_ws is not None else 0.0
                total_var_pts += m_pts
                total_exp_pts += g_val * p0
                total_matrix_var += g_val * p0 * (1.0 - p0)

        pct_done = min(100.0, (g_val / max_g * 100.0)) if max_g > 0 else 0.0

        if eff_status == "RUNNING":
            score_val = f"{float(var_ws):.1f}%" if var_ws is not None else "--"
            score_color = "var(--pipeline-blue)"
            if played >= 5 and var_ws is not None:
                p = float(var_ws) / 100.0
                p0 = target_val / 100.0
                se = math.sqrt((p0 * (1.0 - p0)) / played)
                z = (p - p0) / se if se > 0 else 0.0
                z_score_text = f"{z:+.2f}"
            else:
                z_score_text = "--"
            mode_elapsed = data.get("mode_elapsed_seconds")
            if not mode_elapsed:
                unit_sec = float((unit_data or {}).get("duration_sec", 0.0) or 0.0)
                depth_sec = float((depth_data or {}).get("duration_sec", 0.0) or 0.0)
                mode_elapsed = max(0.0, float(data.get("elapsed_seconds", 0.0) or 0.0) - unit_sec - depth_sec)
            eta_text = format_duration(float(mode_elapsed)) if mode_elapsed > 0 else "0:00"
        elif g_val > 0 and eff_status in ("BETTER", "EQUAL", "WORSE", "PASSED", "PARITY", "FAILED"):
            score_val = f"{float(var_ws):.1f}%" if var_ws is not None else "--"
            score_color = "var(--pipeline-green)" if eff_status in ("BETTER", "PASSED") else ("var(--pipeline-amber)" if eff_status in ("EQUAL", "PARITY") else "var(--pipeline-red)")
            z_score = m.get("z_score")
            if z_score is not None:
                z_score_text = f"{float(z_score):+.2f}"
            elif g_val >= 5 and var_ws is not None:
                p = float(var_ws) / 100.0
                p0 = target_val / 100.0
                se = math.sqrt((p0 * (1.0 - p0)) / g_val)
                z = (p - p0) / se if se > 0 else 0.0
                z_score_text = f"{z:+.2f}"
            else:
                z_score_text = "--"
            dur = m.get("duration_sec")
            eta_text = format_duration(float(dur)) if (dur and float(dur) > 0) else "--"
        elif eff_status == "SKIPPED":
            score_val = f"{float(var_ws):.1f}%" if (var_ws is not None and g_val > 0) else "--"
            score_color = "var(--pipeline-text-muted)"
            z_score_text = "--"
            eta_text = "--"
        else:
            score_val = "--"
            score_color = "var(--pipeline-text-main)"
            z_score_text = "--"
            eta_text = "--"

        status_badge = f'<span class="status-chip status-chip-{eff_status.lower()} matrix-col-status matrix-status-{eff_status.lower()}">{eff_status}</span>'
        row_class = "active-running" if is_curr else ""
        z_color = "var(--pipeline-green)" if z_score_text != "--" and float(z_score_text) >= 1.645 else ("var(--pipeline-red)" if z_score_text != "--" and float(z_score_text) <= -1.645 else ("var(--pipeline-amber)" if z_score_text != "--" else "var(--pipeline-text-muted)"))

        if eff_status != "SKIPPED" and var_ws is not None and g_val > 0:
            d_val = float(var_ws) - target_val
            d_sign = "+" if d_val > 0 else ""
            delta_text = f"{d_sign}{d_val:.1f}%"
            delta_col = "var(--pipeline-green)" if d_val > 0.5 else ("var(--pipeline-red)" if d_val < -0.5 else "var(--pipeline-amber)")
        else:
            delta_text = "--"
            delta_col = "var(--pipeline-text-muted)"

        games_display = "--" if (eff_status == "SKIPPED" and g_val == 0) else f"{g_val} / {max_g}"

        rows.append(f'''
        <tr class="{row_class}">
          <td class="td-left" style="font-weight: 700; color: var(--pipeline-text-main);">
            <span>{bm["name"]}</span>
          </td>
          <td class="td-right" style="font-weight: 700;">{games_display}</td>
          <td class="td-right" style="font-weight: 700; color: {score_color}; font-size: 0.92rem;">{score_val}</td>
          <td class="td-right" style="font-weight: 700; color: {delta_col};">{delta_text}</td>
          <td class="td-right" style="font-weight: 600; color: {z_color};">{z_score_text}</td>
          <td class="td-right" style="color: var(--pipeline-text-muted); font-size: 0.82rem;" id="matrix-eta-{mkey}">{eta_text}</td>
          <td class="td-right">{status_badge}</td>
        </tr>''')

    matrix_rows_html = "\n".join(rows)
    matrix_overall_pct = (total_matrix_played / total_matrix_max * 100.0) if total_matrix_max > 0 else 0.0

    if matrix_modes_count == 0:
        matrix_modes_count = 4

    if total_matrix_played > 0:
        matrix_net_delta = ((total_var_pts - total_exp_pts) / total_matrix_played) * 100.0
        m_delta_str = f"{'+' if matrix_net_delta > 0 else ''}{matrix_net_delta:.1f}%"
        m_delta_col = "var(--pipeline-green)" if matrix_net_delta > 0.5 else ("var(--pipeline-red)" if matrix_net_delta < -0.5 else "var(--pipeline-amber)")
    else:
        m_delta_str = "--"
        m_delta_col = "var(--pipeline-text-muted)"

    if total_matrix_played >= 5 and total_matrix_var > 0:
        m_se = math.sqrt(total_matrix_var)
        m_z = (total_var_pts - total_exp_pts) / m_se
        m_z_str = f"{m_z:+.2f}"
        m_z_col = "var(--pipeline-green)" if m_z >= 1.645 else ("var(--pipeline-red)" if m_z <= -1.645 else "var(--pipeline-amber)")
    else:
        m_z_str = "--"
        m_z_col = "var(--pipeline-text-muted)"

    # Pre-render Zoo Pool Gate table
    pool_data = data.get("pool_data") or {}
    raw_pool_modes = pool_data.get("mode_results") or pool_data.get("modes") or pool_data.get("mode_stats") or {}
    pool_modes = {}
    if isinstance(raw_pool_modes, list):
        for item in raw_pool_modes:
            if isinstance(item, dict) and item.get("mode"):
                pool_modes[item["mode"].lower()] = item
    elif isinstance(raw_pool_modes, dict):
        pool_modes = {k.lower(): v for k, v in raw_pool_modes.items()}

    pool_opponents = pool_data.get("opponents") or []
    is_pool_running = (status == "RUNNING" and run_type == "pool" and data.get("runner_alive", True) is not False)
    pool_overall_z = pool_data.get("z_score", 0.0)

    pool_rows = []
    completed_pool_count = 0
    total_pool_played = 0
    total_pool_max = 0
    pool_modes_count = 0
    for bm in base_modes:
        mkey = bm["mode"]
        pm = pool_modes.get(mkey, {})
        mm = mode_map.get(mkey, {})
        is_curr_pool = (is_pool_running and game_mode == mkey)

        is_omitted = has_matrix and (mkey not in mode_map) and (mkey not in pool_modes)
        is_skipped = is_omitted or (pm.get("status") == "SKIPPED") or (mm.get("status") == "SKIPPED") or (pm.get("decision") == "SKIPPED") or (mm.get("decision") == "SKIPPED")

        cand_s = None if is_omitted else (pm.get("cand_share") if pm.get("cand_share") is not None else (pool_data.get("cand_share") if is_curr_pool else None))
        base_s = None if is_omitted else (pm.get("base_share") if pm.get("base_share") is not None else (pool_data.get("base_share") if is_curr_pool else None))
        p_delta = None if is_omitted else (pm.get("delta") if pm.get("delta") is not None else (((float(cand_s) - float(base_s))) if (cand_s is not None and base_s is not None) else None))
        pz = None if is_omitted else (pm.get("z_score") if pm.get("z_score") is not None else (pool_overall_z if is_curr_pool else None))

        p_max = pm.get("max_games") or pool_data.get("max_games") or (40 if run_type == "pool" else 40)
        cand_g = pm.get("cand_games") if pm.get("cand_games") is not None else (played if is_curr_pool else (pm.get("games") or 0))
        base_g = pm.get("base_games") if pm.get("base_games") is not None else (played if is_curr_pool else (pm.get("games") or 0))
        active_g = max(cand_g, base_g)

        p_dec = pm.get("decision")
        if is_curr_pool:
            p_status_class = "running"
            p_status_text = "RUNNING"
        elif is_skipped:
            p_status_class = "skipped"
            p_status_text = "SKIPPED"
        elif active_g == 0:
            p_status_class = "queued"
            p_status_text = "QUEUED"
        elif p_dec:
            p_status_text = p_dec
            p_status_class = p_dec.lower()
            if p_status_text == "PASSED":
                completed_pool_count += 1
        elif cand_s is not None:
            p_status_text = "PASSED" if (p_delta or 0) >= 0 else "FAILED"
            p_status_class = p_status_text.lower()
            if p_status_text == "PASSED":
                completed_pool_count += 1
        else:
            p_status_class = "queued"
            p_status_text = "QUEUED"

        is_completed_pool = (p_status_text in ("BETTER", "EQUAL", "WORSE", "PASSED", "PARITY", "FAILED", "KEEP", "REVERT") and active_g > 0)
        eff_p_max = active_g if is_completed_pool else p_max
        if not is_skipped:
            pool_modes_count += 1
            total_pool_played += active_g
            total_pool_max += eff_p_max
        p_games_text = f"{active_g} / {eff_p_max}" if (active_g > 0) else ("--" if is_skipped else f"0 / {p_max}")
        p_pct_done = min(100.0, (active_g / p_max * 100.0)) if p_max > 0 else 0.0

        if active_g > 0 and cand_s is not None and base_s is not None:
            ws_html = f'<span style="font-weight: 700; color: var(--pipeline-green);">Var: {float(cand_s):.1f}%</span> <span style="color: var(--pipeline-text-muted);">·</span> <span style="font-weight: 700; color: var(--pipeline-blue);">Base: {float(base_s):.1f}%</span>'
        else:
            ws_html = '<span style="color: var(--pipeline-text-muted);">--</span>'

        if active_g > 0 and p_delta is not None:
            pd_val = float(p_delta)
            pd_sign = "+" if pd_val > 0 else ""
            pd_col = "var(--pipeline-green)" if pd_val > 0.5 else ("var(--pipeline-red)" if pd_val < -0.5 else "var(--pipeline-amber)")
            p_delta_text = f"{pd_sign}{pd_val:.1f}%"
        else:
            p_delta_text = "--"
            pd_col = "var(--pipeline-text-muted)"

        if active_g >= 5 and pz is not None:
            pz_num = float(pz)
            pz_text = f"{pz_num:+.2f}"
            pz_col = "var(--pipeline-green)" if pz_num >= 1.645 else ("var(--pipeline-red)" if pz_num <= -1.645 else "var(--pipeline-amber)")
        else:
            pz_text = "--"
            pz_col = "var(--pipeline-text-muted)"

        dur = pm.get("duration_sec")
        if is_skipped:
            p_eta_text = "--"
        elif is_curr_pool:
            mode_elapsed = data.get("mode_elapsed_seconds")
            if not mode_elapsed:
                unit_sec = float((unit_data or {}).get("duration_sec", 0.0) or 0.0)
                depth_sec = float((depth_data or {}).get("duration_sec", 0.0) or 0.0)
                mode_elapsed = max(0.0, float(data.get("elapsed_seconds", 0.0) or 0.0) - unit_sec - depth_sec)
            p_eta_text = format_duration(float(mode_elapsed)) if mode_elapsed > 0 else "0:00"
        elif p_status_text in ("BETTER", "EQUAL", "WORSE", "PASSED", "PARITY", "FAILED", "KEEP", "REVERT") or dur:
            p_eta_text = format_duration(float(dur)) if (dur and float(dur) > 0) else "--"
        else:
            p_eta_text = "--"

        p_badge = f'<span class="status-chip status-chip-{p_status_class} matrix-col-status matrix-status-{p_status_class}">{p_status_text}</span>'
        p_row_class = "active-running" if is_curr_pool else ""
        pool_rows.append(f'''
        <tr class="{p_row_class}">
          <td class="td-left" style="font-weight: 700; color: var(--pipeline-text-main);">
            <span>{bm["name"]}</span>
          </td>
          <td class="td-right" style="font-weight: 700;">{p_games_text}</td>
          <td class="td-right">{ws_html}</td>
          <td class="td-right" style="font-weight: 700; color: {pd_col};">{p_delta_text}</td>
          <td class="td-right" style="font-weight: 600; color: {pz_col};">{pz_text}</td>
          <td class="td-right" style="color: var(--pipeline-text-muted); font-size: 0.82rem;" id="pool-eta-{mkey}">{p_eta_text}</td>
          <td class="td-right">{p_badge}</td>
        </tr>''')

    pool_rows_html = "\n".join(pool_rows)

    # Execute string replacements
    out = template_html
    out = out.replace(
        '<div style="font-size: 0.85rem; color: var(--text-muted);" id="headerSubtext">Testing: Unspecified Experiment</div>',
        f'<div style="font-size: 0.85rem; color: var(--text-muted);" id="headerSubtext" title="Testing: {exp_desc}">Testing: {exp_desc}</div>'
    )
    out = out.replace(
        '<strong class="header-stat-val" id="headerStatElapsed">--</strong>',
        f'<strong class="header-stat-val" id="headerStatElapsed">{elapsed_clock_display}</strong>'
    )
    out = out.replace(
        '<strong class="header-stat-val" id="headerStatLeft">--</strong>',
        f'<strong class="header-stat-val" id="headerStatLeft">{left_display}</strong>'
    )
    out = out.replace(
        '<strong class="header-stat-val" id="headerStatRate">--</strong>',
        f'<strong class="header-stat-val" id="headerStatRate">{rate_display}</strong>'
    )
    out = out.replace(
        '<div class="header-progress-bar" id="headerProgressBar" style="width: 0%;"></div>',
        f'<div class="header-progress-bar" id="headerProgressBar" style="width: {run_progress_pct:.1f}%;"></div>'
    )
    out = out.replace(
        '<strong class="header-stat-val" id="threadsCount">--</strong>',
        f'<strong class="header-stat-val" id="threadsCount">{threads_display}</strong>'
    )
    out = out.replace(
        '<!-- Dynamically populated test rows -->',
        unit_tests_html
    )
    status_badge_style = ' style="display: inline-flex;"' if status_is_visible else ' style="display: none;"'
    out = out.replace(
        '<div class="badge" id="statusBadge">',
        f'<div class="badge badge-status-{status_chip_class}" id="statusBadge"{status_badge_style}>'
    )
    out = out.replace(
        '<span id="statusText">IDLE</span>',
        f'<span id="statusText">{status_chip_text}</span>'
    )
    if not is_empty:
        raw_target = data.get("target")
        if raw_target is not None and str(raw_target).strip() and str(raw_target).strip().upper() not in ("NONE", "NULL"):
            norm = str(raw_target).strip().upper()
            if "/" in norm and "PARITY" in norm:
                norm = "PARITY"
            target_str = f"🎯 {norm}"
        else:
            target_str = "🎯 NOT SET"
        out = out.replace(
            '<div class="badge badge-target" id="targetBadge" style="display: none;">\n        <span id="targetText">🎯 IMPROVEMENT</span>\n      </div>',
            f'<div class="badge badge-target" id="targetBadge" style="display: inline-flex;">\n        <span id="targetText">{html.escape(target_str)}</span>\n      </div>'
        )
    out = out.replace(
        '<!-- Dynamically populated rows for Constrictor, Royale, Duel, Standard -->',
        matrix_rows_html
    )
    pool_modes_rendered = render_pool_modes_rows(pool_data, is_running=is_running, run_type=run_type)
    out = out.replace(
        '<!-- Dynamically populated rows for Pool Modes -->',
        pool_modes_rendered
    )

    active_stage = "self"
    if requested_stage in ("unit", "depth", "self", "pool"):
        active_stage = requested_stage
    elif status == "TEST_FAILURE" or (unit_data.get("status") == "FAILED"):
        active_stage = "unit"
    elif run_type == "depth":
        active_stage = "depth"
    elif run_type == "pool":
        active_stage = "pool"
    else:
        active_stage = "self"

    if active_stage != "self":
        out = out.replace(
            '<div class="stage-panel-content" id="panel-self" style="display: block;">',
            '<div class="stage-panel-content" id="panel-self" style="display: none;">'
        )
        out = out.replace(
            f'<div class="stage-panel-content" id="panel-{active_stage}" style="display: none;">',
            f'<div class="stage-panel-content" id="panel-{active_stage}" style="display: block;">'
        )

    unit_active = " active" if active_stage == "unit" else ""
    depth_active = " active" if active_stage == "depth" else ""
    matrix_active = " active" if active_stage == "self" else ""
    pool_active = " active" if active_stage == "pool" else ""

    # Derive rail statuses: BETTER, EQUAL, WORSE
    def py_derive_depth_status(d_data):
        if d_data.get("regression") is True or d_data.get("passed") is False:
            return "worse", "WORSE"
        delta_d = float(d_data.get("delta_depth") if d_data.get("delta_depth") is not None else (d_data.get("overall_delta_d") or 0.0))
        if delta_d > 0.0:
            return "better", "BETTER"
        return "equal", "EQUAL"

    def py_derive_matrix_status(m_list, r_rec, s_stop):
        if interrupted_stage == "self":
            if r_rec in ("REVERT", "REJECT", "WORSE") or s_stop in ("REVERT", "FAILED", "DISCONNECTED"):
                return "worse", "WORSE"
        failed_cnt = sum(1 for m in m_list if m.get("status") in ("FAILED", "WORSE", "REVERT"))
        if failed_cnt > 0:
            return "worse", "WORSE"
        better_cnt = sum(1 for m in m_list if m.get("status") in ("BETTER", "KEEP", "PASSED"))
        equal_cnt = sum(1 for m in m_list if m.get("status") in ("EQUAL", "PARITY"))
        if better_cnt > 0 and equal_cnt == 0:
            return "better", "BETTER"
        if equal_cnt > 0 or len(m_list) > 0:
            return "equal", "EQUAL"
        if r_rec in ("KEEP", "BETTER") or s_stop == "KEEP":
            return "better", "BETTER"
        if r_rec in ("PARITY", "EQUAL") or s_stop in ("PARITY", "MATRIX_COMPLETE"):
            return "equal", "EQUAL"
        return "worse", "WORSE"
        return "equal", "EQUAL"

    def py_derive_pool_status(p_data, r_rec):
        if r_rec in ("REVERT", "REJECT", "WORSE"):
            return "worse", "WORSE"
        p_dec = str(p_data.get("decision") or "").upper()
        if p_dec in ("FAILED", "REVERT", "WORSE"):
            return "worse", "WORSE"
        if p_dec in ("KEEP", "BETTER") or r_rec in ("KEEP", "BETTER"):
            return "better", "BETTER"
        if p_dec in ("PARITY", "EQUAL") or r_rec in ("PARITY", "EQUAL"):
            return "equal", "EQUAL"
        p_delta = p_data.get("delta")
        if p_delta is None and (p_data.get("cand_share") is not None and p_data.get("base_share") is not None):
            p_delta = float(p_data["cand_share"]) - float(p_data["base_share"])
        p_delta = float(p_delta or 0.0)
        p_z = float(p_data.get("z_score") or 0.0)
        if p_delta > 0 and p_z >= 1.645:
            return "better", "BETTER"
        if p_delta >= 0 or p_z > -1.645:
            return "equal", "EQUAL"
        return "worse", "WORSE"

    # Unit Tests and Depth Benchmark stage rail presentation
    unit_status = unit_data.get("status", "PASSED" if is_running else ("QUEUED" if is_empty else "PENDING"))
    unit_dur = unit_data.get("duration_sec")
    unit_dur_str = format_duration(float(unit_dur), allow_decimal=True) if unit_dur else ("0:08" if is_running else "--")
    unit_status_clean = "equal" if unit_status in ("PASSED", "DONE", "PASS") else ("worse" if unit_status == "FAILED" else ("running" if unit_status == "RUNNING" else "queued"))
    unit_status_text = "EQUAL" if unit_status in ("PASSED", "DONE", "PASS") else ("WORSE" if unit_status == "FAILED" else ("RUNNING" if unit_status == "RUNNING" else "QUEUED"))
    out = out.replace(
        '<div class="stage-segment pending" id="railSegment-unit">\n        <span class="stage-seg-index">1</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Unit Tests</span>\n          <span class="stage-seg-duration" id="railDuration-unit">--</span>\n        </div>\n        <span class="stage-seg-status matrix-status-queued" id="railStatus-unit">QUEUED</span>\n      </div>',
        f'<div class="stage-segment {unit_status_clean}{unit_active}" id="railSegment-unit">\n        <span class="stage-seg-index">1</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Unit Tests</span>\n          <span class="stage-seg-duration" id="railDuration-unit">{unit_dur_str}</span>\n        </div>\n        <span class="stage-seg-status matrix-status-{unit_status_clean}" id="railStatus-unit">{unit_status_text}</span>\n      </div>'
    )

    # Depth rail
    depth_data = data.get("depth_data") or {}
    depth_dur = depth_data.get("duration_sec")
    depth_dur_str = format_duration(float(depth_dur)) if depth_dur else ("0:48" if is_running else "--")

    matrix_dur_val = data.get("matrix_duration_sec")
    pool_dur_val = data.get("pool_duration_sec") or (data.get("pool_data") or {}).get("duration_sec")
    rec = str(data.get("recommendation") or "").upper()

    if is_running:
        if run_type == "depth":
            depth_status_clean = "running"
            depth_status_text = "RUNNING"
            depth_dur_str = elapsed_str
            matrix_rail_class = "pending"
            matrix_rail_status = "PENDING"
            matrix_dur_str = "--"
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
        elif run_type == "pool":
            depth_status_clean, depth_status_text = py_derive_depth_status(depth_data)
            if matrix_dur_val or data.get("self_play_matrix"):
                matrix_rail_class, matrix_rail_status = py_derive_matrix_status(data.get("self_play_matrix", matrix_list), rec, data.get("stop_type"))
                matrix_dur_str = format_duration(float(matrix_dur_val)) if matrix_dur_val else "--"
            else:
                matrix_rail_class = "skipped"
                matrix_rail_status = "SKIPPED"
                matrix_dur_str = "--"
            pool_rail_class = "running"
            pool_rail_status = "RUNNING"
            pool_dur_str = elapsed_str
        else:
            depth_status_clean, depth_status_text = py_derive_depth_status(depth_data)
            matrix_rail_class = "running"
            matrix_rail_status = "RUNNING"
            matrix_dur_str = elapsed_str
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
    elif is_aborted:
        if interrupted_stage == "unit":
            unit_status_clean = "failed"
            unit_status_text = "FAILED"
            depth_status_clean = "pending"
            depth_status_text = "QUEUED"
            depth_dur_str = "--"
            matrix_rail_class = "pending"
            matrix_rail_status = "QUEUED"
            matrix_dur_str = "--"
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
        elif interrupted_stage == "depth":
            unit_status_clean = "equal"
            unit_status_text = "EQUAL"
            depth_status_clean = "worse"
            depth_status_text = "WORSE"
            depth_dur_str = format_duration(float(depth_dur)) if depth_dur else elapsed_str
            matrix_rail_class = "pending"
            matrix_rail_status = "QUEUED"
            matrix_dur_str = "--"
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
        elif interrupted_stage == "self":
            unit_status_clean = "equal"
            unit_status_text = "EQUAL"
            depth_status_clean, depth_status_text = py_derive_depth_status(depth_data)
            matrix_rail_class = "worse"
            matrix_rail_status = "WORSE"
            matrix_dur_str = format_duration(float(matrix_dur_val)) if matrix_dur_val else elapsed_str
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
        else:
            unit_status_clean = "equal"
            unit_status_text = "EQUAL"
            depth_status_clean, depth_status_text = py_derive_depth_status(depth_data)
            matrix_rail_class, matrix_rail_status = py_derive_matrix_status(matrix_list, rec, data.get("stop_type"))
            if matrix_dur_val:
                matrix_dur_str = format_duration(float(matrix_dur_val))
            else:
                sum_m_sec = sum(float(m.get("duration_sec", 0.0) or 0.0) for m in matrix_list)
                matrix_dur_str = format_duration(sum_m_sec) if sum_m_sec > 0 else "--"
            pool_rail_class = "worse"
            pool_rail_status = "WORSE"
            pool_dur_str = format_duration(float(pool_dur_val)) if pool_dur_val else "--"
    elif is_complete:
        unit_status_clean = "worse" if unit_status == "FAILED" else "equal"
        unit_status_text = "WORSE" if unit_status == "FAILED" else "EQUAL"
        if depth_dur or (depth_data.get("completed_positions", 0) >= 12):
            depth_status_clean, depth_status_text = py_derive_depth_status(depth_data)
        else:
            depth_status_clean = "pending"
            depth_status_text = "QUEUED"
        if run_type == "pool":
            if matrix_dur_val or data.get("self_play_matrix"):
                matrix_rail_class, matrix_rail_status = py_derive_matrix_status(data.get("self_play_matrix", matrix_list), rec, data.get("stop_type"))
                matrix_dur_str = format_duration(float(matrix_dur_val)) if matrix_dur_val else "--"
            else:
                matrix_rail_class = "skipped"
                matrix_rail_status = "SKIPPED"
                matrix_dur_str = "--"
            pool_rail_class, pool_rail_status = py_derive_pool_status(pool_data, rec)
            pool_dur_str = format_duration(float(pool_dur_val)) if pool_dur_val else elapsed_str
        else:
            matrix_rail_class, matrix_rail_status = py_derive_matrix_status(matrix_list, rec, data.get("stop_type"))
            matrix_dur_str = format_duration(float(matrix_dur_val)) if matrix_dur_val else elapsed_str
            pool_rail_class = "pending"
            pool_rail_status = "QUEUED"
            pool_dur_str = "--"
    else:
        unit_status_clean = "queued"
        unit_status_text = "QUEUED"
        depth_status_clean = "queued"
        depth_status_text = "QUEUED"
        depth_dur_str = "--"
        matrix_rail_class = "pending"
        matrix_rail_status = "QUEUED"
        matrix_dur_str = "--"
        pool_rail_class = "pending"
        pool_rail_status = "QUEUED"
        pool_dur_str = "--"

    out = out.replace(
        '<div class="stage-segment pending" id="railSegment-depth">\n        <span class="stage-seg-index">2</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Depth Benchmark</span>\n          <span class="stage-seg-duration" id="railDuration-depth">--</span>\n        </div>\n        <span class="stage-seg-status matrix-status-queued" id="railStatus-depth">QUEUED</span>\n      </div>',
        f'<div class="stage-segment {depth_status_clean}{depth_active}" id="railSegment-depth">\n        <span class="stage-seg-index">2</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Depth Benchmark</span>\n          <span class="stage-seg-duration" id="railDuration-depth">{depth_dur_str}</span>\n        </div>\n        <span class="stage-seg-status matrix-status-{depth_status_clean}" id="railStatus-depth">{depth_status_text}</span>\n      </div>'
    )
    out = out.replace(
        '<div class="stage-segment active pending" id="railSegment-self">\n        <span class="stage-seg-index">3</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Self-Play Matrix</span>\n          <span class="stage-seg-duration" id="railDuration-self">--</span>\n        </div>\n        <span class="stage-seg-status matrix-status-queued" id="railStatus-self">QUEUED</span>\n      </div>',
        f'<div class="stage-segment {matrix_rail_class}{matrix_active}" id="railSegment-self">\n        <span class="stage-seg-index">3</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Self-Play Matrix</span>\n          <span class="stage-seg-duration" id="railDuration-self">{matrix_dur_str}</span>\n        </div>\n        <span class="stage-seg-status matrix-status-{matrix_rail_status.lower()}" id="railStatus-self">{matrix_rail_status}</span>\n      </div>'
    )
    out = out.replace(
        '<div class="stage-segment pending" id="railSegment-pool">\n        <span class="stage-seg-index">4</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Zoo Pool Gate</span>\n          <span class="stage-seg-duration" id="railDuration-pool">--</span>\n        </div>\n        <span class="stage-seg-status matrix-status-queued" id="railStatus-pool">QUEUED</span>\n      </div>',
        f'<div class="stage-segment {pool_rail_class}{pool_active}" id="railSegment-pool">\n        <span class="stage-seg-index">4</span>\n        <div class="stage-seg-info">\n          <span class="stage-seg-name">Zoo Pool Gate</span>\n          <span class="stage-seg-duration" id="railDuration-pool">{pool_dur_str}</span>\n        </div>\n        <span class="stage-seg-status matrix-status-{pool_rail_status.lower().replace(" ", "-")}" id="railStatus-pool">{pool_rail_status}</span>\n      </div>'
    )

    depth_rows_html = render_depth_mode_summary_rows(depth_data)
    out = out.replace(
        '<!-- Dynamically populated depth mode rows -->',
        depth_rows_html
    )

    # Pre-render history table
    history_list = data.get("run_history") or []
    if history_list:
        h_count_str = f"{len(history_list)} Run{'s' if len(history_list) != 1 else ''}"
        h_rows_html = render_history_rows(history_list)

        out = out.replace(
            '<tr class="empty-row">\n            <td colspan="10" class="empty-cell" style="text-align: center; color: var(--pipeline-text-muted); padding: 3rem;">No runs yet</td>\n          </tr>',
            h_rows_html
        )
        out = out.replace(
            '<span class="panel-meta" id="historyCount">0 Runs</span>',
            f'<span class="panel-meta" id="historyCount">{h_count_str}</span>'
        )
    else:
        pass

    # Apply UI-state wrappers and visibility rules
    if is_empty:
        out = out.replace(
            '<div style="font-size: 0.85rem; color: var(--text-muted);" id="headerSubtext">Testing: Unspecified Experiment</div>',
            '<div style="font-size: 0.85rem; color: var(--text-muted);" id="headerSubtext">No data.</div>'
        )
        out = out.replace(
            '<span id="headerNoData" style="display: none;',
            '<span id="headerNoData" style="display: inline-flex;'
        )
    elif is_aborted:
        stop_reason = str(data.get("stop_reason") or "Simulation runner terminated unexpectedly.")
        comp_line = compute_completeness_line(data)
        out = out.replace(
            '<div id="abortedBanner" class="banner revert" style="display: none;',
            '<div id="abortedBanner" class="banner revert" style="display: block;'
        )
        out = out.replace(
            '<div class="banner-desc" id="abortedBannerReason" style="font-weight: 600; font-family: \'JetBrains Mono\', monospace; margin-bottom: 0.25rem;"></div>',
            f'<div class="banner-desc" id="abortedBannerReason" style="font-weight: 600; font-family: \'JetBrains Mono\', monospace; margin-bottom: 0.25rem;">{html.escape(stop_reason)}</div>'
        )
        out = out.replace(
            '<div class="banner-desc" id="abortedBannerDetail" style="color: var(--text-muted); font-size: 0.85rem;"></div>',
            f'<div class="banner-desc" id="abortedBannerDetail" style="color: var(--text-muted); font-size: 0.85rem;">{html.escape(comp_line)}</div>'
        )
        out = out.replace('<div class="header-progress-wrap" id="headerProgressWrap" title="Overall Run Progress">', '<div class="header-progress-wrap" id="headerProgressWrap" title="Overall Run Progress" style="display: none;">')

        stg_id = "matrix" if interrupted_stage == "self" else interrupted_stage
        if stg_id:
            out = out.replace(f'<div id="{stg_id}TableWrap">', f'<div id="{stg_id}TableWrap" style="display: none;">')
            out = out.replace(
                f'<div id="{stg_id}AbortedWrap" style="display: none; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: \'JetBrains Mono\', monospace;"></div>',
                f'<div id="{stg_id}AbortedWrap" style="display: block; text-align: center; color: var(--pipeline-amber); padding: 2.5rem; font-size: 0.95rem; font-family: \'JetBrains Mono\', monospace;">{html.escape(stop_reason)}</div>'
            )
    elif is_complete:
        pass

    return out


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

        try:
            parsed_url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed_url.query)
            req_stage = qs.get("stage", [None])[0]
            html_to_send = render_html_with_state(DASHBOARD_HTML, self.state.to_dict(), requested_stage=req_stage) if self.state else DASHBOARD_HTML
        except Exception as e:
            logger.warning(f"Error rendering SSR HTML, falling back to base template: {e}")
            html_to_send = DASHBOARD_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_to_send.encode("utf-8"))

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
        target: Optional[str] = None,
        pool_enabled: bool = False,
    ):
        if not target:
            raise ValueError("Missing mandatory parameter: 'target'")
        payload = {
            "action": "init",
            "pid": pid if pid is not None else os.getpid(),
            "run_type": run_type,
            "target": target,
            "pool_enabled": pool_enabled,
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

    def set_finished(
        self,
        stop_type: str,
        stop_reason: str,
        recommendation: str,
        csv_path: str,
        duration_sec: Optional[float] = None,
        pool_modes: Optional[list] = None,
        opponents: Optional[list] = None,
    ):
        payload = {
            "action": "set_finished",
            "stop_type": stop_type,
            "stop_reason": stop_reason,
            "recommendation": recommendation,
            "csv_path": csv_path,
        }
        if duration_sec is not None:
            payload["duration_sec"] = float(duration_sec)
        if pool_modes is not None:
            payload["pool_modes"] = pool_modes
        if opponents is not None:
            payload["opponents"] = opponents
        self.post_update(payload, sync=True)

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

    def update_unit_tests(self, status: str = "PASSED", passed: Optional[int] = None, total: Optional[int] = None, details: str = "", duration_sec: float = 0.0, tests: Optional[List[Dict[str, Any]]] = None):
        if tests is None:
            try:
                import sys
                mod = sys.modules.get("tests.test_strategy")
                if mod and hasattr(mod, "LAST_TEST_RECORDS") and getattr(mod, "LAST_TEST_RECORDS"):
                    tests = getattr(mod, "LAST_TEST_RECORDS")
            except Exception:
                pass
        computed_total = len(tests) if tests else (total or 0)
        computed_passed = sum(1 for t in tests if str(t.get("status", "")).upper() == "PASSED") if tests else (passed if passed is not None else 0)
        payload = {
            "action": "update_unit_tests",
            "status": status,
            "passed": computed_passed,
            "total": computed_total,
            "details": details,
            "duration_sec": duration_sec,
        }
        if tests is not None:
            payload["tests"] = tests
        self.post_update(payload, sync=True)

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
