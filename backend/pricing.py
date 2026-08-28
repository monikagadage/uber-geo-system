"""Surge pricing: a simple per-grid-cell supply/demand multiplier.

This is intentionally a toy model, not a production pricing engine. Real
surge pricing accounts for time-of-day baselines, elasticity of demand,
driver incentive effects, historical smoothing, and multi-zone spillover.
None of that is here. The one signal this module uses is:

    demand = number of ride requests that originated in a grid cell within
             the last SURGE_WINDOW_TICKS ticks
    supply = number of currently AVAILABLE drivers physically inside that
             same grid cell right now

and the multiplier is a direct function of how much demand outstrips
supply in that cell, clamped to a maximum. That's it -- no smoothing, no
decay curve, no cross-cell effects, no historical baseline. It's exactly
the same idea that motivates matching.py's local dispatch (a request only
"sees" nearby supply), applied to a coarser grid instead of a radius query,
because pricing wants to reason about a whole neighborhood, not one
expanding ring per request.

`Simulation` (backend/simulation.py) owns the actual bookkeeping — a
rolling window of recent request cells and the live driver dict — and
calls into this module's pure functions. Keeping the math here, with no
`Simulation` object required, mirrors backend/matching.py's split and
keeps this independently testable/benchmarkable.
"""

from __future__ import annotations

from .geo import BoundingBox

# Grid resolution for surge calculation: the city bounding box is sliced
# into GRID_SIZE x GRID_SIZE cells. Coarser than the QuadTree/H3 spatial
# index on purpose -- pricing wants "this neighborhood is busy," not
# individual-driver precision, and a coarser grid means each cell usually
# has enough drivers/requests in it for the ratio to mean something.
GRID_SIZE = 5

# How far back "recent demand" looks, in simulation ticks (1 tick == 1
# second at the default TICK_SECONDS). 30 ticks is a deliberately short,
# arbitrary window chosen to keep the demo's surge visibly reactive within
# a short test run -- not a tuned production constant.
SURGE_WINDOW_TICKS = 30

# Multiplier is 1.0x when supply >= demand in a cell, and climbs linearly
# with the shortage ratio above that, capped here so the demo never shows
# an absurd number.
MAX_SURGE_MULTIPLIER = 3.0

BASE_FARE_USD = 4.0
PER_KM_USD = 1.75


def grid_cell(lat: float, lon: float, bounds: BoundingBox, grid_size: int = GRID_SIZE) -> tuple[int, int]:
    """Which (row, col) cell of a grid_size x grid_size grid over `bounds`
    contains (lat, lon). Points outside `bounds` are clamped to the nearest
    edge cell rather than raising -- callers may pass exact boundary points
    (e.g. 37.800000000001 due to float rounding) and should get a sensible
    cell, not a crash.
    """
    lat_span = bounds.max_lat - bounds.min_lat
    lon_span = bounds.max_lon - bounds.min_lon
    lat_frac = (lat - bounds.min_lat) / lat_span if lat_span else 0.0
    lon_frac = (lon - bounds.min_lon) / lon_span if lon_span else 0.0
    row = min(grid_size - 1, max(0, int(lat_frac * grid_size)))
    col = min(grid_size - 1, max(0, int(lon_frac * grid_size)))
    return row, col


def surge_multiplier(demand: int, supply: int) -> float:
    """Pure function: cell demand/supply counts -> a fare multiplier.

    supply >= demand (drivers can cover current local demand): 1.0x, no
    surge. supply < demand: multiplier grows linearly with the shortage
    fraction (max(0, demand - supply) / supply), capped at
    MAX_SURGE_MULTIPLIER. A cell with recent demand but zero available
    drivers is treated as fully surged (the cap) rather than dividing by
    zero -- there's no data to say *how* short supply is, only that it's
    completely absent.
    """
    if demand <= 0:
        return 1.0
    if supply <= 0:
        return MAX_SURGE_MULTIPLIER
    shortage_ratio = max(0, demand - supply) / supply
    return round(min(MAX_SURGE_MULTIPLIER, 1.0 + shortage_ratio), 2)


def base_fare_usd(distance_km: float) -> float:
    """Flat base fare + per-km rate. No time-based component, no
    minimums/surcharges -- a deliberately simple stand-in for whatever a
    real fare-estimation service would compute."""
    return round(BASE_FARE_USD + PER_KM_USD * distance_km, 2)
