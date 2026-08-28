"""Benchmark: QuadTree (backend/geo.py) vs. real H3 hex-grid indexing
(backend/h3_index.py, via the `h3` PyPI package) on nearest-driver query
time, at several fleet sizes.

Both indices are built from the exact same random driver layout (same
seed) and queried with the exact same set of rider locations, run through
the same `matching.find_nearest_available_driver` expanding-ring search
(1, 2, 4, 8, 16km rings) that the live simulation actually uses -- this
measures the thing that matters end to end ("how long does it take to find
a rider a driver"), not an isolated `query_radius` microbenchmark that the
app never calls in exactly that shape.

A secondary table also times index construction (insert every driver once)
since it's essentially free to capture alongside the query benchmark and
the two backends have very different build costs -- but the task this
script exists for is nearest-driver query time, so that's the headline
number.

Run: .venv/bin/python3 benchmarks/h3_vs_quadtree.py
"""

from __future__ import annotations

import random
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.geo import BoundingBox, QuadTree  # noqa: E402
from backend.h3_index import H3Index  # noqa: E402
from backend.matching import find_nearest_available_driver  # noqa: E402
from backend.models import Driver, DriverStatus  # noqa: E402

BOUNDS = BoundingBox(min_lat=37.760, min_lon=-122.470, max_lat=37.800, max_lon=-122.400)
DRIVER_COUNTS = [40, 500, 2000, 10000, 50000]
NUM_QUERIES = 300
SEED = 11


def make_drivers(num_drivers: int) -> dict[str, Driver]:
    drivers = {}
    for _ in range(num_drivers):
        did = uuid.uuid4().hex[:8]
        drivers[did] = Driver(
            id=did,
            lat=random.uniform(BOUNDS.min_lat, BOUNDS.max_lat),
            lon=random.uniform(BOUNDS.min_lon, BOUNDS.max_lon),
            heading=0,
            speed_kmh=25,
        )
    return drivers


def build_index(index, drivers: dict[str, Driver]) -> float:
    start = time.perf_counter()
    for d in drivers.values():
        index.insert(d.lat, d.lon, d.id)
    return time.perf_counter() - start


def bench_nearest_driver_queries(index, drivers: dict[str, Driver], query_points: list[tuple[float, float]]) -> float:
    """Average time (seconds) of one find_nearest_available_driver() call
    over `query_points`, against an already-built index."""
    start = time.perf_counter()
    for lat, lon in query_points:
        find_nearest_available_driver(index, lat, lon, drivers)
    return (time.perf_counter() - start) / len(query_points)


def main() -> None:
    print(f"scenario: {NUM_QUERIES} nearest-driver queries per fleet size, same random layout for both backends\n")

    build_header = f"{'drivers':>8} | {'quadtree build (ms)':>20} | {'h3 build (ms)':>15}"
    print("=== Index construction (insert every driver once) ===")
    print(build_header)
    print("-" * len(build_header))
    build_rows = []
    for n in DRIVER_COUNTS:
        random.seed(SEED)
        drivers = make_drivers(n)
        qt = QuadTree(BOUNDS)
        qt_build_s = build_index(qt, drivers)
        h3idx = H3Index()
        h3_build_s = build_index(h3idx, drivers)
        build_rows.append((n, qt_build_s, h3_build_s, drivers, qt, h3idx))
        print(f"{n:>8} | {qt_build_s * 1000:>20.3f} | {h3_build_s * 1000:>15.3f}")

    print("\n=== Nearest-driver query time (matching.find_nearest_available_driver) ===")
    query_header = f"{'drivers':>8} | {'quadtree (us/query)':>20} | {'h3 (us/query)':>15} | h3 vs quadtree"
    print(query_header)
    print("-" * len(query_header))
    for n, _, _, drivers, qt, h3idx in build_rows:
        random.seed(SEED + 1)  # different seed than driver placement, same across both backends
        query_points = [
            (random.uniform(BOUNDS.min_lat, BOUNDS.max_lat), random.uniform(BOUNDS.min_lon, BOUNDS.max_lon))
            for _ in range(NUM_QUERIES)
        ]
        qt_us = bench_nearest_driver_queries(qt, drivers, query_points) * 1e6
        h3_us = bench_nearest_driver_queries(h3idx, drivers, query_points) * 1e6
        ratio = h3_us / qt_us if qt_us else float("inf")
        label = f"{ratio:.2f}x slower" if ratio > 1 else f"{1 / ratio:.2f}x faster"
        print(f"{n:>8} | {qt_us:>20.2f} | {h3_us:>15.2f} | h3 is {label}")


if __name__ == "__main__":
    main()
