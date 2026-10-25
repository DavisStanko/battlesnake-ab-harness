"""
Unit tests for Battlesnake A/B Dashboard skip feature, endpoints, and client.
"""

import os
import sys
import unittest
import math
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ab_dashboard import (
    DashboardState,
    DashboardHandler,
    DashboardClient,
    start_dashboard_server,
)
from ab_test import _run_single_mode_simulation, THREADS


class TestDashboardSkip(unittest.TestCase):
    def setUp(self):
        self.state = DashboardState()
        DashboardHandler.state = self.state

    def test_state_skip_flag(self):
        self.assertFalse(self.state.skip_requested)
        self.state.skip_requested = True
        self.assertTrue(self.state.to_dict()["skip_requested"])

        # Reset on init
        self.state.handle_update({"action": "init"})
        self.assertFalse(self.state.skip_requested)
        self.assertFalse(self.state.to_dict()["skip_requested"])

    def test_client_endpoints_and_atomic_consume(self):
        # Start server on dynamic test port
        server, port = start_dashboard_server(self.state, preferred_port=0, timeout_seconds=60)
        try:
            client = DashboardClient(port=port)
            self.assertTrue(client.is_running())

            # Initially false
            self.assertFalse(client.check_skip())

            # Action endpoints return 405 error
            resp_skip = client.skip_mode()
            self.assertEqual(resp_skip.get("status"), "error")
            self.assertEqual(resp_skip.get("code"), 405)

            resp_cancel = client.cancel_test()
            self.assertEqual(resp_cancel.get("status"), "error")
            self.assertEqual(resp_cancel.get("code"), 405)

            # Direct state manipulation for atomic consume
            with self.state.lock:
                self.state.skip_requested = True

            # Atomic consume returns True first time
            self.assertTrue(client.check_skip())

            # Subsequent check returns False because it was consumed
            self.assertFalse(client.check_skip())
        finally:
            server.shutdown()
            server.server_close()

    def test_skip_simulation_loop(self):
        # Start server on dynamic test port
        server, port = start_dashboard_server(self.state, preferred_port=0, timeout_seconds=60)
        try:
            client = DashboardClient(port=port)
            self.assertTrue(client.is_running())

            # Background thread triggers skip via internal state
            def _trigger_skip():
                time.sleep(0.02)
                with self.state.lock:
                    self.state.skip_requested = True

            t = threading.Thread(target=_trigger_skip, daemon=True)
            t.start()

            # Run duel simulation with up to 50 games
            res = _run_single_mode_simulation(
                mode="duel",
                mode_name="DUEL 1v1",
                num_snakes=2,
                target_share=50.0,
                high_stop=60.0,
                low_stop=40.0,
                min_games=50,
                max_games=50,
                check_interval=50,
                threads=THREADS,
                dash_client=client,
                desc="Test Skip Mode",
                is_bug_fix=False,
                keep_threshold=51.5,
                base_seed=12345,
            )

            self.assertEqual(res["stop_type"], "USER_SKIPPED")
            self.assertEqual(res["decision"], "SKIPPED")
            self.assertFalse(res["passed"])
            self.assertIn("skipped by user", res["stop_reason"].lower())
            self.assertLess(res["total_played"], 50)
        finally:
            server.shutdown()
            server.server_close()




    def test_matrix_skip_advances_to_next_mode(self):
        server, port = start_dashboard_server(self.state, preferred_port=0, timeout_seconds=60)
        try:
            client = DashboardClient(port=port)
            self.assertTrue(client.is_running())

            matrix = [
                {"mode": "duel", "name": "DUEL 1v1", "num_snakes": 2, "target_share": 50.0, "high_stop": 60.0, "low_stop": 40.0},
                {"mode": "standard", "name": "STANDARD 4P", "num_snakes": 4, "target_share": 25.0, "high_stop": 35.0, "low_stop": 18.0},
            ]
            matrix_progress = [
                {"mode": m["mode"], "name": m["name"], "status": "PENDING", "win_share": 0.0, "games": 0, "decision": "PENDING", "reason": ""}
                for m in matrix
            ]

            # In background, skip duel after 0.02s, then skip standard after duel finishes
            def _skip_controller():
                time.sleep(0.02)
                with self.state.lock:
                    self.state.skip_requested = True
                # Wait until standard mode is initialized
                while True:
                    time.sleep(0.01)
                    if client.is_running():
                        try:
                            import urllib.request, json
                            with urllib.request.urlopen(f"http://localhost:{port}/api/data") as resp:
                                data = json.loads(resp.read().decode())
                                if data.get("game_mode") == "standard":
                                    time.sleep(0.02)
                                    with self.state.lock:
                                        self.state.skip_requested = True
                                    break
                        except Exception:
                            pass

            t = threading.Thread(target=_skip_controller, daemon=True)
            t.start()

            results = []
            for idx, m in enumerate(matrix):
                matrix_progress[idx]["status"] = "RUNNING"
                client.update_matrix(matrix_progress)

                res = _run_single_mode_simulation(
                    mode=m["mode"],
                    mode_name=m["name"],
                    num_snakes=m["num_snakes"],
                    target_share=m["target_share"],
                    high_stop=m["high_stop"],
                    low_stop=m["low_stop"],
                    min_games=50,
                    max_games=50,
                    check_interval=50,
                    threads=THREADS,
                    dash_client=client,
                    desc="Test Multi Matrix Skip",
                    is_bug_fix=False,
                    keep_threshold=m["target_share"],
                    matrix_progress=matrix_progress,
                    base_seed=1000 + idx * 100,
                )
                results.append(res)
                if res["stop_type"] == "USER_SKIPPED":
                    matrix_progress[idx]["status"] = "SKIPPED"
                    matrix_progress[idx]["decision"] = "SKIPPED"
                else:
                    matrix_progress[idx]["status"] = "PASSED" if res["passed"] else "FAILED"
                client.update_matrix(matrix_progress)

            # Both modes should have been executed in sequence and skipped
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0]["mode"], "duel")
            self.assertEqual(results[0]["stop_type"], "USER_SKIPPED")
            self.assertEqual(results[1]["mode"], "standard")
            self.assertEqual(results[1]["stop_type"], "USER_SKIPPED")
            self.assertEqual(matrix_progress[0]["status"], "SKIPPED")
            self.assertEqual(matrix_progress[1]["status"], "SKIPPED")
        finally:
            server.shutdown()
            server.server_close()

    def test_modes_matrix_order_quickest_to_longest(self):
        from ab_test import MODES_MATRIX
        modes = [m["mode"] for m in MODES_MATRIX]
        self.assertEqual(modes, ["constrictor", "royale", "duel", "standard"])

    def test_live_matrix_progress_update(self):
        self.state.handle_update({
            "action": "init",
            "num_snakes": 4,
            "strategy_variant": "variant",
            "strategy_baseline": "baseline",
            "matrix_progress": [
                {"mode": "constrictor", "name": "Constrictor 4P", "status": "RUNNING", "win_share": 0.0, "games": 0},
                {"mode": "royale", "name": "Royale 4P", "status": "PENDING", "win_share": 0.0, "games": 0},
            ]
        })
        # Record 4 games: variant wins 2, baseline_1 wins 1, baseline_2 wins 1
        self.state.update_game(1, 101, "variant")
        self.state.update_game(2, 102, "variant")
        self.state.update_game(3, 103, "baseline_1")
        self.state.update_game(4, 104, "baseline_2")

        d = self.state.to_dict()
        self.assertEqual(d["total_played"], 4)
        self.assertEqual(d["variant"]["win_share"], 50.0)

        # Verify live matrix progress for running mode reflects live win share and games
        running_mode = d["matrix_progress"][0]
        self.assertEqual(running_mode["status"], "RUNNING")
        self.assertEqual(running_mode["win_share"], 50.0)
        self.assertEqual(running_mode["games"], 4)

        pending_mode = d["matrix_progress"][1]
        self.assertEqual(pending_mode["status"], "PENDING")
        self.assertEqual(pending_mode["win_share"], 0.0)

    def test_matrix_chip_running_status_fallback(self):
        # Simulate race condition where matrix_progress has duel as PENDING even though duel is running
        self.state.handle_update({
            "action": "init",
            "game_mode": "duel",
            "num_snakes": 2,
            "strategy_variant": "variant",
            "strategy_baseline": "baseline",
            "matrix_progress": [
                {"mode": "royale", "name": "Royale 4P", "status": "PARITY", "win_share": 23.5, "games": 200},
                {"mode": "duel", "name": "DUEL 1v1", "status": "PENDING", "win_share": 0.0, "games": 0},
            ]
        })
        self.state.update_game(1, 201, "variant")
        d = self.state.to_dict()
        self.assertEqual(d["matrix_progress"][1]["status"], "RUNNING")
        self.assertEqual(d["matrix_progress"][1]["games"], 1)
        self.assertEqual(d["matrix_progress"][1]["win_share"], 100.0)

    def test_draw_rounding_and_formatting(self):
        from ab_test import save_run_history
        import json
        import tempfile
        from pathlib import Path

        # Fractional draws in 4-player game (e.g. 1 draw shared amongst 3 baselines = 0.3333333333333333)
        results = {
            "baseline": {"wins": 1, "losses": 2, "draws": 0.3333333333333333},
            "variant": {"wins": 2, "losses": 1, "draws": 0.25},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_path = save_run_history(
                "baseline", "variant", 3, results,
                "TEST_COMPLETE", "Completed test", "KEEP",
                experiment_desc="Test Draw Rounding",
                results_dir=Path(tmpdir),
                matrix_results=[
                    {
                        "mode": "royale",
                        "results": {
                            "baseline": {"wins": 1, "losses": 2, "draws": 1.6666666666666667},
                            "variant": {"wins": 2, "losses": 1, "draws": 0.25},
                        }
                    }
                ]
            )
            with open(hist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            last_entry = data[-1]
            self.assertEqual(last_entry["baseline"]["draws"], 0.33)
            self.assertEqual(last_entry["variant"]["draws"], 0.25)
            self.assertEqual(last_entry["matrix_results"][0]["results"]["baseline"]["draws"], 1.67)

    def test_4p_initial_and_zero_win_display(self):
        self.state.handle_update({
            "action": "init",
            "num_snakes": 4,
            "strategy_variant": "variant",
            "strategy_baseline": "baseline",
        })
        d = self.state.to_dict()
        # When played == 0 in 4P under Option A, variant is 25% and baseline team is 75%
        self.assertEqual(d["total_played"], 0)
        self.assertEqual(len(d["snakes_list"]), 2)
        var_entry = next(s for s in d["snakes_list"] if s["is_variant"])
        base_entry = next(s for s in d["snakes_list"] if not s["is_variant"])
        self.assertEqual(var_entry["win_share"], 25.0)
        self.assertEqual(var_entry["wins"], 0)
        self.assertEqual(base_entry["win_share"], 75.0)
        self.assertEqual(base_entry["wins"], 0)

    def test_matrix_early_exit_on_mode_failure(self):
        # Simulate active matrix where first mode fails
        active_matrix = [
            {"mode": "constrictor", "name": "Constrictor 4P", "num_snakes": 4, "target_share": 25.0, "high_stop": 35.0, "low_stop": 18.0},
            {"mode": "royale", "name": "Royale 4P", "num_snakes": 4, "target_share": 25.0, "high_stop": 35.0, "low_stop": 18.0},
            {"mode": "duel", "name": "Duel 1v1", "num_snakes": 2, "target_share": 50.0, "high_stop": 60.0, "low_stop": 40.0},
        ]
        matrix_progress = [
            {"mode": m["mode"], "name": m["name"], "status": "PENDING", "win_share": 0.0, "games": 0, "decision": "PENDING", "reason": ""}
            for m in active_matrix
        ]
        matrix_results = []
        for idx, m in enumerate(active_matrix):
            matrix_progress[idx]["status"] = "RUNNING"
            # Simulate first mode failing
            if idx == 0:
                res = {
                    "mode": m["mode"], "name": m["name"], "passed": False, "decision": "REVERT",
                    "stop_type": "LOW_STOP_EARLY", "stop_reason": "Low stop cutoff",
                    "var_win_share": 15.0, "target_share": 25.0, "total_played": 50,
                }
            else:
                res = {"mode": m["mode"], "name": m["name"], "passed": True, "decision": "KEEP", "stop_type": "COMPLETE", "stop_reason": "ok", "var_win_share": 30.0, "target_share": 25.0, "total_played": 50}

            matrix_results.append(res)
            matrix_progress[idx]["status"] = "PASSED" if res["passed"] else "FAILED"
            matrix_progress[idx]["decision"] = res["decision"]

            if not res["passed"] and res.get("stop_type") != "USER_SKIPPED":
                for rem_idx in range(idx + 1, len(active_matrix)):
                    matrix_progress[rem_idx]["status"] = "SKIPPED"
                    matrix_progress[rem_idx]["decision"] = "SKIPPED"
                    matrix_progress[rem_idx]["reason"] = f"Skipped: Prior mode '{m['name']}' failed regression criteria"
                break

        # Only 1 mode ran because early exit stopped remaining modes
        self.assertEqual(len(matrix_results), 1)
        self.assertEqual(matrix_progress[0]["status"], "FAILED")
        self.assertEqual(matrix_progress[1]["status"], "SKIPPED")
        self.assertEqual(matrix_progress[2]["status"], "SKIPPED")

    def test_statistical_thresholds_and_z_score(self):
        from ab_test import get_statistical_thresholds, compute_z_score, Z_CRIT_EARLY

        # 1v1 tests (p0 = 50.0%)
        # At 30 games, se = 9.13%, high_stop ~ 75.6%, low_stop ~ 24.4%
        h30, l30 = get_statistical_thresholds(30, num_snakes=2)
        self.assertAlmostEqual(h30, 50.0 + Z_CRIT_EARLY * (math.sqrt(0.25 / 30) * 100), places=1)
        self.assertAlmostEqual(l30, 50.0 - Z_CRIT_EARLY * (math.sqrt(0.25 / 30) * 100), places=1)

        # Thresholds must narrow monotonically as sample size N grows
        h100, l100 = get_statistical_thresholds(100, num_snakes=2)
        h200, l200 = get_statistical_thresholds(200, num_snakes=2)
        self.assertGreater(h30, h100)
        self.assertGreater(h100, h200)
        self.assertLess(l30, l100)
        self.assertLess(l100, l200)

        # 4P tests (p0 = 25.0%)
        h4p_200, l4p_200 = get_statistical_thresholds(200, num_snakes=4)
        se_4p_200 = math.sqrt(0.25 * 0.75 / 200) * 100.0
        self.assertAlmostEqual(h4p_200, 25.0 + Z_CRIT_EARLY * se_4p_200, places=1)
        self.assertAlmostEqual(l4p_200, 25.0 - Z_CRIT_EARLY * se_4p_200, places=1)

        # z-score calculations
        z_parity, se = compute_z_score(50.0, 200, 2)
        self.assertAlmostEqual(z_parity, 0.0, places=2)

        # 60% in 1v1 over 200 games: (60 - 50) / 3.535 ~ +2.83
        z_high, _ = compute_z_score(60.0, 200, 2)
        self.assertGreater(z_high, 2.80)

        # 40% in 1v1 over 200 games: (40 - 50) / 3.535 ~ -2.83
        z_low, _ = compute_z_score(40.0, 200, 2)
        self.assertLess(z_low, -2.80)

    def test_three_outcome_decision_classification(self):
        from ab_test import classify_max_games_outcome, compute_z_score

        # Simulate outcome evaluation logic:
        def evaluate_mode_outcome(var_win_share, total_played, num_snakes, keep_thresh, stop_type="MAXIMUM_REACHED", max_opp_ws=25.0):
            z_final, se_final = compute_z_score(var_win_share, total_played, num_snakes)
            if stop_type == "EARLY_STOP_HIGH":
                return "KEEP", True
            elif stop_type == "EARLY_STOP_LOW":
                return "REVERT", False
            
            decision, passed, _ = classify_max_games_outcome(
                var_win_share, z_final, keep_thresh, num_snakes, max_opp_ws, se_final
            )
            return decision, passed

        # Case 1: Early stop high -> KEEP
        dec, passed = evaluate_mode_outcome(70.0, 60, 2, 51.5, stop_type="EARLY_STOP_HIGH")
        self.assertEqual(dec, "KEEP")
        self.assertTrue(passed)

        # Case 2: Early stop low -> REVERT
        dec, passed = evaluate_mode_outcome(30.0, 60, 2, 51.5, stop_type="EARLY_STOP_LOW")
        self.assertEqual(dec, "REVERT")
        self.assertFalse(passed)

        # Case 3: 200 games in 1v1 with 56.0% win share (z = +1.70 >= 1.645) -> KEEP (improvement)
        dec, passed = evaluate_mode_outcome(56.0, 200, 2, 51.5)
        self.assertEqual(dec, "KEEP")
        self.assertTrue(passed)

        # Case 4: 200 games in 1v1 with 49.5% win share (near parity) -> PARITY (Yellow)
        dec, passed = evaluate_mode_outcome(49.5, 200, 2, 51.5)
        self.assertEqual(dec, "PARITY")
        self.assertTrue(passed)

        # Case 5: 200 games in 4P with 20.5% win share (z = -1.47, within parity band) -> PARITY (Yellow)
        dec, passed = evaluate_mode_outcome(20.5, 200, 4, 25.5, max_opp_ws=26.0)
        self.assertEqual(dec, "PARITY")
        self.assertTrue(passed)

        # Case 6: 200 games in 4P with 17.0% win share (z = -2.61 <= -1.65) -> REVERT (confirmed regression)
        dec, passed = evaluate_mode_outcome(17.0, 200, 4, 25.5, max_opp_ws=27.0)
        self.assertEqual(dec, "REVERT")
        self.assertFalse(passed)

    def test_matrix_continuation_on_parity(self):
        # When a mode finishes in PARITY, the matrix MUST NOT halt early
        active_matrix = [
            {"mode": "constrictor", "name": "Constrictor 4P", "num_snakes": 4, "target_share": 25.0},
            {"mode": "royale", "name": "Royale 4P", "num_snakes": 4, "target_share": 25.0},
            {"mode": "duel", "name": "Duel 1v1", "num_snakes": 2, "target_share": 50.0},
        ]
        matrix_progress = [
            {"mode": m["mode"], "name": m["name"], "status": "PENDING", "win_share": 0.0, "games": 0, "decision": "PENDING", "reason": ""}
            for m in active_matrix
        ]
        matrix_results = []
        for idx, m in enumerate(active_matrix):
            # Mode 0 finishes with PARITY
            if idx == 0:
                res = {"mode": m["mode"], "name": m["name"], "passed": True, "decision": "PARITY", "stop_type": "MAXIMUM_REACHED", "stop_reason": "Parity band", "var_win_share": 22.0, "total_played": 200}
            elif idx == 1:
                res = {"mode": m["mode"], "name": m["name"], "passed": True, "decision": "KEEP", "stop_type": "MAXIMUM_REACHED", "stop_reason": "Improved", "var_win_share": 28.0, "total_played": 200}
            else:
                res = {"mode": m["mode"], "name": m["name"], "passed": True, "decision": "KEEP", "stop_type": "MAXIMUM_REACHED", "stop_reason": "Improved", "var_win_share": 54.0, "total_played": 200}

            matrix_results.append(res)
            matrix_progress[idx]["status"] = "PARITY" if res["decision"] == "PARITY" else ("PASSED" if res["passed"] else "FAILED")
            matrix_progress[idx]["decision"] = res["decision"]

            # Regression early stoppage check: ONLY halt on REVERT
            if res["decision"] == "REVERT" and res.get("stop_type") != "USER_SKIPPED":
                for rem_idx in range(idx + 1, len(active_matrix)):
                    matrix_progress[rem_idx]["status"] = "SKIPPED"
                break

        # All 3 modes ran successfully because PARITY did not trigger early stoppage
        self.assertEqual(len(matrix_results), 3)
        self.assertEqual(matrix_progress[0]["status"], "PARITY")
        self.assertEqual(matrix_progress[1]["status"], "PASSED")
        self.assertEqual(matrix_progress[2]["status"], "PASSED")

    def test_dashboard_parity_recommendation(self):
        # Test dashboard handling of PARITY recommendation
        self.state.set_finished(
            stop_type="MAXIMUM_REACHED",
            stop_reason="Parity confirmed with 95% confidence",
            recommendation="PARITY",
            csv_path="/tmp/test.csv"
        )
        d = self.state.to_dict()
        self.assertEqual(d["recommendation"], "PARITY")
        self.assertEqual(d["status"], "FINISHED")

    def test_matrix_chip_running_status_fallback(self):
        # When a mode starts and matrix_progress was sent with status PENDING,
        # to_dict() dynamically sets status to RUNNING for the active mode.
        matrix_prog = [
            {"mode": "duel", "name": "Duel 1v1", "status": "PENDING", "win_share": 0.0, "games": 0},
            {"mode": "standard", "name": "Standard 4P", "status": "PENDING", "win_share": 0.0, "games": 0},
        ]
        self.state.handle_update({
            "action": "init",
            "game_mode": "duel",
            "matrix_progress": matrix_prog,
        })
        d = self.state.to_dict()
        duel_item = next(m for m in d["matrix_progress"] if m["mode"] == "duel")
        self.assertEqual(duel_item["status"], "RUNNING")

    def test_spawn_balance_uniformity(self):
        from ab_test import _setup_initial_board
        corners = [(1, 1), (1, 9), (9, 1), (9, 9)]
        cardinals = [(1, 5), (5, 1), (5, 9), (9, 5)]

        # Verify 4-player spawn balance across 200 games
        v_counts_4p = {(x, y): 0 for x, y in corners + cardinals}
        for g in range(1, 201):
            _, snakes, _ = _setup_initial_board(100, game_num=g, num_snakes=4, mode="standard")
            v = next(s for s in snakes if s["id"] == "variant")
            v_counts_4p[(v["head"]["x"], v["head"]["y"])] += 1

        for pt in corners + cardinals:
            self.assertEqual(v_counts_4p[pt], 25, f"4P spawn at {pt} was visited {v_counts_4p[pt]} times, expected 25")

        # Verify 2-player spawn balance across 200 games
        v_counts_2p = {(x, y): 0 for x, y in corners + cardinals}
        b_counts_2p = {(x, y): 0 for x, y in corners + cardinals}
        for g in range(1, 201):
            _, snakes, _ = _setup_initial_board(100, game_num=g, num_snakes=2, mode="duel")
            v = next(s for s in snakes if s["id"] == "variant")
            b = next(s for s in snakes if s["id"] == "baseline")
            v_counts_2p[(v["head"]["x"], v["head"]["y"])] += 1
            b_counts_2p[(b["head"]["x"], b["head"]["y"])] += 1

        for pt in corners + cardinals:
            self.assertEqual(v_counts_2p[pt], 25, f"2P variant spawn at {pt} was visited {v_counts_2p[pt]} times, expected 25")
            self.assertEqual(b_counts_2p[pt], 25, f"2P baseline spawn at {pt} was visited {b_counts_2p[pt]} times, expected 25")

    def test_load_run_history_resolution(self):
        from ab_dashboard import load_run_history
        history = load_run_history()
        self.assertIsInstance(history, list)
        if len(history) > 0:
            last_entry = history[-1]
            self.assertIn("recommendation", last_entry)

    def test_consolidate_run_history(self):
        from ab_dashboard import consolidate_run_history
        raw = [
            {
                "run_id": 1,
                "timestamp": "2026-10-24 10:00:00",
                "experiment_desc": "Feature X",
                "total_games": 200,
                "matrix_results": [{"mode": "standard", "var_win_share": 25.5}],
                "depth_results": {"overall": {"delta_depth": 0.5}},
            },
            {
                "run_id": 2,
                "timestamp": "2026-10-24 10:15:00",
                "experiment_desc": "Feature X",
                "total_games": 20,
                "pool_data": [{"mode": "standard", "cand_win_share": 60.0}],
                "recommendation": "KEEP",
            },
        ]
        consolidated = consolidate_run_history(raw)
        self.assertEqual(len(consolidated), 1)
        merged = consolidated[0]
        self.assertEqual(merged["experiment_desc"], "Feature X")
        self.assertEqual(merged["timestamp"], "2026-10-24 10:15:00")
        self.assertIsNotNone(merged.get("matrix_results"))
        self.assertIsNotNone(merged.get("pool_data"))
        self.assertIsNotNone(merged.get("depth_results"))
        self.assertEqual(merged["total_games"], 200)
        self.assertEqual(merged["recommendation"], "KEEP")

    def test_save_run_history_merges_pool_benchmark(self):
        from ab_test import save_run_history
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            res_dir = Path(tmpdir)
            save_run_history(
                "baseline", "variant", 200,
                {"baseline": {"wins": 150, "losses": 50, "draws": 0}, "variant": {"wins": 50, "losses": 150, "draws": 0}},
                "COMPLETE", "Self-play complete", "PARITY",
                experiment_desc="Unified Experiment",
                results_dir=res_dir,
                matrix_results=[{"mode": "standard", "win_share": 25.0}],
                depth_results={"overall": {"delta_depth": 0.0}},
            )
            save_run_history(
                "baseline", "variant", 20,
                {"baseline": {"wins": 0, "losses": 0, "draws": 0}, "variant": {"wins": 0, "losses": 0, "draws": 0}},
                "COMPLETE", "Pool complete", "KEEP",
                experiment_desc="Unified Experiment",
                results_dir=res_dir,
                pool_data=[{"mode": "standard", "cand_win_share": 60.0}],
            )
            with open(res_dir / "ab_history.json", "r", encoding="utf-8") as f:
                history = json.load(f)
            self.assertEqual(len(history), 1)
            self.assertIsNotNone(history[0].get("matrix_results"))
            self.assertIsNotNone(history[0].get("pool_data"))
            self.assertEqual(history[0]["total_games"], 200)
            self.assertEqual(history[0]["recommendation"], "KEEP")

    def test_omitted_modes_render_skipped(self):
        from ab_dashboard import render_html_with_state, DASHBOARD_HTML
        data = {
            "status": "FINISHED",
            "recommendation": "PARITY",
            "game_mode": "constrictor,royale,standard",
            "matrix_progress": [
                {"mode": "constrictor", "name": "Constrictor 4P", "status": "PARITY", "games": 200, "max_games": 200, "win_share": 25.0},
                {"mode": "royale", "name": "Royale 4P", "status": "PARITY", "games": 200, "max_games": 200, "win_share": 25.0},
                {"mode": "standard", "name": "Standard 4P", "status": "PARITY", "games": 200, "max_games": 200, "win_share": 25.0},
            ],
            "pool_data": {
                "modes": {
                    "constrictor": {"cand_share": 33.8, "base_share": 37.5, "delta": -3.8, "decision": "PARITY", "games": 40, "max_games": 40},
                    "royale": {"cand_share": 25.0, "base_share": 38.8, "delta": -13.8, "decision": "PARITY", "games": 40, "max_games": 40},
                    "standard": {"cand_share": 22.5, "base_share": 28.7, "delta": -6.2, "decision": "PARITY", "games": 40, "max_games": 40},
                }
            }
        }
        rendered = render_html_with_state(DASHBOARD_HTML, data)
        # In both matrix and pool tables, Duel 1v1 must be SKIPPED instead of PENDING
        self.assertIn('<span class="status-chip status-chip-skipped matrix-col-status matrix-status-skipped">SKIPPED</span>', rendered)
        self.assertNotIn('<span class="status-chip status-chip-pending matrix-col-status matrix-status-pending">PENDING</span>', rendered)

    def test_stage_durations_frozen_on_finish(self):
        # 1. Self-play matrix run
        self.state.handle_update({
            "action": "init",
            "run_type": "self",
            "experiment_desc": "Test Freeze Stages",
        })
        self.state.handle_update({
            "action": "update_unit_tests",
            "duration_sec": 5.0,
            "status": "PASSED",
        })
        # Finish self-play with 305.0 wall seconds (5.0s unit test -> 300.0s matrix)
        self.state.handle_update({
            "action": "set_finished",
            "stop_type": "MATRIX_COMPLETE",
            "recommendation": "PARITY",
            "duration_sec": 305.0,
        })
        d = self.state.to_dict()
        self.assertEqual(d["matrix_duration_sec"], 300.0)
        self.assertEqual(d["matrix_elapsed_seconds"], 300.0)

        # 2. Transition to Pool within same experiment
        self.state.handle_update({
            "action": "init",
            "run_type": "pool",
            "experiment_desc": "Test Freeze Stages",
        })
        d_pool_init = self.state.to_dict()
        # Self-play duration must stay frozen, not reset to None or 0!
        self.assertEqual(d_pool_init["matrix_duration_sec"], 300.0)

        # Finish pool with 605.0 wall seconds (5.0s unit test -> 600.0s pool)
        self.state.handle_update({
            "action": "set_finished",
            "stop_type": "PARITY",
            "recommendation": "PARITY",
            "duration_sec": 605.0,
        })
        d_pool_finish = self.state.to_dict()
        self.assertEqual(d_pool_finish["matrix_duration_sec"], 300.0)
        self.assertEqual(d_pool_finish["pool_duration_sec"], 600.0)

    def test_standalone_pool_run_skips_self_play(self):
        from ab_dashboard import render_html_with_state, DASHBOARD_HTML
        data = {
            "status": "FINISHED",
            "recommendation": "PARITY",
            "run_type": "pool",
            "experiment_desc": "Standalone Pool Test",
            "unit_tests_data": {"status": "PASSED", "duration_sec": 4.7},
            "matrix_duration_sec": None,
            "pool_duration_sec": 892.0,  # 14m 52s
            "pool_data": {
                "duration_sec": 892.0,
                "modes": {
                    "constrictor": {"cand_share": 25.0, "base_share": 25.0, "decision": "PARITY"},
                    "royale": {"cand_share": 25.0, "base_share": 25.0, "decision": "PARITY"},
                    "standard": {"cand_share": 25.0, "base_share": 25.0, "decision": "PARITY"},
                }
            }
        }
        rendered = render_html_with_state(DASHBOARD_HTML, data)
        # Stage 3 (Self-play) must be SKIPPED with '--', NOT DONE 14m 52s
        self.assertIn('id="railSegment-self"', rendered)
        self.assertIn('<span class="stage-seg-status matrix-status-skipped" id="railStatus-self">SKIPPED</span>', rendered)
        self.assertIn('<span class="stage-seg-duration" id="railDuration-self">--</span>', rendered)
        # Stage 4 (Pool) must be DONE with 14:52
        self.assertIn('id="railSegment-pool"', rendered)
        self.assertIn('<span class="stage-seg-status matrix-status-done" id="railStatus-pool">DONE</span>', rendered)
        self.assertIn('<span class="stage-seg-duration" id="railDuration-pool">14:52</span>', rendered)

    def test_pool_modes_and_opponents_in_set_finished(self):
        self.state.handle_update({
            "action": "init",
            "run_type": "pool",
            "experiment_desc": "Pool Finished Test",
        })
        pool_modes = [
            {"mode": "constrictor", "name": "Constrictor 4P", "games": 40, "max_games": 40, "cand_games": 40, "base_games": 40, "cand_win_share": 75.0, "base_win_share": 75.0, "delta": 0.0, "z_score": 0.0, "decision": "PARITY"},
            {"mode": "royale", "name": "Royale 4P", "games": 40, "max_games": 40, "cand_games": 40, "base_games": 40, "cand_win_share": 60.0, "base_win_share": 60.0, "delta": 0.0, "z_score": 0.0, "decision": "PARITY"},
        ]
        opponents = [
            {"opponent": "snork", "games": 20, "cand_win_pct": 50.0, "base_win_pct": 50.0, "delta": 0.0},
        ]
        self.state.handle_update({
            "action": "set_finished",
            "stop_type": "PARITY",
            "recommendation": "PARITY",
            "duration_sec": 300.0,
            "pool_modes": pool_modes,
            "opponents": opponents,
        })
        d = self.state.to_dict()
        p_data = d["pool_data"]
        self.assertIsNotNone(p_data)
        self.assertEqual(len(p_data.get("opponents", [])), 1)
        self.assertEqual(p_data["opponents"][0]["opponent"], "snork")
        self.assertIn("constrictor", p_data.get("modes", {}))
        self.assertEqual(p_data["modes"]["constrictor"]["games"], 40)

    def test_zero_games_never_renders_parity_or_passed(self):
        from ab_dashboard import render_html_with_state, DASHBOARD_HTML
        data = {
            "status": "FINISHED",
            "recommendation": "PARITY",
            "run_type": "pool",
            "experiment_desc": "Zero Games Test",
            "pool_data": {
                "modes": {
                    "constrictor": {"cand_share": 0.0, "base_share": 0.0, "delta": 0.0, "decision": "PARITY", "games": 0, "max_games": 40},
                }
            }
        }
        rendered = render_html_with_state(DASHBOARD_HTML, data)
        # Constrictor had 0 games completed; must NEVER render PARITY
        self.assertNotIn('<span class="status-chip status-chip-parity matrix-col-status matrix-status-parity">PARITY</span>', rendered)
        self.assertIn('<span class="status-chip status-chip-failed matrix-col-status matrix-status-failed">FAILED</span>', rendered)

    def test_restore_state_preserves_pool_data_and_stage_durations(self):
        saved_state = {
            "status": "FINISHED",
            "stop_type": "PARITY",
            "recommendation": "PARITY",
            "run_type": "pool",
            "experiment_desc": "Restore Pool Test",
            "matrix_duration_sec": 300.0,
            "pool_duration_sec": 600.0,
            "pool_data": [
                {"mode": "constrictor", "name": "Constrictor 4P", "games": 40, "decision": "PARITY"},
                {"mode": "standard", "name": "Standard 4P", "games": 40, "decision": "PARITY"},
            ]
        }
        self.state.handle_update({"action": "restore", "data": saved_state})
        d = self.state.to_dict()
        self.assertEqual(d["matrix_duration_sec"], 300.0)
        self.assertEqual(d["pool_duration_sec"], 600.0)
        p_data = d["pool_data"]
        self.assertIn("constrictor", p_data.get("modes", {}))
        self.assertEqual(p_data["modes"]["constrictor"]["games"], 40)


    def test_cumulative_suite_metrics_when_pool_starts(self):
        """
        Verify that elapsed, left, and progress bar represent the entire A/B suite:
        - When pool games start, elapsed does NOT reset to 0:00 (includes prior self-play + unit + depth).
        - Progress bar does NOT reset to 0% (includes completed self-play games).
        - Total planned games includes both stages.
        """
        from ab_dashboard import calculate_suite_progress

        # 1. Self-play completed with 734 games (e.g. Constrictor early stopped at 134, others 200)
        self_matrix = [
            {"mode": "constrictor", "status": "PASSED", "games": 134, "max_games": 200},
            {"mode": "royale", "status": "PARITY", "games": 200, "max_games": 200},
            {"mode": "duel", "status": "PARITY", "games": 200, "max_games": 200},
            {"mode": "standard", "status": "PARITY", "games": 200, "max_games": 200},
        ]

        # In self-play, planned games should include queued pool gate games (4 modes * 50 = 200)
        comp_s, plan_s, pct_s = calculate_suite_progress(
            run_type="self",
            matrix_progress=self_matrix,
            is_running=False,
        )
        self.assertEqual(comp_s, 734)
        self.assertEqual(plan_s, 934)  # 734 self-play + 200 pool
        self.assertAlmostEqual(pct_s, 78.6, places=1)

        # 2. Transition to Pool
        pool_matrix = [
            {"mode": "constrictor", "status": "running", "games": 0, "max_games": 50},
            {"mode": "royale", "status": "pending", "games": 0, "max_games": 50},
            {"mode": "duel", "status": "pending", "games": 0, "max_games": 50},
            {"mode": "standard", "status": "pending", "games": 0, "max_games": 50},
        ]
        comp_p0, plan_p0, pct_p0 = calculate_suite_progress(
            run_type="pool",
            matrix_progress=pool_matrix,
            self_play_matrix=self_matrix,
            current_mode="constrictor",
            current_played=0,
            is_running=True,
        )
        # At start of pool, progress must NOT be 0%! It must be 78.6%
        self.assertEqual(comp_p0, 734)
        self.assertEqual(plan_p0, 934)
        self.assertAlmostEqual(pct_p0, 78.6, places=1)

        # 3. Halfway through pool (50 constrictor + 25 royale done = 75 pool games)
        comp_p1, plan_p1, pct_p1 = calculate_suite_progress(
            run_type="pool",
            matrix_progress=[
                {"mode": "constrictor", "status": "PASSED", "games": 50, "max_games": 50},
                {"mode": "royale", "status": "running", "games": 0, "max_games": 50},
                {"mode": "duel", "status": "pending", "games": 0, "max_games": 50},
                {"mode": "standard", "status": "pending", "games": 0, "max_games": 50},
            ],
            self_play_matrix=self_matrix,
            current_mode="royale",
            current_played=25,
            is_running=True,
        )
        self.assertEqual(comp_p1, 734 + 75)
        self.assertEqual(plan_p1, 934)
        self.assertAlmostEqual(pct_p1, round((809 / 934) * 100.0, 1), places=1)

        # 4. State tracking & suite elapsed time continuity
        import os
        self.state.unit_tests_data = {"status": "PASSED", "duration_sec": 5.0}
        self.state.depth_data = {"passed": True, "duration_sec": 25.0, "completed_positions": 12}
        self.state.handle_update({
            "action": "init",
            "run_type": "self",
            "experiment_desc": "Full Suite Continuity Test",
            "pid": os.getpid(),
            "matrix_progress": self_matrix,
        })
        self.state.handle_update({
            "action": "set_finished",
            "duration_sec": 330.0,  # 5s unit + 25s depth + 300s matrix
            "stop_type": "MATRIX_COMPLETE",
            "recommendation": "PARITY",
        })
        d_self_done = self.state.to_dict()
        self.assertEqual(d_self_done["matrix_duration_sec"], 300.0)

        # Pool starts on same experiment: suite elapsed must start at >= 330.0s, NOT 0.0s!
        self.state.handle_update({
            "action": "init",
            "run_type": "pool",
            "experiment_desc": "Full Suite Continuity Test",
            "game_mode": "constrictor",
            "pid": os.getpid(),
            "matrix_progress": pool_matrix,
        })
        d_pool_start = self.state.to_dict()
        self.assertGreaterEqual(d_pool_start["suite_elapsed_seconds"], 330.0)
        self.assertEqual(d_pool_start["matrix_duration_sec"], 300.0)
        self.assertAlmostEqual(d_pool_start["games_progress_percent"], 78.6, places=1)
        self.assertAlmostEqual(d_pool_start["progress_percent"], 95.0, places=1)

    def test_target_and_pool_enabled_in_dashboard_state(self):
        """Test target classification and pool_enabled status tracking."""
        from ab_dashboard import calculate_suite_progress

        self.state.handle_update({
            "action": "init",
            "run_type": "self",
            "experiment_desc": "Fix hazard depth [TARGET: BUG FIX]",
            "target": "BUG FIX",
            "pool_enabled": False,
            "pid": os.getpid(),
        })
        d = self.state.to_dict()
        self.assertEqual(d["target"], "BUG FIX")
        self.assertFalse(d["pool_enabled"])

        # When pool_enabled is False, self-play suite progress only plans matrix games
        matrix = [
            {"mode": "royale", "status": "running", "games": 50, "max_games": 200},
        ]
        comp, plan, pct = calculate_suite_progress(
            run_type="self",
            matrix_progress=matrix,
            current_mode="royale",
            current_played=50,
            target_games=200,
            is_running=True,
            pool_enabled=False,
        )
        self.assertEqual(comp, 50)
        self.assertEqual(plan, 200)  # Exactly 200, no phantom 50 pool games
        self.assertEqual(pct, 25.0)


if __name__ == "__main__":
    unittest.main()

