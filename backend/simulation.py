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
from .pricing import GRID_SIZE, SURGE_WINDOW_TICKS, base_fare_usd, grid_cell, surge_multiplier
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
        # Rolling window of recent ride-request grid cells, as (tick, row,
        # col), used only for surge_multiplier_at()'s demand signal — see
        # backend/pricing.py. Pruned every tick in tick().
        self._recent_request_cells: list[tuple[int, int, int]] = []
        self._spawn_drivers()
        self._rebuild_index()
        self.rehydration: dict = self._rehydrate_from_store()

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

    def _rehydrate_from_store(self) -> dict:
        """Recover in-flight trip state from `TripStore` on startup instead
        of always starting from a blank slate (see DESIGN.md "Fault-tolerant
        restart" for the full writeup of this choice).

        `Simulation.drivers` and `self.index` are never persisted (see
        README: "what's deliberately left out"), so a restart always spawns
        a brand-new fleet at random positions — there is no old driver state
        to rehydrate them into. What *is* durable is `TripStore`, so this
        only rehydrates trips: any trip still PENDING/MATCHED/IN_PROGRESS in
        the store when this process starts was left that way by an unclean
        shutdown (a clean run always drives every trip to COMPLETED or
        NO_DRIVERS_AVAILABLE before it could go stale). For each one:

        1. Mark it INTERRUPTED in the store — a new terminal status, so it
           stops showing up as "still active" forever. Its `driver_id` (if
           any) is meaningless now: that driver id likely doesn't exist in
           the freshly-spawned fleet, and even if a same-named id existed by
           chance, it wouldn't be at the position the old driver was headed
           to. Resuming the *same* trip object toward its destination would
           mean silently teleporting a driver to wherever the old one
           happened to be — worse than admitting the ride was interrupted.
        2. Submit a brand-new ride request for the same rider/destination
           pair, so the rider is matched again (or told no drivers are
           available) against the real, current fleet — "let a new match
           happen," not "pretend nothing happened."

        Returns a small stats dict so callers (main.py) can log/expose what
        happened, which is also how the "kill mid-trip, restart, prove it
        survived" verification is observed from the outside.
        """
        if self.store is None:
            return {"active_trips_found": 0, "interrupted": 0, "resubmitted": 0}

        active = self.store.list_active_trips()
        resubmitted = 0
        for row in active:
            interrupted = Trip(
                id=row["id"],
                rider_lat=row["rider_lat"],
                rider_lon=row["rider_lon"],
                dest_lat=row["dest_lat"],
                dest_lon=row["dest_lon"],
                status=TripStatus.INTERRUPTED,
                driver_id=row["driver_id"],
                eta_min=None,
                requested_at_tick=row["requested_at_tick"],
                matched_at_tick=row["matched_at_tick"],
                completed_at_tick=self.tick_count,
            )
            self.store.upsert_trip(interrupted)
            self.request_ride(row["rider_lat"], row["rider_lon"], row["dest_lat"], row["dest_lon"])
            resubmitted += 1

        return {
            "active_trips_found": len(active),
            "interrupted": len(active),
            "resubmitted": resubmitted,
        }

    # ---- surge pricing -------------------------------------------------

    def _prune_recent_request_cells(self) -> None:
        cutoff = self.tick_count - SURGE_WINDOW_TICKS
        self._recent_request_cells = [c for c in self._recent_request_cells if c[0] >= cutoff]

    def _record_request_cell(self, rider_lat: float, rider_lon: float) -> None:
        row, col = grid_cell(rider_lat, rider_lon, CITY_BOUNDS)
        self._recent_request_cells.append((self.tick_count, row, col))

    def surge_multiplier_at(self, lat: float, lon: float) -> float:
        """Current surge multiplier for whichever grid cell (lat, lon) falls
        in. demand = ride requests in that cell within the last
        SURGE_WINDOW_TICKS ticks; supply = AVAILABLE drivers physically in
        that cell right now. See backend/pricing.py for the exact formula
        and its (deliberate) limitations.
        """
        target = grid_cell(lat, lon, CITY_BOUNDS)
        demand = sum(1 for _, row, col in self._recent_request_cells if (row, col) == target)
        supply = sum(
            1
            for d in self.drivers.values()
            if d.status == DriverStatus.AVAILABLE and grid_cell(d.lat, d.lon, CITY_BOUNDS) == target
        )
        return surge_multiplier(demand, supply)

    def fare_estimate(self, rider_lat: float, rider_lon: float, dest_lat: float, dest_lon: float) -> dict:
        """Upfront fare quote: base_fare(distance) * surge_multiplier(pickup
        cell). Doesn't touch or record any state — safe to call as many
        times as a rider drags the pickup/dropoff pins around before
        actually requesting (see GET /api/fare-estimate).
        """
        distance_km = haversine_km(rider_lat, rider_lon, dest_lat, dest_lon)
        fare = base_fare_usd(distance_km)
        surge = self.surge_multiplier_at(rider_lat, rider_lon)
        return {
            "distance_km": round(distance_km, 3),
            "base_fare_usd": fare,
            "surge_multiplier": surge,
            "estimated_fare_usd": round(fare * surge, 2),
        }

    def surge_grid(self) -> list[dict]:
        """Multiplier for every cell of the GRID_SIZE x GRID_SIZE surge
        grid, with its bounding box, so the frontend can paint a live surge
        heatmap instead of only quoting a fare after the fact. O(grid_size^2
        * num_drivers + grid_size^2 * recent_requests) -- fine at this
        demo's scale (a 5x5 grid, tens-hundreds of drivers), broadcast once
        per tick alongside everything else in snapshot().
        """
        lat_span = CITY_BOUNDS.max_lat - CITY_BOUNDS.min_lat
        lon_span = CITY_BOUNDS.max_lon - CITY_BOUNDS.min_lon
        cells = []
        for row in range(GRID_SIZE):
            for col in range(GRID_SIZE):
                demand = sum(1 for _, r, c in self._recent_request_cells if (r, c) == (row, col))
                supply = sum(
                    1
                    for d in self.drivers.values()
                    if d.status == DriverStatus.AVAILABLE and grid_cell(d.lat, d.lon, CITY_BOUNDS) == (row, col)
                )
                cells.append(
                    {
                        "row": row,
                        "col": col,
                        "min_lat": CITY_BOUNDS.min_lat + lat_span * row / GRID_SIZE,
                        "max_lat": CITY_BOUNDS.min_lat + lat_span * (row + 1) / GRID_SIZE,
                        "min_lon": CITY_BOUNDS.min_lon + lon_span * col / GRID_SIZE,
                        "max_lon": CITY_BOUNDS.min_lon + lon_span * (col + 1) / GRID_SIZE,
                        "demand": demand,
                        "supply": supply,
                        "multiplier": surge_multiplier(demand, supply),
                    }
                )
        return cells

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
        self._prune_recent_request_cells()

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
        # Record demand for surge purposes before quoting the fare, so this
        # request's own presence counts toward the surge it's quoted --
        # same as it would for anyone requesting into that cell right after.
        self._record_request_cell(rider_lat, rider_lon)
        fare = self.fare_estimate(rider_lat, rider_lon, dest_lat, dest_lon)
        trip = Trip(
            id=trip_id,
            rider_lat=rider_lat,
            rider_lon=rider_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            status=TripStatus.PENDING,
            requested_at_tick=self.tick_count,
            fare_usd=fare["estimated_fare_usd"],
            surge_multiplier=fare["surge_multiplier"],
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
            # What startup rehydration found/did (see _rehydrate_from_store) —
            # zeros on a completely fresh database, non-zero right after a
            # restart that followed an unclean shutdown mid-trip.
            "rehydration": self.rehydration,
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
                    "fare_usd": t.fare_usd,
                    "surge_multiplier": t.surge_multiplier,
                }
                for t in self.trips.values()
            ],
            "bounds": {
                "min_lat": CITY_BOUNDS.min_lat,
                "min_lon": CITY_BOUNDS.min_lon,
                "max_lat": CITY_BOUNDS.max_lat,
                "max_lon": CITY_BOUNDS.max_lon,
            },
            # Live surge multiplier per grid cell -- see "Surge pricing" in
            # DESIGN.md for exactly what demand/supply signal drives this.
            "surge_grid": self.surge_grid(),
        }

