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
from .matching import eta_minutes, find_nearest_available_driver
from .models import Driver, DriverStatus, Trip, TripStatus

# Downtown San Francisco, roughly 4.5km x 6km.
CITY_BOUNDS = BoundingBox(min_lat=37.760, min_lon=-122.470, max_lat=37.800, max_lon=-122.400)

ARRIVAL_THRESHOLD_KM = 0.05  # snap to target within 50m
COMPLETED_TRIP_TTL_TICKS = 8  # keep a finished trip visible for a few ticks


@dataclass
class SimulationConfig:
    num_drivers: int = 40
    tick_seconds: float = 1.0


class Simulation:
    def __init__(self, config: SimulationConfig | None = None):
        self.config = config or SimulationConfig()
        self.drivers: dict[str, Driver] = {}
        self.trips: dict[str, Trip] = {}
        self.index: QuadTree = self._empty_index()
        self.tick_count = 0
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
            if random.random() < 0.15:
                driver.heading = (driver.heading + random.uniform(-45, 45)) % 360
            new_lat, new_lon = destination_point(driver.lat, driver.lon, driver.heading, step_km)
            if not CITY_BOUNDS.contains(new_lat, new_lon):
                driver.heading = (driver.heading + 180 + random.uniform(-30, 30)) % 360
                new_lat, new_lon = destination_point(driver.lat, driver.lon, driver.heading, step_km)
                new_lat, new_lon = CITY_BOUNDS.clamp(new_lat, new_lon)
            driver.lat, driver.lon = new_lat, new_lon
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

    # ---- public API ----------------------------------------------------

    def tick(self) -> None:
        self.tick_count += 1
        for driver in self.drivers.values():
            self._step_driver(driver, self.config.tick_seconds)
        self._rebuild_index()
        stale = [
            t.id
            for t in self.trips.values()
            if t.status == TripStatus.COMPLETED
            and t.completed_at_tick is not None
            and self.tick_count - t.completed_at_tick > COMPLETED_TRIP_TTL_TICKS
        ]
        for trip_id in stale:
            del self.trips[trip_id]

    def request_ride(self, rider_lat: float, rider_lon: float, dest_lat: float, dest_lon: float) -> Trip:
        trip_id = uuid.uuid4().hex[:8]
        match = find_nearest_available_driver(self.index, rider_lat, rider_lon, self.drivers)

        if match is None:
            trip = Trip(
                id=trip_id,
                rider_lat=rider_lat,
                rider_lon=rider_lon,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                status=TripStatus.NO_DRIVERS_AVAILABLE,
                completed_at_tick=self.tick_count,
            )
            self.trips[trip_id] = trip
            return trip

        driver, distance_km = match
        trip = Trip(
            id=trip_id,
            rider_lat=rider_lat,
            rider_lon=rider_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            status=TripStatus.MATCHED,
            driver_id=driver.id,
            eta_min=eta_minutes(distance_km),
        )
        driver.status = DriverStatus.EN_ROUTE_TO_PICKUP
        driver.trip_id = trip_id
        self.trips[trip_id] = trip
        self._rebuild_index()  # this driver is no longer AVAILABLE; keep index consistent
        return trip

    def snapshot(self) -> dict:
        return {
            "tick": self.tick_count,
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

