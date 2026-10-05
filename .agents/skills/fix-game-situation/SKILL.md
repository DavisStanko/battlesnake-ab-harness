---
name: fix-game-situation
description: Use this skill when investigating a Battlesnake game URL or specific blunder, reproducing the turn state in a deterministic unit test, and developing a targeted strategy fix.
---

# Battlesnake Game Situation Bug Fix Skill

Follow this workflow whenever a specific game URL, turn state, or tactical blunder is reported. Reproduce the blunder deterministically in a unit test, verify the baseline failure, develop a fix in the variant, and validate via A/B testing before proposing a PR.

## Workflow Steps

### 1. Recreate Deterministic Unit Test

- **Fetch Turn State**: Extract game state from the Battlesnake URL (e.g., query `https://engine.battlesnake.com/games/<game_id>` or construct from user details).
- **Add Deterministic Test Case**: Add a new test method in `tests/test_strategy.py` setting up the exact board state and turn. The test must assert the single correct, optimal move required in that situation.
- **Confirm Baseline Failure**: Run `python -m unittest tests/test_strategy.py -v` and verify that `strategies/strategy_baseline.py` fails the new test case (reproducing the exact blunder observed in the game).

### 2. Implement Variant Fix

- **Analyze Root Cause**: Determine why the baseline made the wrong move (e.g., over-weighted kill/choke desire, flood-fill space miscalculation, hazard evaluation order).
- **Modify Variant Only**: Implement a targeted correction in `strategies/strategy_variant.py`. **NEVER** modify `strategies/strategy_baseline.py` during testing.
- **Contract & Latency Compliance**: Ensure responses remain valid JSON and evaluate under the 500ms Battlesnake turn timeout.

### 3. Verify Unit Test Suite

- Run `python -m unittest tests/test_strategy.py -v`.
- Confirm that `strategies/strategy_variant.py` passes the new test case AND all existing regression tests (100% pass rate).

### 4. Execute A/B Test

- Run `harness/ab_test.py` passing `--desc` (include the word "Fix" in `--desc` to activate bug fix threshold):
  ```bash
  python harness/ab_test.py --desc "Fix turn X blunder: [brief explanation of fix]"
  ```
- Keep the browser dashboard at `http://localhost:8888` active.
- `harness/ab_test.py` automatically validates unit tests and runs the official Battlesnake CLI smoke test before simulating parallel games.

### 5. Keep / Revert Decision Policy

Tactical fixes target specific, potentially rare board positions:
- **KEEP Policy**: As long as **all unit tests pass** AND the variant achieves a **win share ≥ 50.0%** (parity or better against baseline across simulated games), KEEP the fix.
- **REVERT Policy**: Revert if the variant causes a net drop in overall win rate (Variant Win Share < 50.0%) or fails unit tests (`TEST_FAILURE`).

### 6. Action on Outcome

- **On KEEP**:
  1. Merge the verified logic from `strategies/strategy_variant.py` into `strategies/strategy_baseline.py`.
  2. Re-run `python -m unittest tests/test_strategy.py -v` to ensure `strategy_baseline.py` passes all unit tests including the new test.
  3. Create a feature branch off `main`:
     ```bash
     git checkout -b fix/<descriptive-name> main
     ```
  4. Commit the changes with a clear commit message referencing the blunder, test name, and win rate:
     ```bash
     git commit -m "fix(strategy): fix turn X blunder - space-gated choke pursuit (50.5% WR, N=200)"
     ```
  5. Push the feature branch and open a Pull Request targeting `main`:
     ```bash
     git push -u origin fix/<descriptive-name>
     gh pr create --base main --title "fix(strategy): [brief description]" --body "[details, test case, win rate]"
     ```
  6. **HALT IMMEDIATELY**:
     - Provide the PR link to the user.
     - **DO NOT MERGE THE PR**. Agents are strictly forbidden from running `gh pr merge` or `git merge`.
     - Wait for human review and merge before proceeding.
- **On REVERT**:
  1. Undo changes in `strategies/strategy_variant.py` so it matches `strategies/strategy_baseline.py`.
  2. Document the outcome in `docs/REJECTED_EXPERIMENTS.md` with: experiment name, date, history file, game count, win rate, original game link, hypothesis, and takeaway.
  3. Do NOT commit. Do NOT push.
