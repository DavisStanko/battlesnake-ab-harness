# Battlesnake A/B Testing Harness

Statistically rigorous A/B testing harness and in-process simulation engine for Battlesnake AI strategies.

## Overview

- **Headless In-Process Simulation**: High-speed parallel game evaluation via multiprocessing without HTTP overhead.
- **Official Rules Support**: Standard, Constrictor, Royale, and Wrapped game modes.
- **Statistical Rigor**: Sequential testing with dynamic continuous early-stop cutoffs and one-sided $95\%$ significance gates ($z \ge 1.645$).
- **External Snake Pool Mode**: Common-random-number paired evaluations against external community snake pools (e.g. community snakes via `snake-zoo`).
- **Fixed-Position Depth Tester**: Deterministic snapshot benchmarking across standard turn positions measuring completed depth, nodes searched, and move agreement.
- **Live Web Dashboard & Multi-Mode ETA**: Real-time browser dashboard with persistent per-mode timing history and blended total-run ETA estimation.
- **Results Ledger**: Persistent CSV ledger tracking self-play and pool deltas, significance scores, and decisions across experiments.

## Language Support: Python vs Non-Python

The harness is engineered specifically for **in-process Python execution**. Strategies run directly inside parallel worker processes, avoiding HTTP round-trips, JSON serialization, and socket overhead to evaluate hundreds of turns per second.

- **Python Strategies (First-Class)**: Direct in-process import exposing `choose_move(game_state, wrap, constrictor) -> str` (or standard `move(game_state)`).
- **Non-Python Strategies (Go, TypeScript, Rust, etc.)**: Non-Python engines are supported either via the `zoo_adapter.py` HTTP shim or in external pool mode (`--pool`). While network/socket overhead reduces raw game throughput compared to native C/Python in-process engines, pool mode allows full statistical comparison against any HTTP-speaking Battlesnake.

## Quick Start

```bash
# Benchmark starter variant against baseline snake (Duel 1v1)
python ab_test.py --desc "Starter variant baseline test" --modes duel --skip-smoke

# Benchmark across all active modes (Constrictor 4P, Royale 4P, Duel 1v1, Standard 4P)
python ab_test.py --desc "Full regression suite" --modes all --skip-smoke
```

## Pool Mode & Snake Zoo Adapter

The harness can evaluate strategies against a pool of external community snakes (such as those hosted locally via Battlesnake's official [snake-zoo](https://github.com/BattlesnakeOfficial/snake-zoo)):

1. Launch your opponent containers locally using `snake-zoo`:
   ```bash
   snake-zoo run nessegrev-expert devious-devin hovering-hobbs improbable-irene snork amphibious-arthur constant-carter
   ```

2. Create a pool configuration file (see `pool.example.json`):
   ```json
   [
     { "slug": "nessegrev-expert", "url": "http://localhost:8001" },
     { "slug": "devious-devin", "url": "http://localhost:8002/devious-devin" }
   ]
   ```

3. Run paired pool evaluation:
   ```bash
   python ab_test.py --pool pool.json --pool-games 50 --modes duel,standard --desc "Candidate vs Zoo Pool"
   ```

Pool mode runs two evaluations using an identical seed stream (Common Random Numbers): candidate vs pool and baseline vs pool. It reports win share per mode, the paired difference ($\Delta$), and the statistical $z$-score of the difference.

## Fixed-Position Depth Tester

To isolate algorithmic and move-ordering performance from game variance, the harness includes a fixed-position depth tester (`depth_test.py`):

1. **Fixture Generation**:
   Snapshots full game states at turns 25, 50, and 75 across all four game modes into `positions/`:
   ```bash
   python depth_test.py --generate
   ```

2. **Benchmark Execution**:
   Runs interleaved searches under identical time budgets and clean memory contexts:
   ```bash
   python depth_test.py --baseline baseline --variant variant --time-ms 450 --repeats 5
   ```

3. **Output**:
   Outputs per-mode tables and an aggregate summary showing median depth, nodes/sec (NPS) ratio, and move agreement %, flagging any mode where variant median depth drops below baseline.

## Web Dashboard & Blended Multi-Mode ETA

The live dashboard (`ab_dashboard.py`) runs at `http://localhost:8888`:

```bash
python ab_dashboard.py --port 8888
```

- **Tug-of-War Win Rate Bar**: Live visualizer of variant vs baseline points.
- **Multi-Mode Regression Matrix**: Live status for each mode in the regression suite (Constrictor, Royale, Duel, Standard).
- **Persistent Timing History**: Completed mode durations are appended to `mode_timings.json`.
- **Total Run ETA**: Blends the active mode's live pace (once $\ge 20$ games have finished) with historical medians for upcoming modes to provide an accurate total job completion estimate.

## Results Ledger

All benchmark runs append one summary row to `results_ledger.csv`:
```csv
date,description,self-play delta if run,pool delta per mode,decision
2026-10-20 18:41:34,Pool benchmark evaluation,,duel: -20.0% (z=-2.18),REVERT
```

## Strategy Interface

Custom strategies implement:
- `INFO: dict` — Snake metadata (`apiversion`, `author`, `color`, `head`, `tail`)
- `choose_move(game_state: dict, wrap: bool, constrictor: bool) -> str` — Returns `"up"`, `"down"`, `"left"`, or `"right"`. (Alternatively, standard Battlesnake `move(game_state: dict)` returning `{"move": "..."}`).

See `example_snakes/baseline.py` and `example_snakes/variant.py` for reference implementations.

## License

GNU General Public License v3. See [LICENSE](LICENSE) for details.
