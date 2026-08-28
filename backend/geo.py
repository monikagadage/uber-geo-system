"""Geospatial primitives: distance/bearing math and a QuadTree spatial index.

In production, Uber indexes driver locations with H3 (hexagonal hierarchical
geo-indexing): the map is tiled into hex cells at multiple resolutions, each
driver update writes to the cell it falls in, and a "find drivers near me"
query just reads the rider's cell plus its ring of neighbors instead of
scanning the whole fleet. A QuadTree (recursively split rectangles instead of
hexagons) gives the same big-O behavior — O(log n) region queries instead of
O(n) full scans — with far less code, which is why we use it here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

EARTH_RADIUS_KM = 6371.0


class SpatialIndex(Protocol):
    """The three operations `matching.py` and `simulation.py` need from a
    spatial index. `QuadTree` (below) and `backend/h3_index.py`'s
    `H3Index` both satisfy this structurally (no inheritance needed) --
    see `SimulationConfig.index_backend` for how a running simulation picks
    one or the other and `benchmarks/h3_vs_quadtree.py` for a head-to-head
    comparison.
    """

    def insert(self, lat: float, lon: float, payload: Any) -> bool: ...

    def remove(self, lat: float, lon: float, payload: Any) -> bool: ...

    def query_radius(
        self, lat: float, lon: float, radius_km: float, results: list[tuple[float, Any]] | None = None
    ) -> list[tuple[float, Any]]: ...


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing (0-360) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def destination_point(lat: float, lon: float, bearing: float, distance_km: float) -> tuple[float, float]:
    """Point reached by travelling `distance_km` from (lat, lon) on `bearing`."""
    delta = distance_km / EARTH_RADIUS_KM
    theta = math.radians(bearing)
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)

    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lambda2) + 540) % 360 - 180


@dataclass
class BoundingBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon

    def clamp(self, lat: float, lon: float) -> tuple[float, float]:
        return (
            min(max(lat, self.min_lat), self.max_lat),
            min(max(lon, self.min_lon), self.max_lon),
        )

    def intersects_circle(self, lat: float, lon: float, radius_km: float) -> bool:
        clat, clon = self.clamp(lat, lon)
        return haversine_km(lat, lon, clat, clon) <= radius_km

    def split(self) -> tuple["BoundingBox", "BoundingBox", "BoundingBox", "BoundingBox"]:
        mid_lat = (self.min_lat + self.max_lat) / 2
        mid_lon = (self.min_lon + self.max_lon) / 2
        return (
            BoundingBox(mid_lat, self.min_lon, self.max_lat, mid_lon),  # NW
            BoundingBox(mid_lat, mid_lon, self.max_lat, self.max_lon),  # NE
            BoundingBox(self.min_lat, self.min_lon, mid_lat, mid_lon),  # SW
            BoundingBox(self.min_lat, mid_lon, mid_lat, self.max_lon),  # SE
        )


@dataclass
class QuadTree:
    """A region-quadtree spatial index over (lat, lon, payload) points.

    insert() is O(log n) amortized; query_radius() only descends into
    quadrants whose bounding box actually intersects the search circle, so a
    "nearby drivers" lookup touches a small fraction of the tree instead of
    every point — same idea as an H3 ring query, different tiling shape.
    """

    boundary: BoundingBox
    capacity: int = 8
    max_depth: int = 12
    depth: int = 0
    points: list[tuple[float, float, Any]] = field(default_factory=list)
    children: list["QuadTree"] = field(default_factory=list)
    divided: bool = False

    def insert(self, lat: float, lon: float, payload: Any) -> bool:
        if not self.boundary.contains(lat, lon):
            return False
        if len(self.points) < self.capacity or self.depth >= self.max_depth:
            self.points.append((lat, lon, payload))
            return True
        if not self.divided:
            self._subdivide()
        for child in self.children:
            if child.insert(lat, lon, payload):
                return True
        self.points.append((lat, lon, payload))  # shouldn't happen; keep points safe
        return True

    def _subdivide(self) -> None:
        self.children = [QuadTree(b, self.capacity, self.max_depth, self.depth + 1) for b in self.boundary.split()]
        self.divided = True

    def remove(self, lat: float, lon: float, payload: Any) -> bool:
        """Remove a single (lat, lon, payload) point — the inverse of insert().

        A point can end up stored at any node along its insertion path (see
        insert(): a node keeps points it already held even after it later
        subdivides), so removal checks this node's own points first, then
        recurses into children if the boundary contains the point and it
        wasn't found here. Cost is O(depth), not O(n): this is what makes
        "remove the old cell, insert the new cell" for one moved driver
        cheap enough to do every tick instead of rebuilding the whole tree.
        """
        if not self.boundary.contains(lat, lon):
            return False
        for i, (plat, plon, ppayload) in enumerate(self.points):
            if plat == lat and plon == lon and ppayload == payload:
                del self.points[i]
                return True
        if self.divided:
            for child in self.children:
                if child.remove(lat, lon, payload):
                    return True
        return False

    def query_radius(
        self, lat: float, lon: float, radius_km: float, results: list[tuple[float, Any]] | None = None
    ) -> list[tuple[float, Any]]:
        """Return [(distance_km, payload), ...] for every point within radius_km."""
        if results is None:
            results = []
        if not self.boundary.intersects_circle(lat, lon, radius_km):
            return results
        for plat, plon, payload in self.points:
            d = haversine_km(lat, lon, plat, plon)
            if d <= radius_km:
                results.append((d, payload))
        if self.divided:
            for child in self.children:
                child.query_radius(lat, lon, radius_km, results)
        return results
