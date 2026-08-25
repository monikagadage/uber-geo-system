# Geo Dispatch Demo — a small, working model of Uber's matching system

A runnable simulation of the core of Uber's geo-dispatch system: driver
location ingestion, a spatial index for fast "who's near me" queries, nearest-driver
matching, and real-time updates to a map UI. It's not production infrastructure —
it's the same *algorithms* stripped down to something you can read end to end in
one sitting.

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
window (every 3 ticks) resolves a whole group of pending requests at once —
see [`backend/matching.py`](backend/matching.py) and "Where to go next"
below.

## The pieces, and what they map to in the real system

| This repo | Production Uber (roughly) |
|---|---|
| [`backend/geo.py`](backend/geo.py) — `QuadTree` | **H3** hexagonal geo-index. Both exist to answer "which drivers are near this point?" in O(log n) instead of scanning every driver. H3 tiles the world in hexagons at multiple resolutions; a quadtree recursively splits rectangles. Different shape, same purpose: turn a spatial query into a small, bounded lookup. |
| [`backend/simulation.py`](backend/simulation.py) — driver movement + tick loop | **Location ingestion service.** In production, each driver's phone streams GPS pings (~every few seconds) into a service (via Kafka or similar) that writes the latest position into the geo-index. Here, one process just fabricates and moves the pings. |
| `Simulation._rebuild_index()` / `SimulationConfig.incremental_index` | The index-maintenance step of that ingestion pipeline. The **default** now updates the tree **incrementally** — each moved driver's ping does an O(log n) remove of its old cell plus an O(log n) insert of its new one (`QuadTree.remove`, added alongside `insert`) — instead of throwing the whole tree away and reinserting every driver (O(n log n)) each tick. The old full-rebuild path is kept behind `incremental_index=False` purely so the two can be benchmarked against each other; see [`benchmarks/index_rebuild_vs_incremental.py`](benchmarks/index_rebuild_vs_incremental.py) and the numbers below. |
| [`backend/matching.py`](backend/matching.py) — expanding-ring search + batch assignment | **Dispatch service.** Two strategies: `find_nearest_available_driver` (default) queries a small radius first and widens if no driver is found (sparse-supply areas need a bigger net) — greedy, one rider at a time. `find_batch_assignment` collects several pending requests and solves them together as a linear assignment problem via `scipy.optimize.linear_sum_assignment` (the Hungarian algorithm), minimizing *total* pickup distance across the batch instead of each rider's distance in isolation — closer to how real batching dispatchers behave in high-demand windows. Real dispatch adds ETA-by-road (not straight-line), driver ratings, destination filters, and demand/supply balancing (surge) on top of either. |
| [`backend/store.py`](backend/store.py) — `TripStore` | **Durable trip-state store.** A SQLite file (`data/trips.db`, stdlib `sqlite3`, no ORM) that every trip's request/match/status-change gets upserted into, so trip history survives a process restart even though `Simulation.trips` is still in-memory and trims itself. Production does the equivalent with a proper replicated database written to on every state transition — same idea, one file instead of a cluster. See `GET /api/trips`. |
| [`backend/main.py`](backend/main.py) — FastAPI + WebSocket | **API gateway + real-time fanout.** `POST /api/request-ride` is the "tap Request" call (returns `matched`, `pending` under batch matching, or `no_drivers_available`); `GET /api/trips` is a trip-history read from SQLite; the WebSocket is the push channel a rider/driver app keeps open for live position and trip-status updates. Production does this with a pub/sub layer (e.g. Kafka + a fanout/notification service) across millions of concurrent connections, not one Python process broadcasting to a list of sockets. |
| [`frontend/index.html`](frontend/index.html) | The rider app's map view — Leaflet + a WebSocket client, nothing more. |

## What's deliberately left out

These are the pieces that matter at Uber's actual scale but would just be
plumbing here, not new concepts:

- **Sharding / horizontal scale** — a single in-memory quadtree instead of a
  geo-index partitioned across many machines by region.
- **Full durability** — trip history now survives a restart (SQLite, see
  `backend/store.py`), but driver state and the spatial index itself are
  still in-memory only, and there's no write-ahead log or replication.
  Production persists to a distributed, replicated store so a service crash
  never loses a trip mid-ride.
- **Real road ETAs** — ETA here is straight-line distance ÷ average speed.
  Production uses actual routing (road graph, live traffic) — a whole
  separate "routing service."
- **Surge pricing / supply-demand balancing** — dispatch here always
  picks the nearest free driver; nothing models fare, incentives, or demand
  shaping.
- **Fault tolerance** — no retries, replication, or failover. One process,
  one point of failure, on purpose, to keep the code readable.

## Benchmarks

Two standalone scripts back up the claims above with real numbers instead of
just big-O:

```bash
.venv/bin/python3 benchmarks/index_rebuild_vs_incremental.py
.venv/bin/python3 benchmarks/batch_vs_greedy.py
```

`index_rebuild_vs_incremental.py` runs the same driver-movement simulation
with `incremental_index=True` vs. `False` at several fleet sizes and times
each tick. `batch_vs_greedy.py` generates random "surge moment" scenarios
(several riders requesting at once against a fixed driver pool) and compares
total pickup distance under greedy vs. batch matching. See each script's
docstring for methodology.

## Where to go next

- Swap the `QuadTree` for a real [H3](https://h3geo.org/) index (`pip install h3`)
  and compare query patterns — hexagons avoid the quadtree's uneven cell-size
  problem near boundaries.
- Persist driver state too (not just trips), so a restart doesn't scatter
  the fleet to random new positions.
- Make the batch window adapt to demand (shorter when there are more pending
  riders than idle drivers) instead of a fixed `batch_window_ticks`.
