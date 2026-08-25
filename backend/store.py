"""SQLite-backed persistence for trip history.

`Simulation` itself is still in-memory (see README: "What's deliberately
left out") — it forgets everything on restart, and only keeps a completed
trip around for a few ticks before dropping it so the map doesn't grow
unbounded. `TripStore` gives trips a durable home alongside that: every
request, match, and status change is upserted into a local SQLite file, so
`GET /api/trips` can answer "what happened" even after the process restarts.
Production Uber does the equivalent with a proper durable trip-state store
(e.g. a replicated database) written to on every state transition; a single
SQLite file is the same idea at demo scale, using only the standard library.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import Trip

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trips.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    rider_lat REAL NOT NULL,
    rider_lon REAL NOT NULL,
    dest_lat REAL NOT NULL,
    dest_lon REAL NOT NULL,
    driver_id TEXT,
    status TEXT NOT NULL,
    eta_min REAL,
    requested_at_tick INTEGER,
    matched_at_tick INTEGER,
    completed_at_tick INTEGER,
    requested_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class TripStore:
    """Thin wrapper around one SQLite connection, one row per trip.

    `upsert_trip` is called on every status transition (requested, matched,
    in progress, completed / no-drivers-available) and just overwrites the
    row for that trip id, so the table always reflects each trip's latest
    known state — a full audit log isn't the point here, "did this trip
    happen and how did it end" is.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def upsert_trip(self, trip: Trip) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT INTO trips (
                id, rider_lat, rider_lon, dest_lat, dest_lon, driver_id,
                status, eta_min, requested_at_tick, matched_at_tick,
                completed_at_tick, requested_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                driver_id = excluded.driver_id,
                status = excluded.status,
                eta_min = excluded.eta_min,
                matched_at_tick = excluded.matched_at_tick,
                completed_at_tick = excluded.completed_at_tick,
                updated_at = excluded.updated_at
            """,
            (
                trip.id,
                trip.rider_lat,
                trip.rider_lon,
                trip.dest_lat,
                trip.dest_lon,
                trip.driver_id,
                trip.status.value,
                trip.eta_min,
                trip.requested_at_tick,
                trip.matched_at_tick,
                trip.completed_at_tick,
                now,
                now,
            ),
        )
        self._conn.commit()

    def list_trips(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            """
            SELECT id, rider_lat, rider_lon, dest_lat, dest_lon, driver_id,
                   status, eta_min, requested_at_tick, matched_at_tick,
                   completed_at_tick, requested_at, updated_at
            FROM trips
            ORDER BY requested_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
