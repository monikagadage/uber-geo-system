# Design

Architecture and design notes for the geo dispatch demo. For "how do I run
it," see [README.md](README.md).

## System overview

One Python process runs a tick loop that moves simulated drivers, maintains
a spatial index of their positions, and matches ride requests to drivers.
FastAPI exposes that state over HTTP + a WebSocket; a static Leaflet page is
the only client.

```
                 POST /api/request-ride
   ┌────────┐    ───────────────────────►   ┌──────────────┐
   │ Browser│                                │   FastAPI    │
   │(Leaflet│    ◄───────────────────────    │  (main.py)   │
   │  map)  │      matched / pending /       └──────┬───────┘
   └───┬────┘      no_drivers_available              │
       │                                             │ sim.request_ride()
       │  WS /ws (push every tick)                    ▼
       │                                     ┌──────────────────┐
       │                                     │   Simulation      │
       └─────────────────────────────────────┤  (simulation.py)  │
                    broadcast(sim.snapshot()) │  drivers, trips,  │
                                               │  tick loop         │
                                               └──┬────────┬───────┘
                                                  │        │
                                    index.query/  │        │ store.upsert_trip()
                                 insert/remove     │        │  on every state change
                                                  ▼        ▼
                                      ┌───────────────┐  ┌──────────────┐
                                      │   QuadTree     │  │  TripStore   │
                                      │   (geo.py)     │  │  (store.py)  │
                                      │  in-memory      │  │  SQLite file │
                                      └───────────────┘  └──────────────┘
                                              ▲
                                              │ find_nearest_available_driver /
                                              │ find_batch_assignment
                                              │ (matching.py)
```

Request flow: a client action (map click -> `POST /api/request-ride`, or
just an open WebSocket) reaches FastAPI, which delegates to `Simulation`.
`Simulation` asks `matching.py` for a driver, `matching.py` queries the
`QuadTree` for nearby candidates, and every trip state change gets upserted
into `TripStore`. Once a tick advances, `Simulation.snapshot()` is broadcast
to every open WebSocket so the map redraws.

## Component breakdown

Each backend module owns one concern, split along the same lines a
production system would split into separate *services* — here they're just
files/classes so the whole thing fits in one process and one sitting.

- **`backend/geo.py` — geometry + `QuadTree`.** Pure math (haversine
  distance, bearing, destination-point projection) plus a region-quadtree
  spatial index. Kept dependency-free and stateless-per-call so it can be
  tested and benchmarked in isolation from the simulation that uses it. This
  is the only module that knows what a "region" or "distance" is; nothing
  else recomputes geometry.
- **`backend/models.py` — `Driver`, `Trip`, and their status enums.** Plain
  dataclasses, no behavior. Kept separate so `matching.py`, `simulation.py`,
  and `store.py` can all import the same shape without any of them owning
  it, and so the state machine (`DriverStatus`, `TripStatus`) is visible in
  one place instead of scattered across whichever module happens to mutate
  status first.
- **`backend/matching.py` — dispatch logic.** Two independent strategies
  (`find_nearest_available_driver`, `find_batch_assignment`) that both take
  a `QuadTree`/driver dict and return a driver assignment; neither knows
  about ticks, trips, or persistence. Separated from `simulation.py` so the
  matching algorithms can be benchmarked and unit-tested against a plain
  `QuadTree` and driver dict, with no `Simulation` object required.
- **`backend/simulation.py` — the tick loop and state machine.** Owns
  driver movement, index maintenance (incremental vs. full-rebuild), trip
  state transitions, and the batch-window timer. This is intentionally the
  "orchestrator" — everything else in `backend/` is a pure function or a
  narrow-purpose store that `Simulation` calls into. Splitting movement,
  indexing, and dispatch into separate modules (rather than inlining them
  here) mirrors how production splits ingestion, indexing, and dispatch
  into separate *services*; keeping them all invoked from one class here
  mirrors that they still form one logical pipeline per driver-ping.
- **`backend/store.py` — `TripStore`.** The only module that touches SQL.
  Isolated behind `upsert_trip`/`list_trips` so `Simulation` doesn't know
  it's SQLite underneath — it could be swapped for Postgres or nothing
  (`store=None`) without touching `simulation.py`'s logic.
- **`backend/main.py` — FastAPI app.** Wires the above together and exposes
  them over HTTP/WebSocket. Contains no dispatch or geometry logic itself —
  its job is request/response shaping and the `ConnectionManager` broadcast
  fanout, nothing more.
- **`frontend/index.html`** — Leaflet map + WebSocket client. No build step,
  no framework; renders whatever `Simulation.snapshot()` sends.

## Matching strategies

`backend/matching.py` implements two ways to turn "a rider wants a ride"
into "a driver is assigned":

- **Greedy (`find_nearest_available_driver`, default).** Runs the instant a
  request arrives. Expanding-ring search: try a 1km radius against the
  `QuadTree`, widen (2, 4, 8, 16km) until an available driver turns up.
  Low latency, but under contention — several riders requesting near the
  same driver in the same moment — whoever's processed first takes the
  closest driver even if a different pairing would shorten the group's
  total wait.
