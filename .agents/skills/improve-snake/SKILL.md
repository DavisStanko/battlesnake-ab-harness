---
name: improve-snake
description: Use this skill when the user asks to improve the Battlesnake strategy, optimize heuristics or search, run A/B benchmark tests, or execute items from TODO.md.
---

# Battlesnake Strategy Optimization Skill

Follow this iterative workflow to develop, benchmark, and propose strategy improvements.

## Workflow Rules & Guidelines

1. **Active Task Source & Loop Coordination**:
   - Check `TODO.md` to identify the highest-priority pending improvement.
   - Refer to `TODO.md` for the full autonomous agentic loop state machine.
   - Check `docs/REJECTED_EXPERIMENTS.md` to avoid repeating previously failed hypotheses.

2. **Isolated Variant Development & Stacked Branching**:
   - Identify the current stack tip `<parent-branch>` (`main` or the previously opened feature/reject branch).
   - Create a feature branch stacked directly off `<parent-branch>`:
     `git checkout -b feat/<descriptive-name> <parent-branch>`
   - Make all algorithmic modifications exclusively in the variant strategy (e.g. `strategies/strategy_variant.py`).
   - **NEVER** modify the baseline strategy during testing. It serves as the fixed control.

3. **A/B Benchmark Testing**:
   - Execute `harness/ab_test.py` passing `--desc` describing the change:
     `python harness/ab_test.py --desc "Description of improvement"`
   - If the modification is strictly mode-gated (e.g. `hazard_dmg > 0` for Royale), pass `--modes <mode>` (e.g. `--modes royale`) to test only the relevant mode. Otherwise pass `--modes all`.
   - Keep the browser dashboard at `http://localhost:8888` active.
   - The harness automatically runs pre-flight tactical unit tests (`tests/test_strategy.py`) and Battlesnake CLI smoke tests before simulating games across target modes.

4. **Decision Criteria & Target Accounting**:
   Evaluate overall outcome against the task's annotated target in `TODO.md`:
   - **`[TARGET: IMPROVEMENT]`**:
     - **KEEP** -> **ACCEPT** (Confident win share improvement).
     - **PARITY** -> **REJECT** (Failed improvement hypothesis; revert to prevent code bloat unless depth/speedup justifies).
     - **REVERT** -> **REJECT** (Confirmed regression).
   - **`[TARGET: PARITY]`**:
     - **KEEP or PARITY** -> **ACCEPT** (Confirmed zero regression, $z > -1.645$; preserves gameplay invariance while completing refactor/fast-exit).
     - **REVERT** -> **REJECT** (Confirmed regression).
   - **`[TARGET: PARITY / IMPROVEMENT]`**:
     - **KEEP** -> **ACCEPT**.
     - **PARITY with Depth Gain** -> **ACCEPT** (Average search depth increased with no win share regression).
     - **PARITY without Depth Gain** -> **REJECT**.
     - **REVERT** -> **REJECT**.
   - **`[TARGET: HARNESS VERIFICATION]`**:
     - Pre-flight unit tests pass, zero simulation divergence against Python rules, benchmark proves wall-clock speedup, and A/B test confirms PARITY -> **ACCEPT**.

5. **Action on Outcome & Stacked PRs**:
   - **On ACCEPT**:
     1. Promote variant to baseline:
        - Sync verified logic from the variant strategy into the baseline strategy (and remove any temporary variant files).
     2. Remove the completed item from `TODO.md`.
     3. Commit changes to the feature branch:
        `git commit -am "feat(strategy): <description>"`
     4. Push the branch and open a Pull Request targeting main:
        `git push -u origin feat/<descriptive-name>`
        `gh pr create --base main --head feat/<descriptive-name> --title "feat(strategy): <description>" --body "<summary>"`
     5. **Output PR & Continue**:
        - Provide the PR link to the user.
        - **DO NOT MERGE THE PR**. Agents are strictly forbidden from running `gh pr merge`. Merging is reserved for the human user.
        - Set `<parent-branch> = feat/<descriptive-name>` and proceed to the next item in the loop.
   - **On REVERT (or Rejected PARITY)**:
     1. Revert the variant strategy back to match baseline (and remove any temporary variant files).
     2. Log the experiment in `docs/REJECTED_EXPERIMENTS.md` with details on hypothesis, game count, win rates, and takeaway.
     3. Remove the failed item from `TODO.md`.
     4. Create documentation branch, commit, push, and open PR targeting main:
        `git checkout -b docs/reject-<descriptive-name> <parent-branch>`
        `git commit -am "docs: log rejected experiment <N> (<name>) and update TODO"`
        `git push -u origin docs/reject-<descriptive-name>`
        `gh pr create --base main --head docs/reject-<descriptive-name> --title "docs: log rejected experiment <N> (<name>)" --body "<summary>"`
     5. Provide the PR link to the user.
     6. **DO NOT MERGE THE PR**.
     7. Set `<parent-branch> = docs/reject-<descriptive-name>` and proceed to the next item in the loop.
