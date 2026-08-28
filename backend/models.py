from dataclasses import dataclass
from enum import Enum


class DriverStatus(str, Enum):
    AVAILABLE = "available"
    EN_ROUTE_TO_PICKUP = "en_route_to_pickup"
    ON_TRIP = "on_trip"


class TripStatus(str, Enum):
    PENDING = "pending"  # batch strategy only: waiting for the next batch window
    MATCHED = "matched"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_DRIVERS_AVAILABLE = "no_drivers_available"
    # Terminal status assigned on startup rehydration to any trip that was
    # PENDING/MATCHED/IN_PROGRESS when the process died — see
    # Simulation._rehydrate_from_store() and DESIGN.md "Fault-tolerant
    # restart". The driver it names (if any) may not even exist in the new
    # process's freshly-spawned fleet, so this trip itself never resumes;
    # a brand-new trip is submitted for the same rider/destination instead.
    INTERRUPTED = "interrupted"


@dataclass
class Driver:
    id: str
    lat: float
    lon: float
    heading: float
    speed_kmh: float
    status: DriverStatus = DriverStatus.AVAILABLE
    trip_id: str | None = None


@dataclass
class Trip:
    id: str
    rider_lat: float
    rider_lon: float
    dest_lat: float
    dest_lon: float
    status: TripStatus
    driver_id: str | None = None
    eta_min: float | None = None
    completed_at_tick: int | None = None
    requested_at_tick: int | None = None
    matched_at_tick: int | None = None
    # Fare estimate computed at request time (backend/pricing.py). Fixed at
    # request time, not recomputed as the trip progresses -- same as a real
    # upfront-fare quote.
    fare_usd: float | None = None
    surge_multiplier: float | None = None
