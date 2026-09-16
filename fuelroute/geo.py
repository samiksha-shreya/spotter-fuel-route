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


def _segment_distance_miles(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Distance from p to segment a-b, and the projection fraction in [0, 1].

    Local equirectangular projection around p's latitude - accurate to
    centimetres over the mile-scale segments OSRM returns.
    """
    lat0 = math.radians(p[0])
    kx = math.cos(lat0) * 69.093  # miles per degree lon at this latitude
    ky = 68.703                    # miles per degree lat
    ax, ay = (a[1] - p[1]) * kx, (a[0] - p[0]) * ky
    bx, by = (b[1] - p[1]) * kx, (b[0] - p[0]) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py), t


def _segments(polyline):
    pts = list(polyline)
    return pts, zip(pts, pts[1:])


def distance_to_polyline_miles(point: Point, polyline: Iterable[Point]) -> float:
    """True distance from a point to a polyline (min over segments).

    Vertex-only distance overestimates for stops near the midpoint of a long
    straight segment (a stop 1 mile off the route mid-segment could measure
    5+ miles and be wrongly dropped from the corridor), so project onto each
    segment instead.
    """
    pts, segs = _segments(polyline)
    if len(pts) == 1:
        return haversine_miles(point, pts[0])
    return min(_segment_distance_miles(point, a, b)[0] for a, b in segs)


def mile_marker_for_point(point: Point, polyline: Sequence[Point], markers: Sequence[float]) -> float:
    """Mile marker of a point along the route (projected onto the nearest segment)."""
    best_d, best_mile = float("inf"), 0.0
    for i in range(1, len(polyline)):
        d, t = _segment_distance_miles(point, polyline[i - 1], polyline[i])
        if d < best_d:
            best_d = d
            best_mile = markers[i - 1] + t * (markers[i] - markers[i - 1])
    return best_mile
