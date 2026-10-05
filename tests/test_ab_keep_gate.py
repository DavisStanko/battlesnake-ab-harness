import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from ab_test import classify_max_games_outcome, compute_z_score, Z_CRIT_KEEP, Z_CRIT_REGRESSION


class TestABKeepGate(unittest.TestCase):
    """
    Unit tests for classify_max_games_outcome and the statistical significance gate (z >= 1.645).
    """

    def test_duel_51_5_percent_bug_case(self):
        """
        duel, 103/200 (51.5%, z≈0.42) -> PARITY, not KEEP.
        This verifies that meeting the win share threshold alone without statistical significance
        is no longer falsely classified as KEEP.
        """
        total_played = 200
        wins = 103
        var_win_share = wins / total_played * 100.0  # 51.5%
        keep_threshold = 51.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=2)
        self.assertAlmostEqual(z, 0.424, places=2)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=2,
            se=se,
        )
        self.assertEqual(decision, "PARITY")
        self.assertTrue(passed)
        self.assertIn("parity confidence band", reason)

    def test_duel_56_percent_keep(self):
        """
        duel, 112/200 (56.0%, z≈1.70) -> KEEP.
        Confirms significant improvement (z >= 1.645) is classified as KEEP.
        """
        total_played = 200
        wins = 112
        var_win_share = wins / total_played * 100.0  # 56.0%
        keep_threshold = 51.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=2)
        self.assertAlmostEqual(z, 1.70, places=2)
        self.assertGreaterEqual(z, Z_CRIT_KEEP)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=2,
            se=se,
        )
        self.assertEqual(decision, "KEEP")
        self.assertTrue(passed)
        self.assertIn("Confident improvement", reason)

    def test_duel_42_5_percent_revert(self):
        """
        duel, 85/200 (42.5%, z≈-2.12) -> REVERT.
        Confirms significant regression (z <= -1.645) is classified as REVERT.
        """
        total_played = 200
        wins = 85
        var_win_share = wins / total_played * 100.0  # 42.5%
        keep_threshold = 51.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=2)
        self.assertAlmostEqual(z, -2.12, places=2)
        self.assertLessEqual(z, -Z_CRIT_REGRESSION)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=2,
            se=se,
        )
        self.assertEqual(decision, "REVERT")
        self.assertFalse(passed)
        self.assertIn("Confident regression", reason)

    def test_4p_variant_26_percent_below_best_baseline(self):
        """
        4P, variant 26% but below best baseline -> not KEEP (PARITY).
        """
        total_played = 200
        var_win_share = 26.0
        max_opp_win_share = 28.0
        keep_threshold = 25.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=4)
        # z for 26% vs 25% at N=200 is ~0.33
        self.assertAlmostEqual(z, 0.327, places=2)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=4,
            max_opp_win_share=max_opp_win_share,
            se=se,
        )
        self.assertEqual(decision, "PARITY")
        self.assertTrue(passed)

    def test_4p_variant_31_percent_below_best_baseline(self):
        """
        4P, variant 31% (z=1.96 >= 1.645) but below best baseline (e.g. 33%) -> not KEEP (PARITY).
        Confirms that in multiplayer, variant must also be the top snake to KEEP.
        """
        total_played = 200
        var_win_share = 31.0
        max_opp_win_share = 33.0
        keep_threshold = 25.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=4)
        self.assertGreaterEqual(z, Z_CRIT_KEEP)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=4,
            max_opp_win_share=max_opp_win_share,
            se=se,
        )
        self.assertEqual(decision, "PARITY")
        self.assertTrue(passed)

    def test_4p_variant_31_percent_top_share_keep(self):
        """
        4P, variant 31% and top share -> KEEP.
        At N=200, 31% has z ≈ 1.96 >= 1.645, exceeds keep_threshold (25.5%), and is top snake.
        """
        total_played = 200
        var_win_share = 31.0
        max_opp_win_share = 25.0
        keep_threshold = 25.5
        z, se = compute_z_score(var_win_share, total_played, num_snakes=4)
        self.assertAlmostEqual(z, 1.96, places=2)
        self.assertGreaterEqual(z, Z_CRIT_KEEP)

        decision, passed, reason = classify_max_games_outcome(
            var_win_share=var_win_share,
            z_final=z,
            keep_threshold=keep_threshold,
            num_snakes=4,
            max_opp_win_share=max_opp_win_share,
            se=se,
        )
        self.assertEqual(decision, "KEEP")
        self.assertTrue(passed)
        self.assertIn("Confident improvement", reason)

    def test_exact_z_crit_keep_boundary(self):
        """
        Tests the exact boundary around Z_CRIT_KEEP (1.645).
        """
        keep_threshold = 50.0
        # Just below 1.645 -> PARITY
        dec_below, passed_below, _ = classify_max_games_outcome(
            var_win_share=56.0,
            z_final=1.644,
            keep_threshold=keep_threshold,
            num_snakes=2,
        )
        self.assertEqual(dec_below, "PARITY")
        self.assertTrue(passed_below)

        # Exactly 1.645 -> KEEP
        dec_exact, passed_exact, _ = classify_max_games_outcome(
            var_win_share=56.0,
            z_final=1.645,
            keep_threshold=keep_threshold,
            num_snakes=2,
        )
        self.assertEqual(dec_exact, "KEEP")
        self.assertTrue(passed_exact)

    def test_exact_z_crit_regression_boundary(self):
        """
        Tests the exact boundary around -Z_CRIT_REGRESSION (-1.645).
        """
        keep_threshold = 50.0
        # Exactly -1.645 -> REVERT
        dec_exact, passed_exact, _ = classify_max_games_outcome(
            var_win_share=44.0,
            z_final=-1.645,
            keep_threshold=keep_threshold,
            num_snakes=2,
        )
        self.assertEqual(dec_exact, "REVERT")
        self.assertFalse(passed_exact)

        # Just above -1.645 -> PARITY
        dec_above, passed_above, _ = classify_max_games_outcome(
            var_win_share=44.0,
            z_final=-1.644,
            keep_threshold=keep_threshold,
            num_snakes=2,
        )
        self.assertEqual(dec_above, "PARITY")
        self.assertTrue(passed_above)


if __name__ == "__main__":
    unittest.main()
