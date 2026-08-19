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

## The pieces, and what they map to in the real system

| This repo | Production Uber (roughly) |
|---|---|
| [`backend/geo.py`](backend/geo.py) — `QuadTree` | **H3** hexagonal geo-index. Both exist to answer "which drivers are near this point?" in O(log n) instead of scanning every driver. H3 tiles the world in hexagons at multiple resolutions; a quadtree recursively splits rectangles. Different shape, same purpose: turn a spatial query into a small, bounded lookup. |
| [`backend/simulation.py`](backend/simulation.py) — driver movement + tick loop | **Location ingestion service.** In production, each driver's phone streams GPS pings (~every few seconds) into a service (via Kafka or similar) that writes the latest position into the geo-index. Here, one process just fabricates and moves the pings. |
| `Simulation._rebuild_index()` | The index-maintenance step of that ingestion pipeline. We *rebuild the whole tree every tick* (O(n log n)) because it's simple to reason about at 40 drivers. Uber's real index updates **incrementally** — one driver's ping only touches the one or two cells it moved between — because at fleet scale a full rebuild per tick would never keep up. |
| [`backend/matching.py`](backend/matching.py) — expanding-ring search | **Dispatch service.** Query a small radius first, widen if no driver is found (sparse-supply areas need a bigger net). Real dispatch adds ETA-by-road (not straight-line), driver ratings, destination filters, and demand/supply balancing (surge) on top of this same "who's closest and free" core. |
| [`backend/main.py`](backend/main.py) — FastAPI + WebSocket | **API gateway + real-time fanout.** `POST /api/request-ride` is the "tap Request" call; the WebSocket is the push channel a rider/driver app keeps open for live position and trip-status updates. Production does this with a pub/sub layer (e.g. Kafka + a fanout/notification service) across millions of concurrent connections, not one Python process broadcasting to a list of sockets. |
| [`frontend/index.html`](frontend/index.html) | The rider app's map view — Leaflet + a WebSocket client, nothing more. |

## What's deliberately left out

These are the pieces that matter at Uber's actual scale but would just be
plumbing here, not new concepts:

- **Sharding / horizontal scale** — a single in-memory quadtree instead of a
  geo-index partitioned across many machines by region.
- **Durability** — no database; state lives in one process's memory and is
  gone on restart. Production persists trip/driver state (e.g. to a
  distributed store) so a service crash doesn't lose a trip mid-ride.
- **Real road ETAs** — ETA here is straight-line distance ÷ average speed.
  Production uses actual routing (road graph, live traffic) — a whole
  separate "routing service."
- **Surge pricing / supply-demand balancing** — dispatch here always
  picks the nearest free driver; nothing models fare, incentives, or demand
  shaping.
- **Fault tolerance** — no retries, replication, or failover. One process,
  one point of failure, on purpose, to keep the code readable.

## Where to go next

- Swap the `QuadTree` for a real [H3](https://h3geo.org/) index (`pip install h3`)
  and compare query patterns — hexagons avoid the quadtree's uneven cell-size
  problem near boundaries.
- Make `_rebuild_index()` incremental (update just the moved driver's cell)
  and measure the difference at higher driver counts.
- Add a second matching strategy (e.g. batch multiple pending riders and
  solve as an assignment problem) and compare rider wait time vs. the greedy
  nearest-driver approach used here.
