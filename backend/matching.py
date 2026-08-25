"""Dispatch: matches ride requests to available drivers.

Real Uber dispatch also weighs surge/demand balancing, driver ratings, and
destination-filter preferences — this keeps to the part every implementation
shares: turn "who's closest and free" into a fast spatial query instead of an
O(n) scan of the whole fleet, then expand the search radius if the first ring
comes up empty (sparse-supply areas need a wider net).

Two matching strategies live here:

- `find_nearest_available_driver` — greedy, one rider at a time. Simple and
  low-latency, but under contention (several riders requesting near the same
  driver in the same moment) it can leave the group as a whole worse off:
  whoever is processed first takes the closest driver even if a different
  pairing would shorten everyone's pickup.
- `find_batch_assignment` — collects several pending requests and solves the
  whole group at once as a linear assignment problem (Hungarian algorithm),
  minimizing total pickup distance across the batch instead of each rider's
  distance in isolation.
"""

from __future__ import annotations

from scipy.optimize import linear_sum_assignment

from .geo import QuadTree, haversine_km
from .models import Driver, DriverStatus

SEARCH_RINGS_KM = [1, 2, 4, 8, 16]
AVG_URBAN_SPEED_KMH = 25.0


def find_nearest_available_driver(
    index: QuadTree, rider_lat: float, rider_lon: float, drivers: dict[str, Driver]
) -> tuple[Driver, float] | None:
    """Expanding-ring search: try a 1km radius, then widen until a driver is
    found or we run out of rings. Returns (driver, distance_km) or None.
    """
    for radius_km in SEARCH_RINGS_KM:
        candidates = index.query_radius(rider_lat, rider_lon, radius_km)
        available = [
            (dist, driver_id)
            for dist, driver_id in candidates
            if drivers.get(driver_id) and drivers[driver_id].status == DriverStatus.AVAILABLE
        ]
        if available:
            available.sort(key=lambda pair: pair[0])
            best_dist, best_id = available[0]
            return drivers[best_id], best_dist
    return None


def eta_minutes(distance_km: float) -> float:
    return round((distance_km / AVG_URBAN_SPEED_KMH) * 60, 1)


def find_batch_assignment(
    pending: list[tuple[str, float, float]], drivers: dict[str, Driver]
) -> list[tuple[str, str, float]]:
    """Match a batch of pending ride requests to available drivers at once.

    `pending` is [(trip_id, rider_lat, rider_lon), ...]. Builds the full
    rider-x-driver pickup-distance matrix and solves it with
    `scipy.optimize.linear_sum_assignment` (the Hungarian algorithm), which
    finds the pairing that minimizes *total* pickup distance across the
    whole batch in polynomial time — not each rider's nearest driver
    independently, which is what the greedy strategy does.

    Returns [(trip_id, driver_id, distance_km), ...], one entry per match
    made. If there are more riders than available drivers (or none at all),
    some trip_ids simply don't appear in the result and stay pending.
    """
    available = [d for d in drivers.values() if d.status == DriverStatus.AVAILABLE]
    if not pending or not available:
        return []

    cost = [
        [haversine_km(rider_lat, rider_lon, d.lat, d.lon) for d in available]
        for _, rider_lat, rider_lon in pending
    ]
    row_idx, col_idx = linear_sum_assignment(cost)
    return [(pending[r][0], available[c].id, cost[r][c]) for r, c in zip(row_idx, col_idx)]
