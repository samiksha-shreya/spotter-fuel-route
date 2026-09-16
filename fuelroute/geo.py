"""Geographic helpers: distances, polyline sampling, corridor tests.

Everything here is pure Python over (lat, lon) pairs - no external calls.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_MILES = 3958.7613

Point = tuple[float, float]  # (lat, lon)


def haversine_miles(a: Point, b: Point) -> float:
    """Great-circle distance between two (lat, lon) points in miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(h))


def cumulative_distances(points: Sequence[Point]) -> list[float]:
    """Mile markers along a polyline: output[i] = distance from start to points[i]."""
    markers = [0.0]
    for i in range(1, len(points)):
        markers.append(markers[-1] + haversine_miles(points[i - 1], points[i]))
    return markers


def sample_polyline(points: Sequence[Point], markers: Sequence[float], step_miles: float) -> list[tuple[float, Point]]:
    """Walk a polyline and yield (mile_marker, point) roughly every step_miles.

    Used to reduce a dense route geometry to a manageable set of probe points
    for reverse geocoding and proximity checks.
    """
    if not points:
        return []
    samples: list[tuple[float, Point]] = [(0.0, points[0])]
    next_mark = step_miles
    for i in range(1, len(points)):
        while markers[i] >= next_mark:
            # interpolate between points[i-1] and points[i]
            span = markers[i] - markers[i - 1]
            frac = 0.0 if span == 0 else (next_mark - markers[i - 1]) / span
            lat = points[i - 1][0] + frac * (points[i][0] - points[i - 1][0])
            lon = points[i - 1][1] + frac * (points[i][1] - points[i - 1][1])
            samples.append((next_mark, (lat, lon)))
            next_mark += step_miles
    samples.append((markers[-1], points[-1]))
    return samples


def distance_to_polyline_miles(point: Point, polyline: Iterable[Point]) -> float:
    """Approximate distance from a point to a polyline (min over vertices).

    Route geometries from OSRM are dense (sub-mile spacing on highways), so
    vertex distance is a good approximation and much cheaper than true
    point-to-segment projection for corridor filtering.
    """
    return min(haversine_miles(point, vertex) for vertex in polyline)


def mile_marker_for_point(point: Point, polyline: Sequence[Point], markers: Sequence[float]) -> float:
    """Approximate mile marker of a point along the route (nearest vertex)."""
    best_i, best_d = 0, float("inf")
    for i, vertex in enumerate(polyline):
        d = haversine_miles(point, vertex)
        if d < best_d:
            best_i, best_d = i, d
    return markers[best_i]
