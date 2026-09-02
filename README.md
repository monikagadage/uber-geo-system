# geo-dispatch — spatial indexing and driver matching, end to end

A working ride-dispatch backend: driver-location ingestion, a spatial index
for "who's near this rider" queries, nearest-driver matching, surge pricing,
crash-safe trip persistence, and a live map UI over WebSocket. The pieces
that matter for scale — the index and the matching strategy — are
**swappable at startup and benchmarked against each other**, with the
numbers (including the ones that don't flatter the fancier option) in
[DESIGN.md](DESIGN.md).

## What's interesting here

| Area | What it does | What the benchmark showed |
|---|---|---|
| **Spatial index** | QuadTree with O(log n) radius queries updated incrementally per ping, or a real [H3](https://h3geo.org/) hex-grid index — `INDEX_BACKEND=h3` | H3 builds 2–6× faster (dict write vs. tree descent), but the **QuadTree wins nearest-driver query time by 1.2–2.4×** at every fleet size, because this workload's expanding-ring search re-queries as it widens. The counter-intuitive result is real and explained, not hidden. |
| **Incremental vs. full index rebuild** | Move one driver: two tree ops, or rebuild the whole index | Depends on access pattern. Whole-fleet-per-tick (this demo): rebuild is ~1.7× *faster*. One-ping-at-a-time (real ingestion): incremental is **40×–1000×+ faster**. Both measured because they tell different stories. |
| **Matching strategy** | Greedy expanding-ring (nearest free driver, immediately), or batch assignment via the Hungarian algorithm — `MATCHING_STRATEGY=batch` | Under contention (8 riders, 12 drivers), batch cuts total pickup distance **~8%** for a bounded added wait. With requests spread out in time it just adds latency — so greedy is the default. |
| **Crash-safe restart** | Trips persist to SQLite; a trip caught mid-flight by an unclean shutdown is marked `interrupted` and a fresh request is submitted against the current fleet | Resubmitting beats trying to resume a stale trip — see [DESIGN.md](DESIGN.md#fault-tolerant-restart). |
| **Surge pricing** | Per-grid-cell multiplier: recent requests vs. available drivers, capped 3× | An honest demo heuristic with stated limitations, not a pricing model. |

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8420
```

Open `http://localhost:8420`. Click the map for a pickup pin, again for a
dropoff, then **Request Ride**. A driver turns orange (en route), drives to
you, turns blue (on trip), and drives you to the destination — 40 simulated
drivers wander the map throughout. Once both pins are set, the panel shows a
live `$base + surge` fare and a translucent surge heatmap.

Swap the internals at startup:

```bash
INDEX_BACKEND=h3         .venv/bin/uvicorn backend.main:app --port 8420
MATCHING_STRATEGY=batch  .venv/bin/uvicorn backend.main:app --port 8420
```

## Benchmarks

```bash
.venv/bin/python3 benchmarks/index_rebuild_vs_incremental.py
.venv/bin/python3 benchmarks/batch_vs_greedy.py
.venv/bin/python3 benchmarks/h3_vs_quadtree.py
```

Each feeds the *same* fixed-seed driver layout through the actual matching
code path the live server uses — not isolated microbenchmarks. See
[DESIGN.md](DESIGN.md#benchmarks) for the full tables and the reasoning.

## Architecture

```
driver pings ──▶ spatial index ──┐
                                 ├──▶ matching ──▶ trip ──▶ SQLite (store.py)
rider request ───────────────────┘        │                    │
                                          ▼                    ▼
                              surge (pricing.py)      rehydrate on restart
                                          │
        FastAPI + WebSocket broadcast ◀───┴───▶ Leaflet map (no build step)
```

| Module | Responsibility |
|---|---|
| `backend/geo.py` | QuadTree spatial index, incremental update |
| `backend/h3_index.py` | H3 hex-grid index (the `h3` package), same interface |
| `backend/matching.py` | greedy expanding-ring + batch (Hungarian) assignment |
| `backend/pricing.py` | per-cell surge multiplier |
| `backend/store.py` | SQLite trip persistence + startup rehydration |
| `backend/simulation.py` | the 40-driver world, tick loop, index-rebuild policy |
| `backend/main.py` | FastAPI app, WebSocket state broadcast, static frontend |

## Tech stack

Python 3.10+, FastAPI, uvicorn, Pydantic, scipy (`linear_sum_assignment`),
h3, stdlib `sqlite3`. Frontend: one static HTML file with Leaflet.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/request-ride` | submit a ride request (pickup + dropoff) |
| `GET /api/fare-estimate` | live `$base + surge` for a pickup cell |
| `GET /api/state` | full world snapshot, incl. `rehydration` status |
| `GET /api/trips` | trip history (survives restarts) |
| `WS /ws` | live driver/trip state stream |
