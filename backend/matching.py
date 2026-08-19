"""Dispatch: matches a ride request to the nearest available driver.

Real Uber dispatch also weighs surge/demand balancing, driver ratings, and
destination-filter preferences — this keeps to the part every implementation
shares: turn "who's closest and free" into a fast spatial query instead of an
O(n) scan of the whole fleet, then expand the search radius if the first ring
comes up empty (sparse-supply areas need a wider net).
"""

from __future__ import annotations

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
