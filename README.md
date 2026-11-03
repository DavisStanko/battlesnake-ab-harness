# Battlesnake A/B Testing Harness

Statistically rigorous A/B testing harness and in-process simulation engine for Battlesnake AI strategies.

## Overview

- **Integrated 4-Stage Pipeline**: A single `ab_test.py` run executes unit tests, a fixed-position depth benchmark, a self-play matrix, and — for PARITY and IMPROVEMENT targets — the external zoo pool gate, in order, with each stage gating the next.
- **Headless In-Process Simulation**: High-speed parallel game evaluation via multiprocessing without HTTP overhead.
- **Official Rules Support**: Standard, Constrictor, Royale, and Wrapped game modes.
- **Statistical Rigor**: Sequential testing with dynamic continuous early-stop cutoffs ($z = \pm 2.80$) and one-sided $95\%$ significance gates ($z \ge 1.645$) at the game cap.
- **Target-Driven Acceptance**: Every run declares a target (IMPROVEMENT, PARITY, BUG FIX, or HARNESS VERIFICATION) that determines which gates apply and what counts as acceptance. Self-play alone never accepts a change.
- **External Snake Pool Gate**: Common-random-number paired evaluations against external community snake pools (e.g. community snakes via `snake-zoo`).
- **Fixed-Position Depth Tester**: Deterministic snapshot benchmarking across standard turn positions measuring completed depth, nodes searched, and move agreement.
- **Live Web Dashboard & Multi-Mode ETA**: Real-time browser dashboard with persistent per-mode timing history and blended total-run ETA estimation.
- **Results Ledger & Run History**: Persistent CSV ledger of every experiment, plus the JSON run history that powers the dashboard.

## Language Support: Python vs Non-Python

The harness is engineered specifically for **in-process Python execution**. Strategies run directly inside parallel worker processes, avoiding HTTP round-trips, JSON serialization, and socket overhead to evaluate hundreds of turns per second.

- **Python Strategies (First-Class)**: Direct in-process import exposing `choose_move(game_state, wrap, constrictor) -> str` (or standard `move(game_state)`).
- **Non-Python Strategies (Go, TypeScript, Rust, etc.)**: Non-Python engines are supported either via the `zoo_adapter.py` HTTP shim or in external pool mode (`--pool`). While network/socket overhead reduces raw game throughput compared to native C/Python in-process engines, pool mode allows full statistical comparison against any HTTP-speaking Battlesnake.

## Quick Start

`--target` is required on every run.

```bash
# Full pipeline across all active modes (Constrictor 4P, Royale 4P, Duel 1v1, Standard 4P)
python ab_test.py --desc "Full regression suite" --target PARITY --modes all --skip-smoke

# Improvement candidate, Duel only
python ab_test.py --desc "Starter variant improvement test" --target IMPROVEMENT --modes duel --skip-smoke
```

`--skip-smoke` skips the pre-flight binary smoke test.

## The Pipeline

**Stage 1 — Unit Tests.** The full strategy test suite (`tests/test_strategy.py`) must pass. Any failure stops the run.

**Stage 2 — Depth Benchmark.** Fixed-position fixtures (turns 25, 50, 75 across all four modes) searched under identical time budgets, interleaved, reported as medians. For parity-class targets (PARITY, BUG FIX, HARNESS VERIFICATION) this stage is a strict non-regression gate: variant median depth must not drop below baseline in any mode, per-mode NPS ratio must be $\ge 0.90\times$, and the overall NPS ratio must be $\ge 0.95\times$. For IMPROVEMENT targets the stage is informational only — the matrix decides.

**Stage 3 — Self-Play Matrix.** Variant against baseline clones, up to 200 games per mode ($p_0 = 25\%$ win share in 4P, $50\%$ in Duel). Outcomes are per mode: KEEP, PARITY, or REVERT. Sequential evaluation starts at 30 games and runs after every game: dynamic early-stop thresholds correspond to $z = \pm 2.80$ (nominal anchors $60\%/40\%$ in 1v1, $35\%/18\%$ in 4P). At the 200-game cap, KEEP requires win share at or above the keep threshold with $z \ge 1.645$ (in 4P the variant must also finish as the top snake), $z \le -1.645$ is REVERT, and anything between is PARITY. A REVERT in any mode rejects the run outright. KEEP or PARITY only qualifies the run for Stage 4 — it never accepts a change by itself.

**Stage 4 — Zoo Pool Gate.** Candidate and baseline are each evaluated against the external pool (below). Runs automatically for PARITY and IMPROVEMENT targets after a qualifying matrix.

## Targets & Acceptance

