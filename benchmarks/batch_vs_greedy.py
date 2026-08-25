"""Benchmark: greedy nearest-driver matching vs. batch Hungarian assignment.

Simulates one "surge moment": several ride requests landing at once while a
fixed set of drivers is available. Greedy matching (matching.py:
find_nearest_available_driver) handles them one at a time in arrival order —
each rider takes whichever driver is closest and still free by the time
their request is processed. Batch matching (matching.py:
find_batch_assignment) looks at the whole group together and solves it as a
linear assignment problem, minimizing *total* pickup distance across all
riders in the batch rather than each rider's distance in isolation.

Both strategies run against the exact same randomly generated
driver/rider layout each trial, so the only variable is the matching
algorithm. The gap in average pickup distance is a reasonable stand-in for
rider wait time, since ETA here is distance / average speed (see
matching.eta_minutes).

Run: .venv/bin/python3 benchmarks/batch_vs_greedy.py
"""

from __future__ import annotations

import random
import sys
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.geo import BoundingBox, QuadTree  # noqa: E402
from backend.matching import find_batch_assignment, find_nearest_available_driver  # noqa: E402
from backend.models import Driver, DriverStatus  # noqa: E402

BOUNDS = BoundingBox(min_lat=37.760, min_lon=-122.470, max_lat=37.800, max_lon=-122.400)
NUM_DRIVERS = 12
NUM_RIDERS = 8
TRIALS = 50
SEED = 7


def make_drivers() -> dict[str, Driver]:
    drivers = {}
    for _ in range(NUM_DRIVERS):
        did = uuid.uuid4().hex[:8]
        drivers[did] = Driver(
            id=did,
            lat=random.uniform(BOUNDS.min_lat, BOUNDS.max_lat),
            lon=random.uniform(BOUNDS.min_lon, BOUNDS.max_lon),
            heading=0,
            speed_kmh=25,
        )
    return drivers


def make_riders() -> list[tuple[str, float, float]]:
    return [
        (
            uuid.uuid4().hex[:8],
            random.uniform(BOUNDS.min_lat, BOUNDS.max_lat),
            random.uniform(BOUNDS.min_lon, BOUNDS.max_lon),
        )
        for _ in range(NUM_RIDERS)
    ]


def run_greedy(drivers: dict[str, Driver], riders: list[tuple[str, float, float]]) -> list[float]:
    drivers = {did: replace(d) for did, d in drivers.items()}  # don't mutate the shared scenario
    index = QuadTree(BOUNDS)
    for d in drivers.values():
        index.insert(d.lat, d.lon, d.id)

    distances = []
    for _, lat, lon in riders:
        match = find_nearest_available_driver(index, lat, lon, drivers)
        if match is None:
            continue
        driver, dist = match
        distances.append(dist)
        driver.status = DriverStatus.EN_ROUTE_TO_PICKUP
        index.remove(driver.lat, driver.lon, driver.id)
    return distances


def run_batch(drivers: dict[str, Driver], riders: list[tuple[str, float, float]]) -> list[float]:
    drivers = {did: replace(d) for did, d in drivers.items()}
    return [dist for _, _, dist in find_batch_assignment(riders, drivers)]


def main() -> None:
    random.seed(SEED)
    greedy_totals: list[float] = []
    batch_totals: list[float] = []

    for _ in range(TRIALS):
        drivers = make_drivers()
        riders = make_riders()
        greedy_totals.append(sum(run_greedy(drivers, riders)))
        batch_totals.append(sum(run_batch(drivers, riders)))

    avg_greedy = sum(greedy_totals) / len(greedy_totals)
    avg_batch = sum(batch_totals) / len(batch_totals)
    improvement = (avg_greedy - avg_batch) / avg_greedy * 100 if avg_greedy else 0.0

    print(f"scenario: {NUM_RIDERS} simultaneous riders, {NUM_DRIVERS} available drivers, {TRIALS} trials\n")
    print(f"greedy  avg total pickup distance: {avg_greedy:.3f} km  (avg/rider: {avg_greedy / NUM_RIDERS:.3f} km)")
    print(f"batch   avg total pickup distance: {avg_batch:.3f} km  (avg/rider: {avg_batch / NUM_RIDERS:.3f} km)")
    print(f"\nbatch reduces total pickup distance by {improvement:.1f}% on average")


if __name__ == "__main__":
    main()
