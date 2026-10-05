# Battlesnake A/B Testing Harness

Statistically rigorous A/B testing harness and in-process simulation engine for Battlesnake AI strategies.

## Overview

- **Headless In-Process Simulation**: High-speed parallel game evaluation via multiprocessing without HTTP overhead.
- **Official Rules Support**: Standard, Constrictor, Royale, and Wrapped game modes.
- **Statistical Rigor**: Sequential testing with dynamic continuous early-stop cutoffs and one-sided $95\%$ significance gates ($z \ge 1.645$).
- **Live Web Dashboard**: Real-time WebSocket match visualizer and progress monitor.

## Quick Start

```bash
# Benchmark starter variant against baseline survival snake (Duel 1v1)
python ab_test.py --desc "Starter variant baseline test" --modes duel --skip-smoke

# Benchmark across all active modes (Constrictor 4P, Royale 4P, Duel 1v1, Standard 4P)
python ab_test.py --desc "Full regression suite" --modes all --skip-smoke
```

## Strategy Interface

Custom strategies implement:
- `INFO: dict` — Snake metadata (`apiversion`, `author`, `color`, `head`, `tail`)
- `choose_move(game_state: dict, wrap: bool, constrictor: bool) -> str` — Returns `"up"`, `"down"`, `"left"`, or `"right"`.

See `example_snakes/survival.py` and `example_snakes/variant_template.py` for reference implementations.

## License

MIT License. See [LICENSE](LICENSE) for details.