| Target | Acceptance requires |
| :--- | :--- |
| **IMPROVEMENT** | Matrix KEEP or PARITY, then pool $\Delta > 0$ with $z \ge 1.645$, plus fresh-seed replication of the pool result. |
| **PARITY** | Matrix KEEP or PARITY, then pool $z > -1.645$ (an improvement-level pool result also satisfies PARITY), plus fresh-seed replication. |
| **BUG FIX** | Pool exemption (Rule 3): a fixture test that fails before the fix — reproducing the fatal illegal move or crash — and passes after it, covered by a unit test in `tests/test_strategy.py`, plus self-play non-regression (KEEP or PARITY). |
| **HARNESS VERIFICATION** | Verifies harness behaviour itself; no strategy acceptance. |

Every experiment is recorded in `results_ledger.csv`, whatever the outcome.

## Pool Mode & Snake Zoo Adapter

The harness can evaluate strategies against a pool of external community snakes (such as those hosted locally via Battlesnake's official [snake-zoo](https://github.com/BattlesnakeOfficial/snake-zoo)):

1. Launch your opponent containers locally using `snake-zoo`:
   ```bash
   snake-zoo run nessegrev-expert devious-devin hovering-hobbs improbable-irene snork amphibious-arthur constant-carter
   ```

2. Create a pool configuration file (see `pool.example.json`; the pipeline's default is `pool_snakes.json` in this directory):
   ```json
   [
     { "slug": "nessegrev-expert", "url": "http://localhost:8001" },
     { "slug": "devious-devin", "url": "http://localhost:8002/devious-devin" }
   ]
   ```

3. The pool gate runs as Stage 4 of the pipeline, using `pool_snakes.json` by default. Pass `--pool` to use a different pool configuration:
   ```bash
   python ab_test.py --pool pool.json --pool-games 50 --modes duel,standard --target PARITY --desc "Candidate vs Zoo Pool"
   ```

Pool mode runs two evaluations using an identical seed stream (Common Random Numbers): candidate vs pool and baseline vs pool. It reports win share per mode, the paired difference ($\Delta$), and the statistical $z$-score of the difference.

## Fixed-Position Depth Tester

To isolate algorithmic and move-ordering performance from game variance, the harness includes a fixed-position depth tester (`depth_test.py`), which also runs as Stage 2 of the pipeline:

1. **Fixture Generation**:
   Snapshots full game states at turns 25, 50, and 75 across all four game modes into `positions/`:
   ```bash
   python depth_test.py --generate
   ```

2. **Benchmark Execution**:
   Runs interleaved searches under identical time budgets and clean memory contexts:
   ```bash
   python depth_test.py --baseline baseline --variant variant --repeats 5
   ```

3. **Output**:
   Per-mode tables and an aggregate summary showing median depth, nodes/sec (NPS) ratio, and move agreement %. For parity-class targets the strict non-regression gate above applies; for IMPROVEMENT targets the results are diagnostic.

## Web Dashboard & Blended Multi-Mode ETA

The live dashboard (`ab_dashboard.py`) runs at `http://localhost:8888`:

```bash
python ab_dashboard.py --port 8888
```

- **4-Stage Pipeline Rail**: Live state of Unit Tests → Depth Benchmark → Self-Play Matrix → Zoo Pool Gate, with the run's target and overall status.
- **Stage Cards**: Per-stage detail — test results, depth and NPS by mode, per-mode matrix results, and pool results by mode and opponent.
- **Run History**: One row per recorded run in a 10-column table (Date, Experiment, Tests, Depth, Src, Constrictor, Royale, Duel, Standard, Verdict). Each mode cell stacks the self-play result above the pool result when a run has both — the two are different measurements and are never collapsed into one — with the source labels stated once per row in the Src column. Verdict shows the run's recorded recommendation.
- **Persistent Timing History**: Completed mode durations are appended to `mode_timings.json`.
- **Total Run ETA**: Blends the active mode's live pace (once $\ge 20$ games have finished) with historical medians for upcoming modes to provide an accurate total job completion estimate.

## Results Ledger & Run History

Every run appends one summary row to `results_ledger.csv`:
```csv
date,description,self-play delta if run,pool delta per mode,decision
2026-10-20 18:41:34,Pool benchmark evaluation,,duel: -20.0% (z=-2.18),REVERT
```

Full run records are stored as JSON in `ab_results/ab_history.json`; the dashboard reads them to restore live state and to render Run History.

## Strategy Interface

Custom strategies implement:
- `INFO: dict` — Snake metadata (`apiversion`, `author`, `color`, `head`, `tail`)
- `choose_move(game_state: dict, wrap: bool, constrictor: bool) -> str` — Returns `"up"`, `"down"`, `"left"`, or `"right"`. (Alternatively, standard Battlesnake `move(game_state: dict)` returning `{"move": "..."}`).

See `example_snakes/baseline.py` and `example_snakes/variant.py` for reference implementations.

## License

GNU General Public License v3. See [LICENSE](LICENSE) for details.
