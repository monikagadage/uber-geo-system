"""FastAPI app: real-time driver location streaming + ride-request API.

Endpoints mirror what a client-facing API gateway would expose in the real
system: POST /api/request-ride is the "rider taps Request" call, GET
/api/state is a REST fallback/initial-load snapshot, and WS /ws is the
push channel a mobile app would keep open for live driver movement and trip
status (in production: a pub/sub fanout service, not a Python list of
sockets).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .simulation import Simulation, SimulationConfig
from .store import TripStore

TICK_SECONDS = 1.0

# "greedy" (default) or "batch" — see SimulationConfig / backend/matching.py.
# Set via e.g. `MATCHING_STRATEGY=batch uvicorn backend.main:app ...`.
MATCHING_STRATEGY = os.environ.get("MATCHING_STRATEGY", "greedy")

# "quadtree" (default) or "h3" — see SimulationConfig / backend/h3_index.py.
# Set via e.g. `INDEX_BACKEND=h3 uvicorn backend.main:app ...`.
INDEX_BACKEND = os.environ.get("INDEX_BACKEND", "quadtree")


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


store = TripStore()
sim = Simulation(
    SimulationConfig(
        num_drivers=40,
        tick_seconds=TICK_SECONDS,
        matching_strategy=MATCHING_STRATEGY,
        index_backend=INDEX_BACKEND,
    ),
    store=store,
)
manager = ConnectionManager()

if sim.rehydration["active_trips_found"]:
    print(
        f"[startup] rehydrated from {store.db_path}: "
        f"{sim.rehydration['active_trips_found']} trip(s) were still active at last shutdown -> "
        f"marked INTERRUPTED and resubmitted {sim.rehydration['resubmitted']} fresh ride request(s)."
    )
else:
    print(f"[startup] rehydrated from {store.db_path}: no active trips found (clean start).")


async def simulation_loop() -> None:
    while True:
        sim.tick()
        await manager.broadcast(sim.snapshot())
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(simulation_loop())
    yield
    task.cancel()


app = FastAPI(title="Uber-style Geo Dispatch Demo", lifespan=lifespan)


class RideRequest(BaseModel):
    rider_lat: float
    rider_lon: float
    dest_lat: float
    dest_lon: float


@app.post("/api/request-ride")
def request_ride(req: RideRequest) -> dict:
    trip = sim.request_ride(req.rider_lat, req.rider_lon, req.dest_lat, req.dest_lon)
    return {
        "trip_id": trip.id,
        "status": trip.status.value,
        "driver_id": trip.driver_id,
        "eta_min": trip.eta_min,
        "fare_usd": trip.fare_usd,
        "surge_multiplier": trip.surge_multiplier,
    }


@app.get("/api/fare-estimate")
def fare_estimate(rider_lat: float, rider_lon: float, dest_lat: float, dest_lon: float) -> dict:
    """Upfront fare quote for a not-yet-requested trip, including the
    current surge multiplier at the pickup location -- see
    backend/pricing.py and DESIGN.md "Surge pricing" for exactly what
    signal drives the multiplier. Read-only: doesn't record demand or
    change any state, unlike POST /api/request-ride.
    """
    return sim.fare_estimate(rider_lat, rider_lon, dest_lat, dest_lon)


@app.get("/api/state")
def get_state() -> dict:
    return sim.snapshot()


@app.get("/api/trips")
def get_trips(limit: int = 50) -> dict:
    """Trip history from SQLite — survives a process restart, unlike
    `sim.trips` which only holds recently-active/completed trips in memory.
    """
    return {"trips": store.list_trips(limit=limit)}


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        await websocket.send_json(sim.snapshot())
        while True:
            await websocket.receive_text()  # keep the connection open; ignore client pings
    except WebSocketDisconnect:
        manager.disconnect(websocket)


frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
