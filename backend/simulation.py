"""The simulation loop: drives fake driver GPS pings, keeps the spatial index
fresh, and runs each trip through its state machine one tick at a time.

This stands in for what would, in production, be several separate services
talking over Kafka: a location-ingestion service (drivers -> stream),
a geo-index service consuming that stream to keep an in-memory/Redis index
current, and a dispatch service reading from the index on each ride request.
Here they're just three methods on one class, called once per tick — see
README.md for how each maps onto the real, distributed version.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass

from .geo import BoundingBox, QuadTree, bearing_deg, destination_point, haversine_km
from .matching import eta_minutes, find_batch_assignment, find_nearest_available_driver
from .models import Driver, DriverStatus, Trip, TripStatus
from .store import TripStore

# Downtown San Francisco, roughly 4.5km x 6km.
CITY_BOUNDS = BoundingBox(min_lat=37.760, min_lon=-122.470, max_lat=37.800, max_lon=-122.400)

ARRIVAL_THRESHOLD_KM = 0.05  # snap to target within 50m
COMPLETED_TRIP_TTL_TICKS = 8  # keep a finished trip visible for a few ticks


@dataclass
class SimulationConfig:
    num_drivers: int = 40
    tick_seconds: float = 1.0
    # "greedy" matches each request the instant it arrives, to the nearest
    # free driver. "batch" holds requests for `batch_window_ticks` and
    # solves the whole group at once (see matching.find_batch_assignment).
    matching_strategy: str = "greedy"
    batch_window_ticks: int = 3
    batch_max_wait_ticks: int = 12  # give up and report no-drivers after this long pending
    # Update only the moved driver's cell(s) each tick instead of rebuilding
    # the whole QuadTree. Kept switchable so the two strategies can be
    # benchmarked against each other (see benchmarks/).
    incremental_index: bool = True


class Simulation:
    def __init__(self, config: SimulationConfig | None = None, store: TripStore | None = None):
        self.config = config or SimulationConfig()
        self.store = store
        self.drivers: dict[str, Driver] = {}
        self.trips: dict[str, Trip] = {}
        self.index: QuadTree = self._empty_index()
        self.tick_count = 0
        self.pending_batch: list[str] = []  # trip ids awaiting the next batch window
        self._ticks_since_batch = 0
        self._spawn_drivers()
        self._rebuild_index()

    # ---- setup -----------------------------------------------------

    def _empty_index(self) -> QuadTree:
        return QuadTree(CITY_BOUNDS)

    def _spawn_drivers(self) -> None:
        for _ in range(self.config.num_drivers):
            driver_id = uuid.uuid4().hex[:8]
            lat = random.uniform(CITY_BOUNDS.min_lat, CITY_BOUNDS.max_lat)
            lon = random.uniform(CITY_BOUNDS.min_lon, CITY_BOUNDS.max_lon)
            self.drivers[driver_id] = Driver(
                id=driver_id,
                lat=lat,
                lon=lon,
                heading=random.uniform(0, 360),
                speed_kmh=random.uniform(20, 35),
            )

    def _rebuild_index(self) -> None:
        """Rebuild the spatial index from current AVAILABLE driver positions.

        A production geo-index updates incrementally (one cell write per GPS
        ping) instead of rebuilding from scratch. A full rebuild each tick is
        O(n log n) instead of O(log n) per update — fine at demo scale
        (tens-hundreds of drivers), not how you'd do it at Uber's scale.
        """
        self.index = self._empty_index()
        for driver in self.drivers.values():
            if driver.status == DriverStatus.AVAILABLE:
                self.index.insert(driver.lat, driver.lon, driver.id)

    # ---- movement ----------------------------------------------------

    def _step_driver(self, driver: Driver, dt_seconds: float) -> None:
        step_km = driver.speed_kmh * (dt_seconds / 3600)

        if driver.status == DriverStatus.AVAILABLE:
            old_lat, old_lon = driver.lat, driver.lon
            if random.random() < 0.15:
                driver.heading = (driver.heading + random.uniform(-45, 45)) % 360
            new_lat, new_lon = destination_point(driver.lat, driver.lon, driver.heading, step_km)
            if not CITY_BOUNDS.contains(new_lat, new_lon):
                driver.heading = (driver.heading + 180 + random.uniform(-30, 30)) % 360
                new_lat, new_lon = destination_point(driver.lat, driver.lon, driver.heading, step_km)
                new_lat, new_lon = CITY_BOUNDS.clamp(new_lat, new_lon)
            driver.lat, driver.lon = new_lat, new_lon
            if self.config.incremental_index:
                # Touch only the cell(s) this driver moved between, instead
                # of rebuilding the whole tree once per tick (see
                # _rebuild_index and README "Incremental spatial index").
                self.index.remove(old_lat, old_lon, driver.id)
                self.index.insert(driver.lat, driver.lon, driver.id)
            return

        # EN_ROUTE_TO_PICKUP or ON_TRIP: head straight for the trip's target.
        trip = self.trips.get(driver.trip_id) if driver.trip_id else None
        if trip is None:
            driver.status = DriverStatus.AVAILABLE
            return

        target_lat, target_lon = (
            (trip.rider_lat, trip.rider_lon)
            if driver.status == DriverStatus.EN_ROUTE_TO_PICKUP
            else (trip.dest_lat, trip.dest_lon)
        )
        remaining_km = haversine_km(driver.lat, driver.lon, target_lat, target_lon)

        if remaining_km <= max(step_km, ARRIVAL_THRESHOLD_KM):
            driver.lat, driver.lon = target_lat, target_lon
            self._advance_trip_state(driver, trip)
        else:
            bearing = bearing_deg(driver.lat, driver.lon, target_lat, target_lon)
            driver.lat, driver.lon = destination_point(driver.lat, driver.lon, bearing, step_km)

        if trip.status in (TripStatus.MATCHED, TripStatus.IN_PROGRESS):
            trip.eta_min = eta_minutes(haversine_km(driver.lat, driver.lon, target_lat, target_lon))

    def _advance_trip_state(self, driver: Driver, trip: Trip) -> None:
        if driver.status == DriverStatus.EN_ROUTE_TO_PICKUP:
            driver.status = DriverStatus.ON_TRIP
            trip.status = TripStatus.IN_PROGRESS
        elif driver.status == DriverStatus.ON_TRIP:
            driver.status = DriverStatus.AVAILABLE
            driver.trip_id = None
            trip.status = TripStatus.COMPLETED
            trip.eta_min = 0
            trip.completed_at_tick = self.tick_count
            if self.config.incremental_index:
                # Driver is back in the pool; give it a fresh index entry
                # at its current (dropoff) position.
                self.index.insert(driver.lat, driver.lon, driver.id)
        if self.store:
            self.store.upsert_trip(trip)

    # ---- public API ----------------------------------------------------

    def tick(self) -> None:
        self.tick_count += 1
        for driver in self.drivers.values():
            self._step_driver(driver, self.config.tick_seconds)
        if not self.config.incremental_index:
            self._rebuild_index()

        if self.config.matching_strategy == "batch":
            self._ticks_since_batch += 1
            if self.pending_batch and self._ticks_since_batch >= self.config.batch_window_ticks:
                self._run_batch_matching()
                self._ticks_since_batch = 0

        stale = [
            t.id
            for t in self.trips.values()
            if t.status == TripStatus.COMPLETED
            and t.completed_at_tick is not None
            and self.tick_count - t.completed_at_tick > COMPLETED_TRIP_TTL_TICKS
        ]
        for trip_id in stale:
            del self.trips[trip_id]

    def _match_driver(self, trip: Trip, driver: Driver, distance_km: float) -> None:
        """Common bookkeeping once a driver has been chosen for a trip,
        shared by the greedy (immediate) and batch (deferred) paths."""
        trip.status = TripStatus.MATCHED
        trip.driver_id = driver.id
        trip.eta_min = eta_minutes(distance_km)
        trip.matched_at_tick = self.tick_count
        driver.status = DriverStatus.EN_ROUTE_TO_PICKUP
        driver.trip_id = trip.id
        if self.config.incremental_index:
            self.index.remove(driver.lat, driver.lon, driver.id)
        if self.store:
            self.store.upsert_trip(trip)

    def request_ride(self, rider_lat: float, rider_lon: float, dest_lat: float, dest_lon: float) -> Trip:
        trip_id = uuid.uuid4().hex[:8]
        trip = Trip(
            id=trip_id,
            rider_lat=rider_lat,
            rider_lon=rider_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            status=TripStatus.PENDING,
            requested_at_tick=self.tick_count,
        )
        self.trips[trip_id] = trip

        if self.config.matching_strategy == "batch":
            # Deferred: parked here until the next batch window resolves it
            # in _run_batch_matching(). Rider sees "pending" until then.
            self.pending_batch.append(trip_id)
            if self.store:
                self.store.upsert_trip(trip)
            return trip

        # Greedy: match immediately against the nearest available driver.
        match = find_nearest_available_driver(self.index, rider_lat, rider_lon, self.drivers)
        if match is None:
            trip.status = TripStatus.NO_DRIVERS_AVAILABLE
            trip.completed_at_tick = self.tick_count
            if self.store:
                self.store.upsert_trip(trip)
            return trip

        driver, distance_km = match
        self._match_driver(trip, driver, distance_km)
        if not self.config.incremental_index:
            self._rebuild_index()  # this driver is no longer AVAILABLE; keep index consistent
        return trip

    def _run_batch_matching(self) -> None:
        """Solve the accumulated pending-request queue as one assignment
        problem (see matching.find_batch_assignment) instead of matching
        each request the instant it arrived.
        """
        pending = [
            (tid, self.trips[tid].rider_lat, self.trips[tid].rider_lon)
            for tid in self.pending_batch
            if tid in self.trips
        ]
        matched_ids: set[str] = set()
        if pending:
            for trip_id, driver_id, distance_km in find_batch_assignment(pending, self.drivers):
                self._match_driver(self.trips[trip_id], self.drivers[driver_id], distance_km)
                matched_ids.add(trip_id)
            if not self.config.incremental_index:
                self._rebuild_index()

        still_pending = []
        for trip_id in self.pending_batch:
            if trip_id in matched_ids:
                continue
            trip = self.trips.get(trip_id)
            if trip is None:
                continue
            if self.tick_count - trip.requested_at_tick >= self.config.batch_max_wait_ticks:
                trip.status = TripStatus.NO_DRIVERS_AVAILABLE
                trip.completed_at_tick = self.tick_count
                if self.store:
                    self.store.upsert_trip(trip)
            else:
                still_pending.append(trip_id)
        self.pending_batch = still_pending

    def snapshot(self) -> dict:
        return {
            "tick": self.tick_count,
            "matching_strategy": self.config.matching_strategy,
            "drivers": [
                {
                    "id": d.id,
                    "lat": round(d.lat, 6),
                    "lon": round(d.lon, 6),
                    "heading": round(d.heading, 1),
                    "status": d.status.value,
                }
                for d in self.drivers.values()
            ],
            "trips": [
                {
                    "id": t.id,
                    "status": t.status.value,
                    "rider_lat": t.rider_lat,
                    "rider_lon": t.rider_lon,
                    "dest_lat": t.dest_lat,
                    "dest_lon": t.dest_lon,
                    "driver_id": t.driver_id,
                    "eta_min": t.eta_min,
                }
                for t in self.trips.values()
            ],
            "bounds": {
                "min_lat": CITY_BOUNDS.min_lat,
                "min_lon": CITY_BOUNDS.min_lon,
                "max_lat": CITY_BOUNDS.max_lat,
                "max_lon": CITY_BOUNDS.max_lon,
            },
        }

