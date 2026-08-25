"""Benchmark: full QuadTree rebuild vs. incremental cell updates.

Two different comparisons, because they answer two different questions:

1. **Per-tick, whole-fleet update** (`bench_per_tick`). This repo's demo has
   every driver report a new position every single tick (continuous
   simulated movement, for a lively map) — so a once-per-tick rebuild
   already batches all n drivers' changes into one pass. Doing n individual
   incremental remove+insert calls instead lands in the same O(n log n)
   ballpark, with `QuadTree.remove()`'s list-scan adding real overhead on
   top. At this access pattern the two strategies are close, and the plain
   rebuild can even edge ahead — that's a genuine, useful finding, not a
   reason to skip measuring it.

2. **Per-update, single driver ping** (`bench_single_update`). The pattern
   production location-ingestion actually sees: pings arrive continuously,
   one driver at a time, not conveniently batched into one rebuild covering
   the whole fleet every second. This measures the cost of applying exactly
   one driver's new position to an *already-built* index of n drivers.
   Rebuilding to reflect that one ping means reinserting all n drivers —
   O(n) — while incremental touches only that driver's old and new cell —
   O(log n). This is where "O(1) amortized per driver" / "wouldn't keep up
   at fleet scale" actually shows up, and it's the reason real geo-indexes
   (H3-backed or otherwise) update incrementally rather than rebuild.

`Simulation` keeps both paths available (`SimulationConfig.incremental_index`)
specifically so both comparisons are apples to apples — same movement code,
same random seed, only the index-maintenance strategy differs.

Run: .venv/bin/python3 benchmarks/index_rebuild_vs_incremental.py
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.simulation import Simulation, SimulationConfig  # noqa: E402

TICKS = 200
DRIVER_COUNTS = [40, 500, 1000, 2000]
SINGLE_UPDATE_TRIALS = 500
SEED = 42


def bench_per_tick(num_drivers: int, incremental: bool) -> float:
    random.seed(SEED)
    sim = Simulation(SimulationConfig(num_drivers=num_drivers, incremental_index=incremental))
    start = time.perf_counter()
    for _ in range(TICKS):
        sim.tick()
    return time.perf_counter() - start


def bench_single_update(num_drivers: int, incremental: bool) -> float:
    """Time applying ONE driver's position change to an already-built index
    of `num_drivers` drivers, averaged over SINGLE_UPDATE_TRIALS pings."""
    random.seed(SEED)
    sim = Simulation(SimulationConfig(num_drivers=num_drivers, incremental_index=True))
    driver = next(iter(sim.drivers.values()))

    total = 0.0
    lat, lon = driver.lat, driver.lon
    for i in range(SINGLE_UPDATE_TRIALS):
        new_lat, new_lon = lat + (0.0002 if i % 2 == 0 else -0.0002), lon
        if incremental:
            start = time.perf_counter()
            sim.index.remove(lat, lon, driver.id)
            sim.index.insert(new_lat, new_lon, driver.id)
            total += time.perf_counter() - start
        else:
            driver.lat, driver.lon = new_lat, new_lon
            start = time.perf_counter()
            sim._rebuild_index()
            total += time.perf_counter() - start
        lat, lon = new_lat, new_lon
    return total / SINGLE_UPDATE_TRIALS


def main() -> None:
    print("=== 1. Per-tick, whole fleet moves every tick ===")
    print(f"{TICKS} ticks per run, same seed for each variant\n")
    header = f"{'drivers':>8} | {'full rebuild (ms/tick)':>24} | {'incremental (ms/tick)':>22} | ratio"
    print(header)
    print("-" * len(header))
    for n in DRIVER_COUNTS:
        rebuild_ms = (bench_per_tick(n, incremental=False) / TICKS) * 1000
        incr_ms = (bench_per_tick(n, incremental=True) / TICKS) * 1000
        ratio = rebuild_ms / incr_ms if incr_ms > 0 else float("inf")
        print(f"{n:>8} | {rebuild_ms:>24.3f} | {incr_ms:>22.3f} | {ratio:>5.2f}x")

    print("\n=== 2. Per-update, cost of ONE driver ping into an existing index ===")
    print(f"average of {SINGLE_UPDATE_TRIALS} single-driver updates per fleet size\n")
    header = f"{'drivers':>8} | {'full rebuild (ms/ping)':>24} | {'incremental (ms/ping)':>22} | speedup"
    print(header)
    print("-" * len(header))
    for n in DRIVER_COUNTS:
        rebuild_ms = bench_single_update(n, incremental=False) * 1000
        incr_ms = bench_single_update(n, incremental=True) * 1000
        speedup = rebuild_ms / incr_ms if incr_ms > 0 else float("inf")
        print(f"{n:>8} | {rebuild_ms:>24.4f} | {incr_ms:>22.4f} | {speedup:>6.1f}x")


if __name__ == "__main__":
    main()
