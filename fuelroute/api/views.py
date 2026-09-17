"""HTTP layer: POST /api/route-with-fuel/  {"start": "...", "finish": "..."}

One routing API call per request (cached after the first), geocoding served
from a persistent cache wherever possible.
"""
from __future__ import annotations

import json
import math
import time

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from fuelroute.geo import (
    cumulative_distances,
    distance_to_polyline_miles,
    mile_marker_for_point,
    sample_polyline,
)
from fuelroute.services.fuel import (
    STATE_ABBR_TO_NAME,
    FuelStop,
    PlannedStop,
    candidate_stops,
    geocode_stop,
    normalize_city,
    plan_refuelling,
)
from fuelroute.services.geocoding import GeocodeError, get_geocoder
from fuelroute.services.routing import RoutingError, get_route, route_api_call_count

# Corridor tuning
_REVERSE_SAMPLE_MILES = 75.0   # probe spacing for state/city detection
_CORRIDOR_RADIUS_MILES = 3.0   # max distance from the route for a usable stop
_MAX_GEOCODE_STOPS = 150       # safety bound on per-request geocoding work
_CITY_PREFILTER_MILES = 12.0   # drop stops whose town sits far off the route


def health(request):
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_http_methods(["POST"])
def route_fuel(request):
    started = time.monotonic()
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Request body must be JSON."}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "Request body must be a JSON object."}, status=400)
    start_v, finish_v = body.get("start"), body.get("finish")
    if not isinstance(start_v, str) or not isinstance(finish_v, str):
        return JsonResponse(
            {"error": "'start' and 'finish' must be strings (US city/address)."}, status=400
        )

    start_q = start_v.strip()
    finish_q = finish_v.strip()
    if not start_q or not finish_q:
        return JsonResponse(
            {"error": "Provide both 'start' and 'finish' (US city/address strings)."},
            status=400,
        )

    geocoder = get_geocoder()
    try:
        start = geocoder.forward(query=f"{start_q}, USA")
        finish = geocoder.forward(query=f"{finish_q}, USA")
    except GeocodeError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    if not start or not finish:
        missing = "start" if not start else "finish"
        return JsonResponse({"error": f"Could not geocode the {missing} location within the USA."}, status=422)

    routing_calls_before = route_api_call_count()
    try:
        route = get_route(start, finish)
    except RoutingError as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    routing_calls = route_api_call_count() - routing_calls_before

    geometry = route["geometry"]
    markers = cumulative_distances(geometry)
    distance_miles = route["distance_miles"]

    response: dict = {
        "start": {"query": start_q, "lat": start[0], "lon": start[1]},
        "finish": {"query": finish_q, "lat": finish[0], "lon": finish[1]},
        "route": {
            "distance_miles": round(distance_miles, 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 2),
            # GeoJSON LineString (lon, lat order) - drop into any map renderer.
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in geometry],
            },
        },
        "vehicle": {
            "range_miles": settings.VEHICLE_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
        },
    }

    if distance_miles <= settings.VEHICLE_RANGE_MILES:
        response["fuel_stops"] = []
        response["fuel"] = {
            "total_gallons": 0.0,
            "total_cost": 0.0,
            "note": "Route is within one tank of fuel; no fuel stops needed (vehicle starts full).",
        }
        response["meta"] = _meta(started, geocoded_stops=0, routing_calls=routing_calls)
        return JsonResponse(response)

    # 1. Find the corridor: reverse-geocode route probes -> states + cities.
    probes = sample_polyline(geometry, markers, _REVERSE_SAMPLE_MILES)
    state_codes: set[str] = set()
    city_names: set[str] = set()
    probe_failures = 0
    for _, (lat, lon) in probes:
        try:
            addr = geocoder.reverse(lat, lon)
        except GeocodeError:
            probe_failures += 1  # one bad probe must not abort the request
            continue
        if addr.get("state"):
            code = _abbr_for_state(addr["state"])
            if code:
                state_codes.add(code)
        if addr.get("city"):
            city_names.add(addr["city"])
    if probe_failures and not state_codes and not city_names:
        return JsonResponse(
            {"error": "geocoding unavailable while mapping the route corridor"}, status=502
        )

    # 2. Candidate truckstops: on a traversed highway or in a traversed
    #    locality, then prefiltered by their town's distance to the route.
    candidates = candidate_stops(state_codes, city_names, route.get("highways"))
    by_city: dict[tuple[str, str], list] = {}
    for stop in candidates:
        by_city.setdefault((stop.state, normalize_city(stop.city)), []).append(stop)
    # Order survivors by position along the route (the city pin from the
    # prefilter doubles as a free approximate mile marker), so the geocode
    # budget below is spent across the whole corridor instead of an
    # alphabetical slice of states. Without this, a long route can burn the
    # entire cap on far-end states and report the start as infeasible.
    positioned: list[tuple[float, FuelStop]] = []
    unpositioned: list[FuelStop] = []
    for (state, city), city_stops in sorted(by_city.items()):
        state_name = STATE_ABBR_TO_NAME.get(state, state)
        try:
            city_pt = geocoder.forward(query=f"{city_stops[0].city}, {state_name}, USA")
        except GeocodeError:
            # Geocoder hiccup on one town: keep its stops rather than abort
            # the whole request; the precise pin below still filters them.
            unpositioned.extend(city_stops)
            continue
        if city_pt is None:
            unpositioned.extend(city_stops)
        elif distance_to_polyline_miles(city_pt, geometry) <= _CITY_PREFILTER_MILES:
            mile = mile_marker_for_point(city_pt, geometry, markers)
            positioned.extend((mile, s) for s in city_stops)
    positioned.sort(key=lambda pair: pair[0])
    kept = [s for _, s in positioned] + unpositioned
    if len(kept) > _MAX_GEOCODE_STOPS:
        # Even stride along the whole route: full corridor coverage within
        # the cap, so every 500-mile window keeps candidate stops.
        stride = math.ceil(len(kept) / _MAX_GEOCODE_STOPS)
        kept = kept[::stride]

    # 3. Precise geocode per surviving stop (cached) and pin to a mile marker.
    pinned: list[PlannedStop] = []
    geocode_failures = 0
    for stop in kept[: _MAX_GEOCODE_STOPS]:
        try:
            point = geocode_stop(geocoder, stop)
        except GeocodeError:
            geocode_failures += 1  # skip this stop; others may still pin
            continue
        if point is None:
            continue
        if distance_to_polyline_miles(point, geometry) <= _CORRIDOR_RADIUS_MILES:
            pinned.append(
                PlannedStop(
                    stop=stop,
                    lat=point[0],
                    lon=point[1],
                    mile_marker=mile_marker_for_point(point, geometry, markers),
                )
            )
    if geocode_failures and not pinned:
        return JsonResponse(
            {"error": "geocoding unavailable while locating fuel stops"}, status=502
        )

    # 4. Cheapest refuelling plan.
    try:
        planned, total_cost = plan_refuelling(pinned, distance_miles)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=422)

    response["fuel_stops"] = [
        {
            "name": s.stop.name,
            "address": f"{s.stop.address}, {s.stop.city}, {s.stop.state}",
            "lat": s.lat,
            "lon": s.lon,
            "mile_marker": round(s.mile_marker, 1),
            "price_per_gallon": round(s.stop.price, 3),
            "gallons": s.gallons,
            "cost": s.cost,
        }
        for s in planned
    ]
    response["fuel"] = {
        "total_gallons": round(sum(s.gallons for s in planned), 2),
        "total_cost": total_cost,
    }
    response["map_url"] = _google_maps_url(start_q, finish_q, planned)
    response["meta"] = _meta(
        started,
        geocoded_stops=min(len(kept), _MAX_GEOCODE_STOPS),
        routing_calls=routing_calls,
        candidates_capped=len(kept) > _MAX_GEOCODE_STOPS,
    )
    return JsonResponse(response)


def _abbr_for_state(state_name: str) -> str | None:
    for abbr, name in STATE_ABBR_TO_NAME.items():
        if name.lower() == state_name.lower():
            return abbr
    return None


def _google_maps_url(start_q: str, finish_q: str, planned) -> str:
    """Human-viewable map of the route with the chosen fuel stops as waypoints."""
    from urllib.parse import quote

    waypoints = "|".join(f"{s.lat},{s.lon}" for s in planned[:9])  # Maps allows <=9 waypoints
    url = f"https://www.google.com/maps/dir/?api=1&origin={quote(start_q)}&destination={quote(finish_q)}"
    if waypoints:
        url += f"&waypoints={quote(waypoints)}"
    return url


def _meta(started: float, geocoded_stops: int, routing_calls: int = 1, candidates_capped: bool = False) -> dict:
    return {
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "routing_api_calls": routing_calls,  # real HTTP calls; 0 when served from cache
        "stops_precisely_geocoded": geocoded_stops,
        "candidates_capped": candidates_capped,  # True if the safety bound dropped candidates
        "geocoding": "served from persistent cache when warm",
    }