- **Batch (`find_batch_assignment`, `MATCHING_STRATEGY=batch`).** Requests
  queue as `PENDING` for up to `batch_window_ticks` (3 ticks by default),
  then the whole pending group is solved together as a linear assignment
  problem via `scipy.optimize.linear_sum_assignment` (the Hungarian
  algorithm) over the full rider-x-driver pickup-distance matrix —
  minimizing *total* pickup distance across the batch, not each rider's
  distance in isolation.

**When each is better:** greedy wins on latency — a rider is matched (or
told no drivers are available) in one tick, always. Batch wins on total
system efficiency when several requests land close together in time,
at the cost of every rider in that window waiting for the batch to close.
`benchmarks/batch_vs_greedy.py` runs a "surge moment" scenario (8
simultaneous riders competing for 12 available drivers, 50 trials, same
random layout fed to both strategies) and measured:

```
greedy  avg total pickup distance: 8.750 km  (avg/rider: 1.094 km)
batch   avg total pickup distance: 8.019 km  (avg/rider: 1.002 km)
batch reduces total pickup distance by 8.4% on average
```

So under real contention, batch buys a meaningful (~8%) reduction in total
pickup distance for a bounded added wait (up to 3 ticks) — a reasonable
trade when demand briefly outpaces supply, not something you'd want as the
only strategy when requests are spread out in time (there, batch just adds
latency for no benefit, since there's no contention to resolve).

## Incremental index tradeoff

`Simulation._rebuild_index()` (full rebuild, O(n log n): clear the tree,
reinsert every available driver) vs. incrementally calling
`QuadTree.remove()` + `insert()` for just the driver that moved (O(log n)
per driver, `SimulationConfig.incremental_index=True`, the default).
`benchmarks/index_rebuild_vs_incremental.py` measures two different access
patterns because they tell different stories:

**1. Per-tick, whole fleet moves every tick** — this demo's actual
workload (every driver reports a new position every tick, for a lively
map), so a once-per-tick rebuild already batches all n updates into one
pass:

```
 drivers | full rebuild (ms/tick) | incremental (ms/tick) | ratio
      40 |                  0.076 |                  0.101 | 0.75x
     500 |                  1.283 |                  2.180 | 0.59x
    1000 |                  2.978 |                  5.084 | 0.59x
    2000 |                  6.467 |                 11.642 | 0.56x
```

Both are the same O(n log n) big-O at this access pattern — rebuild
touches every driver once, incremental does two tree operations (remove +
insert) per driver, so incremental is consistently ~1.7x *slower* here.
That's a genuine, useful finding, not a reason to skip measuring it: at
this workload the naive rebuild is actually the better choice.

**2. Per-update, cost of one driver ping into an already-built index** —
the pattern production location-ingestion actually sees (pings arrive one
driver at a time, not conveniently batched into a once-a-second rebuild
covering the whole fleet):

```
 drivers | full rebuild (ms/ping) | incremental (ms/ping) | speedup
      40 |                 0.0321 |                 0.0007 |   46.6x
     500 |                 0.8326 |                 0.0007 | 1239.5x
    1000 |                 2.0261 |                 0.0006 | 3278.1x
    2000 |                 4.3494 |                 0.0007 | 6534.6x
```

Here incremental is O(log n) per ping and full-rebuild is O(n) per ping
(every other driver has to be reinserted just to reflect one driver's
move), so the gap widens sharply with fleet size — thousands-of-times
faster at 2000 drivers. This is the access pattern that matters at real
scale, and it's why production geo-indexes (H3-backed or otherwise) update
incrementally rather than rebuild: a system fielding continuous
one-at-a-time pings, not neat once-a-second full-fleet batches, would
never keep up with per-ping full rebuilds as the fleet grows.

`incremental_index` stays a config flag (not just the default) specifically
so both benchmarks remain reproducible against the same simulation code.

## SQLite persistence model

`backend/store.py`'s `TripStore` wraps one `sqlite3` connection (stdlib,
no ORM) over a single `trips` table, one row per trip, keyed by trip id.
`upsert_trip()` is called on every trip state transition (requested,
matched, in-progress, completed / no-drivers-available) and does an
`INSERT ... ON CONFLICT(id) DO UPDATE`, so the row always reflects a trip's
latest known state — this is a "current state" store, not an append-only
event log. `GET /api/trips` reads it back, most-recent-first.

This only covers trip history. `Simulation.drivers` and the `QuadTree`
itself are still purely in-memory: a restart resets every driver to a
random position and forgets the live index, even though `data/trips.db`
still remembers what trips happened. That split is deliberate for this
demo's scope (see "what's deliberately left out" below) but is the first
thing to fix if you wanted this to survive a real restart.

## Fault-tolerant restart

