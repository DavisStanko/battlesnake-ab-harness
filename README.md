# Battlesnake A/B Testing Harness

Statistically rigorous A/B testing harness and in-process simulation engine for Battlesnake AI strategies.

## Overview

- **Headless In-Process Simulation**: High-speed parallel game evaluation via multiprocessing without HTTP overhead.
- **Official Rules Support**: Standard, Constrictor, Royale, and Wrapped game modes.
- **Statistical Rigor**: Sequential testing with dynamic continuous early-stop cutoffs and one-sided $95\%$ significance gates ($z \ge 1.645$).
- **Live Web Dashboard**: Real-time WebSocket match visualizer and progress monitor.

## Language Support: Python vs Non-Python

The harness is engineered specifically for **in-process Python execution**. Strategies run directly inside parallel worker processes, avoiding HTTP round-trips, JSON serialization, and socket overhead to evaluate hundreds of turns per second.

- **Python Strategies (First-Class)**: Direct in-process import exposing `choose_move(game_state, wrap, constrictor) -> str` (or standard `move(game_state)`).
- **Non-Python Strategies (Go, TypeScript, Rust, etc.)**: Non-Python engines are not well served. Benchmarking an external engine requires writing a Python shim that invokes a subprocess or sends an HTTP POST per turn. This introduces IPC/socket overhead that eliminates the speed advantage the harness exists to provide.

## Quick Start

```bash
# Benchmark starter variant against baseline snake (Duel 1v1)
python ab_test.py --desc "Starter variant baseline test" --modes duel --skip-smoke

# Benchmark across all active modes (Constrictor 4P, Royale 4P, Duel 1v1, Standard 4P)
python ab_test.py --desc "Full regression suite" --modes all --skip-smoke
```

## Strategy Interface

Custom strategies implement:
- `INFO: dict` — Snake metadata (`apiversion`, `author`, `color`, `head`, `tail`)
- `choose_move(game_state: dict, wrap: bool, constrictor: bool) -> str` — Returns `"up"`, `"down"`, `"left"`, or `"right"`. (Alternatively, standard Battlesnake `move(game_state: dict)` returning `{"move": "..."}`).

See `example_snakes/baseline.py` and `example_snakes/variant.py` for reference implementations.

## License

MIT License. See [LICENSE](LICENSE) for details.
