"""Routing over OSRM: exactly one route request per API call, then cached in-process."""
from __future__ import annotations

import re
import time

import requests
from django.conf import settings


class RoutingError(Exception):
    pass


def normalize_highway(text: str) -> str | None:
    """'I-20' / 'I 20' / 'IH 20' / 'Interstate 20' -> 'I20'; 'US 69' -> 'US69'."""
    text = text.strip().upper()
    if not text or not any(ch.isdigit() for ch in text):
        return None
    # "Historic US 66" -> "US 66": strip the marker so both normalizers agree.
    text = re.sub(r"^HISTORIC\s+", "", text)
    m = re.match(r"^([A-Z .]*?)\s*-?\s*(\d{1,4}[A-Z]?)(?:\s+(?:BUS|BUSINESS|LOOP|SPUR))?$", text)
    if not m:
        return None
    cls = re.sub(r"[^A-Z]", "", m.group(1))
    num = m.group(2)
    if cls in ("I", "IH", "INTERSTATE", "INTERSTAT"):
        cls = "I"
    elif cls in ("US", "USHWY"):
        cls = "US"
    elif cls in ("FM", "RM", "CR"):
        pass
    elif cls and len(cls) == 2 and cls not in ("SH", "SR"):
        cls = "SH"  # two-letter state prefix, e.g. "TX 31"
    elif cls not in ("SH", "SR"):
        cls = "SH"  # STATE/HWY/HIGHWAY/STATEHIGHWAY and friends
    return f"{cls}{num}" if cls else num


# Simple in-process cache: {(start, finish): (timestamp, payload)}
_ROUTE_CACHE: dict[tuple, tuple[float, dict]] = {}
_ROUTE_CACHE_TTL_SECONDS = 3600
_ROUTE_CACHE_MAX = 64  # bounded: routes are ~100-300KB each
_ROUTE_API_CALLS = 0  # real HTTP calls made (cache hits not counted)


def route_api_call_count() -> int:
    return _ROUTE_API_CALLS


def get_route(start: tuple[float, float], finish: tuple[float, float]) -> dict:
    """Driving route between two (lat, lon) points.

    Returns {'geometry': [(lat, lon)...], 'distance_miles': float, 'duration_seconds': float}.
    One HTTP call per unique (start, finish) pair; results cached for an hour.
    """
    key = (round(start[0], 5), round(start[1], 5), round(finish[0], 5), round(finish[1], 5))
    hit = _ROUTE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _ROUTE_CACHE_TTL_SECONDS:
        return hit[1]

    coords = f"{start[1]},{start[0]};{finish[1]},{finish[0]}"  # OSRM wants lon,lat
    url = f"{settings.OSRM_URL}/route/v1/driving/{coords}"
    try:
        resp = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson", "steps": "true"},
            headers={"User-Agent": settings.HTTP_USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"routing request failed: {exc}") from exc

    routes = data.get("routes") or []
    if not routes:
        raise RoutingError("no driving route found between the two locations")

    route = routes[0]
    geometry = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]  # geojson is lon,lat
    # Highway refs traversed (e.g. "I 20", "US 69;US 69 Bus") - one routing
    # call still, but it tells us exactly which roads the route uses.
    highways: set[str] = set()
    for leg in route.get("legs", []):
        for step in leg.get("steps", []):
            ref = step.get("ref") or ""
            for token in ref.split(";"):
                norm = normalize_highway(token)
                if norm:
                    highways.add(norm)
    payload = {
        "geometry": geometry,
        "distance_miles": route["distance"] / 1609.344,
        "duration_seconds": route["duration"],
        "highways": highways,
    }
    global _ROUTE_API_CALLS
    _ROUTE_API_CALLS += 1
    if len(_ROUTE_CACHE) >= _ROUTE_CACHE_MAX:
        # Evict the oldest quarter rather than growing without bound.
        for stale_key in sorted(_ROUTE_CACHE, key=lambda k: _ROUTE_CACHE[k][0])[: _ROUTE_CACHE_MAX // 4]:
            del _ROUTE_CACHE[stale_key]
    _ROUTE_CACHE[key] = (time.monotonic(), payload)
    return payload