Before this, `Simulation.__init__` always started from a blank slate: 40
fresh drivers at random positions, no trips. That's fine for a clean
`Ctrl+C` shutdown (every trip is already `COMPLETED` or
`NO_DRIVERS_AVAILABLE` by then), but if the process dies mid-trip — a
crash, an out-of-memory kill, a deploy that SIGKILLs instead of draining —
whatever trip was `PENDING`/`MATCHED`/`IN_PROGRESS` at that instant is
frozen at that status in `data/trips.db` forever, and the next process has
no idea it ever existed.

`Simulation._rehydrate_from_store()` (`backend/simulation.py`) runs once,
at the end of `__init__`, right after the fresh driver fleet is spawned:

1. `TripStore.list_active_trips()` reads every trip still
   `pending`/`matched`/`in_progress` in SQLite — by construction, only
   trips an unclean shutdown left mid-flight, since a clean run always
   drives every trip to a terminal status first.
2. Each one is marked `INTERRUPTED` (a new terminal `TripStatus`) and
   upserted back into the store, so it stops being "active" forever.
3. A **brand-new** ride request is submitted for the same rider/destination
   pair, matched against the real, current fleet.

**Why resubmit instead of resuming the same trip toward the same
destination:** driver state (`Simulation.drivers`, the spatial index) was
never persisted — only trip history is durable (see "SQLite persistence
model" above) — so the new process always spawns a fresh fleet at random
positions. The old trip's `driver_id` names a driver that, in general,
doesn't exist in the new fleet at all; even in the coincidental case where
some driver reused that same id, it wouldn't be at the position the old
driver was actually at when the crash happened. Silently "resuming" the
old trip would mean either inventing a driver location out of thin air or
quietly reassigning an unrelated driver — both misrepresent what actually
happened. Marking it `INTERRUPTED` and dispatching a fresh request is
honest about the crash and gets the rider re-matched immediately (or a
clean "no drivers available" if the fleet genuinely can't cover it) —
"let a new match happen," the option this repo's task list called out as
acceptable. Driver-state persistence (so the fleet itself survives a
restart, not just trip records) is still on the "where to go next" list
below; without it, resuming toward the same destination isn't a truthful
option in the first place.

`GET /api/state` (and every `/ws` broadcast) includes a `"rehydration"`
field — `{"active_trips_found", "interrupted", "resubmitted"}` — set once
at startup, so this is observable from outside the process, not just in
server logs. Verification performed for this change: request a ride, poll
until it reaches `in_progress`, `kill -9` the server, confirm the row is
still `in_progress` in `data/trips.db`, restart, and confirm (a) the
startup log line and `GET /api/state`'s `rehydration` field both report
`{"active_trips_found": 1, "interrupted": 1, "resubmitted": 1}`, (b) the
original trip row flips to `status='interrupted'` in SQLite, and (c) a new
trip row appears for the same rider lat/lon, matched to a (different)
driver in the new fleet.

## What's deliberately left out / production differences

These are the pieces that matter at Uber's actual scale but would just be
plumbing here, not new concepts:

- **Sharding / horizontal scale** — a single in-memory quadtree instead of
  a geo-index partitioned across many machines by region.
- **Full durability** — trip history survives a restart (SQLite, see
  `backend/store.py`), but driver state and the spatial index itself are
  still in-memory only, and there's no write-ahead log or replication.
  Production persists to a distributed, replicated store so a service crash
  never loses a trip mid-ride.
- **Real road ETAs** — ETA here is straight-line distance ÷ average speed.
  Production uses actual routing (road graph, live traffic) — a whole
  separate "routing service."
- **Surge pricing / supply-demand balancing** — dispatch here always picks
  the nearest free driver (or minimizes total batch distance); nothing
  models fare, incentives, or demand shaping.
- **Fault tolerance** — a restart now rehydrates *trip* state from SQLite
  (see "Fault-tolerant restart" above), but there's still no retries,
  replication, or failover, and driver/fleet state is never persisted, so
  every restart still spawns a brand-new fleet at random positions. One
  process, one point of failure, on purpose, to keep the code readable.
- **H3 vs. QuadTree** — production Uber indexes with H3 (hexagonal
  hierarchical tiling at multiple resolutions); this repo uses a
  region-quadtree (recursive rectangle splitting). Same big-O purpose
  (turn "who's near me" into a small, bounded lookup instead of an O(n)
  scan), different tiling shape and far less code.

## Where to go next

- Swap the `QuadTree` for a real [H3](https://h3geo.org/) index
  (`pip install h3`) and compare query patterns — hexagons avoid the
  quadtree's uneven cell-size problem near boundaries.
- Persist driver state too (not just trips), so a restart doesn't scatter
  the fleet to random new positions — and so a resumed trip could actually
  continue toward its destination instead of being marked `INTERRUPTED`
  and rematched (see "Fault-tolerant restart" above).
- Make the batch window adapt to demand (shorter when there are more
  pending riders than idle drivers) instead of a fixed
  `batch_window_ticks`.
