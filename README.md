# Geo Dispatch Demo — a small, working model of Uber's matching system

A runnable simulation of the core of Uber's geo-dispatch system: driver
location ingestion, a spatial index for fast "who's near me" queries, nearest-driver
matching, and real-time updates to a map UI. It's not production infrastructure —
it's the same *algorithms* stripped down to something you can read end to end in
one sitting. See [DESIGN.md](DESIGN.md) for architecture, component breakdown,
and benchmark numbers.

## Run it

```bash
cd uber-geo-system
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8420
```

Open `http://localhost:8420`. Click the map to set a pickup pin, click again for
a dropoff pin, then hit **Request Ride**. You'll see a driver turn orange
("en route to pickup"), drive to you, turn blue ("on trip"), and drive you to
the destination — 40 simulated drivers are wandering the map the whole time.

Trip history persists across restarts in a local SQLite file
(`data/trips.db`, created on first run) — see `GET /api/trips`.

By default, ride requests are matched greedily (nearest free driver, the
instant the request arrives). To try the batch-assignment strategy instead:

```bash
MATCHING_STRATEGY=batch .venv/bin/uvicorn backend.main:app --reload --port 8420
```

With batch matching, a ride request goes "pending" until the next batch
window (every 3 ticks) resolves a whole group of pending requests at once.

## Features

- QuadTree spatial index with O(log n) radius queries, updated incrementally
  per driver ping instead of rebuilt from scratch each tick
- Two matching strategies: greedy expanding-ring search, and batch
  assignment via the Hungarian algorithm (`scipy.optimize.linear_sum_assignment`)
- SQLite-backed trip persistence (`backend/store.py`) — trip history
  survives a restart
- FastAPI + WebSocket backend broadcasting live driver/trip state
- Leaflet map frontend, no build step

## Tech stack

Python 3.10+, FastAPI, uvicorn, Pydantic, scipy (Hungarian algorithm),
stdlib `sqlite3`. Frontend: one static HTML file with Leaflet, served by
FastAPI's `StaticFiles`.

## Benchmarks

```bash
.venv/bin/python3 benchmarks/index_rebuild_vs_incremental.py
.venv/bin/python3 benchmarks/batch_vs_greedy.py
```

See [DESIGN.md](DESIGN.md#benchmarks) for what each script measures and the
actual numbers they produce.
