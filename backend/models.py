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
