"""H3-backed spatial index -- a second, selectable strategy alongside the
QuadTree in `backend/geo.py`.

Production Uber indexes driver locations with H3 (hexagonal hierarchical
geo-indexing, open-sourced by Uber itself): the map is tiled into hex cells
at a fixed resolution, each driver ping writes to the cell it falls in, and
a "who's near me" query reads the query point's cell plus a ring of
neighboring cells instead of scanning the whole fleet or descending a tree.
This module is a real implementation of that idea using the `h3` PyPI
package (the same library, just called from Python), not a simulation of
one -- see `benchmarks/h3_vs_quadtree.py` for how it actually compares to
the QuadTree on this repo's workload.

Same three-method shape as `QuadTree` (`insert`/`remove`/`query_radius`,
see `geo.SpatialIndex`), so `matching.py` and `simulation.py` work against
either without caring which one they were handed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import h3

from .geo import haversine_km

# H3 resolution 9: ~0.2km hexagon edge length, i.e. roughly comparable
# granularity to a QuadTree leaf cell over this repo's ~4.5km x 6km city
# bounds with the QuadTree's default capacity=8/max_depth=12. Higher
# resolutions (10+) mean smaller hexagons -- more precise cells, but more
# of them to touch per query; lower resolutions (7-8) mean fewer, bigger
# cells. This is a reasonable default for a city-scale demo, not a tuned
# value -- see benchmarks/h3_vs_quadtree.py for how resolution and fleet
# size interact.
DEFAULT_RESOLUTION = 9


@dataclass
class H3Index:
    """Buckets (lat, lon, payload) points by H3 cell at a fixed resolution.

    insert()/remove() are O(1) (one dict lookup by cell id -- no tree
    descent). query_radius() converts the search radius into a ring size
    `k` around the query point's cell (`grid_disk`, H3's equivalent of the
    QuadTree's bounding-box-intersects-circle descent) and only visits
    cells in that ring, then filters to the true haversine distance --
    same "don't scan everything" idea as the QuadTree, different tiling
    shape (hexagons partition the plane far more uniformly near boundaries
    than axis-aligned rectangles do, which is H3's main real advantage).
    """

    resolution: int = DEFAULT_RESOLUTION
    cells: dict[str, dict[Any, tuple[float, float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Hex edge length at this resolution, used to size grid_disk's k
        # for a given search radius (see query_radius).
        self._edge_km: float = h3.average_hexagon_edge_length(self.resolution, unit="km")

    def insert(self, lat: float, lon: float, payload: Any) -> bool:
        cell = h3.latlng_to_cell(lat, lon, self.resolution)
        self.cells.setdefault(cell, {})[payload] = (lat, lon)
        return True

    def remove(self, lat: float, lon: float, payload: Any) -> bool:
        """Remove one (lat, lon, payload) point -- the inverse of insert().

        The common case (the given lat/lon still hashes to the same cell it
        was inserted under) is an O(1) dict lookup. Falls back to scanning
        every cell only if that lookup misses -- e.g. a caller passes a
        slightly different float than what was inserted, landing it just
        the other side of a hexagon boundary. `QuadTree.remove()` has the
        same "point may not be exactly where insert() put it" concern and
        handles it by construction (a node's own `points` list is checked
        directly); H3 buckets by exact-match cell id instead, so this is
        the equivalent safety net, at removal-time cost rather than
        insert-time cost.
        """
        cell = h3.latlng_to_cell(lat, lon, self.resolution)
        bucket = self.cells.get(cell)
        if bucket is not None and payload in bucket:
            del bucket[payload]
            if not bucket:
                del self.cells[cell]
            return True
        for other_cell, other_bucket in list(self.cells.items()):
            if payload in other_bucket:
                del other_bucket[payload]
                if not other_bucket:
                    del self.cells[other_cell]
                return True
        return False

    def query_radius(
        self, lat: float, lon: float, radius_km: float, results: list[tuple[float, Any]] | None = None
    ) -> list[tuple[float, Any]]:
        """Return [(distance_km, payload), ...] for every point within
        radius_km, same contract as `QuadTree.query_radius`."""
        if results is None:
            results = []
        center = h3.latlng_to_cell(lat, lon, self.resolution)
        # +1 ring of slack beyond the geometric minimum so a point near the
        # edge of the last ring (whose cell center is outside radius_km but
        # whose actual position is inside it) isn't missed. Overshooting by
        # a ring costs a few extra (empty, in a sparse index) cell lookups;
        # undershooting would silently drop real matches -- the QuadTree's
        # boundary.intersects_circle() descent makes the same "would rather
        # visit a few extra empty regions than miss a point" tradeoff.
        k = max(1, math.ceil(radius_km / self._edge_km) + 1)
        for cell in h3.grid_disk(center, k):
            bucket = self.cells.get(cell)
            if not bucket:
                continue
            for payload, (plat, plon) in bucket.items():
                d = haversine_km(lat, lon, plat, plon)
                if d <= radius_km:
                    results.append((d, payload))
        return results
