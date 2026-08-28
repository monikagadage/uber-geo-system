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
(`data/trips.db`, created on first run) — see `GET /api/trips`. On startup
the server also rehydrates any trip left `pending`/`matched`/`in_progress`
by an unclean shutdown (a crash, `kill -9`, ...): it's marked
`interrupted` and a fresh ride request is submitted for the same
rider/destination against the real, current fleet. See `GET /api/state`'s
`"rehydration"` field, and [DESIGN.md](DESIGN.md#fault-tolerant-restart)
for why resubmitting beats trying to resume the same trip.

By default, ride requests are matched greedily (nearest free driver, the
instant the request arrives). To try the batch-assignment strategy instead:

```bash
MATCHING_STRATEGY=batch .venv/bin/uvicorn backend.main:app --reload --port 8420
```

With batch matching, a ride request goes "pending" until the next batch
window (every 3 ticks) resolves a whole group of pending requests at once.

The spatial index itself is also swappable — QuadTree (default) or a real
[H3](https://h3geo.org/) hex-grid index:

```bash
INDEX_BACKEND=h3 .venv/bin/uvicorn backend.main:app --reload --port 8420
```

See [DESIGN.md](DESIGN.md#h3-alternative-index) for how they compare —
short version: QuadTree wins on this repo's actual query pattern, real
benchmark numbers included, not a guess.

Once both pickup and dropoff are set, the panel shows a live fare
estimate — `$base + surge` — and the map overlays a translucent red
"surge heatmap" grid (toggle it off in the panel). Surge is a simple,
honest ratio: recent ride requests vs. currently-available drivers in the
same grid cell, capped at 3x — see
[DESIGN.md](DESIGN.md#surge-pricing) for exactly what drives it and its
known limitations (it's a demo heuristic, not a pricing model).

## Features

- Two spatial index backends, selectable at startup: a QuadTree with
  O(log n) radius queries updated incrementally per driver ping, and a
  real H3 hex-grid index (`backend/h3_index.py`, the `h3` package)
- Two matching strategies: greedy expanding-ring search, and batch
  assignment via the Hungarian algorithm (`scipy.optimize.linear_sum_assignment`)
- SQLite-backed trip persistence (`backend/store.py`) — trip history
  survives a restart, and any trip caught mid-flight by an unclean
  shutdown is rehydrated and rematched on the next startup
- Surge pricing (`backend/pricing.py`) — a per-grid-cell fare multiplier
  driven by recent-requests-vs-available-drivers, exposed via
  `GET /api/fare-estimate` and shown live in the frontend
- FastAPI + WebSocket backend broadcasting live driver/trip state
- Leaflet map frontend, no build step

## Tech stack

Python 3.10+, FastAPI, uvicorn, Pydantic, scipy (Hungarian algorithm), h3
(hex-grid spatial index), stdlib `sqlite3`. Frontend: one static HTML file
with Leaflet, served by FastAPI's `StaticFiles`.

## Benchmarks

```bash
.venv/bin/python3 benchmarks/index_rebuild_vs_incremental.py
.venv/bin/python3 benchmarks/batch_vs_greedy.py
.venv/bin/python3 benchmarks/h3_vs_quadtree.py
```

See [DESIGN.md](DESIGN.md#benchmarks) for what each script measures and the
actual numbers they produce.
