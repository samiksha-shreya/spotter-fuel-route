"""Fuel-price data loading and the minimum-cost refuelling optimizer.

The OPIS CSV ships without coordinates, so stop locations are geocoded lazily
(and cached) - only for stops in cities the route actually passes through.
"""
from __future__ import annotations

import csv
import heapq
import re
import threading
from dataclasses import dataclass, field

from django.conf import settings

_state_abbr_to_name = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def normalize_city(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


@dataclass(frozen=True)
class FuelStop:
    opis_id: str
    name: str
    address: str
    city: str
    state: str  # two-letter code
    price: float  # USD per gallon


@dataclass
class PlannedStop:
    stop: FuelStop
    lat: float
    lon: float
    mile_marker: float
    gallons: float = 0.0
    cost: float = 0.0


_store_lock = threading.Lock()
_stops_by_state: dict[str, list[FuelStop]] | None = None


def load_stops() -> dict[str, list[FuelStop]]:
    """Load the OPIS CSV once into memory, indexed by state code."""
    global _stops_by_state
    with _store_lock:
        if _stops_by_state is not None:
            return _stops_by_state
        by_state: dict[str, list[FuelStop]] = {}
        with open(settings.FUEL_PRICE_CSV, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                try:
                    price = float(row["Retail Price"])
                except (KeyError, ValueError):
                    continue
                state = (row.get("State") or "").strip().upper()
                stop = FuelStop(
                    opis_id=(row.get("OPIS Truckstop ID") or "").strip(),
                    name=(row.get("Truckstop Name") or "").strip(),
                    address=(row.get("Address") or "").strip(),
                    city=(row.get("City") or "").strip(),
                    state=state,
                    price=price,
                )
                by_state.setdefault(state, []).append(stop)
        _stops_by_state = by_state
        return by_state


_ADDR_HWY_RE = re.compile(
    r"\b(INTERSTATE|INT|IH|I|US|FM|RM|SH|SR|CR|HWY|HIGHWAY|STATE)\s*[-]?\s*(\d{1,3}[A-Z]?)\b", re.I
)


def highway_tokens(text: str) -> set[str]:
    """Normalized highway tokens mentioned in a free-text address."""
    out: set[str] = set()
    for cls, num in _ADDR_HWY_RE.findall(text.upper()):
        cls = {"INTERSTATE": "I", "INT": "I", "IH": "I", "HWY": "SH",
               "HIGHWAY": "SH", "SR": "SH", "STATE": "SH"}.get(cls, cls)
        out.add(f"{cls}{num}")
    return out


def geocode_stop(geocoder, stop: FuelStop) -> tuple[float, float] | None:
    """Best-effort coordinates for a truckstop, trying several query shapes.

    OPIS addresses are often highway exits ('I-20, EXIT 283') which geocode
    poorly, so fall back to the business name, then the bare road junction.
    """
    state_name = _state_abbr_to_name.get(stop.state, stop.state)
    queries = [
        f"{stop.address}, {stop.city}, {stop.state}, USA",
        f"{stop.name}, {stop.city}, {stop.state}, USA",
        f"{stop.address}, {stop.city}, {state_name}, USA",
    ]
    for q in queries:
        point = geocoder.forward(query=q)
        if point is not None:
            return point
    return None


def candidate_stops(state_codes: set[str], city_names: set[str], highways: set[str] | None = None) -> list[FuelStop]:
    """Truckstops worth geocoding: in a traversed state AND a traversed city.

    A stop is a candidate when it sits on a highway the route actually uses
    (address text match against the OSRM step refs) or in a locality the
    reverse-geocoded route probes reported. This keeps the number of
    rate-limited geocoding calls small instead of geocoding whole states
    (Texas alone has ~800 stops in the file).
    """
    by_state = load_stops()
    normalized_cities = {normalize_city(c) for c in city_names if c}
    out: list[FuelStop] = []
    for code in state_codes:
        for stop in by_state.get(code, []):
            if normalize_city(stop.city) in normalized_cities:
                out.append(stop)
            elif highways and highway_tokens(stop.address) & highways:
                out.append(stop)
    return out


def plan_refuelling(
    stops: list[PlannedStop],
    route_distance_miles: float,
    range_miles: float = settings.VEHICLE_RANGE_MILES,
    mpg: float = settings.VEHICLE_MPG,
) -> tuple[list[PlannedStop], float]:
    """Minimum-cost refuelling plan along a one-dimensional route.

    The vehicle starts at mile 0 with a full tank (capacity = range/mpg
    gallons) and must reach the finish. At each stop we apply the classic
    minimum-cost rule:

      - if a cheaper stop is reachable on a full tank, buy just enough fuel
        here to reach the first such stop (never pay more than necessary);
      - otherwise this is the cheapest fuel around: fill the tank and drive
        to the cheapest reachable stop.

    The start point is modelled as a stop with price 0 and an already-full
    tank, which folds the initial fuel into the same rule.

    Returns (stops with purchases, in route order, total_cost).
    Raises ValueError when the route is infeasible (fuel gap > range).
    """
    capacity = range_miles / mpg

    # Collapse stops sharing a mile marker, keeping the cheapest.
    by_mile: dict[float, PlannedStop] = {}
    for s in stops:
        if s.mile_marker <= 0 or s.mile_marker >= route_distance_miles:
            continue  # no point fuelling at the origin or the finish line
        existing = by_mile.get(s.mile_marker)
        if existing is None or s.stop.price < existing.stop.price:
            by_mile[s.mile_marker] = s
    pts = sorted(by_mile.values(), key=lambda s: s.mile_marker)

    purchases: dict[float, tuple[PlannedStop, float]] = {}  # mile -> (stop, gallons)
    pos = 0.0          # current mile
    tank = capacity    # gallons on board
    cur_price = 0.0    # effective price of fuel available at current point (start fuel is free)

    while True:
        remaining_gal = (route_distance_miles - pos) / mpg
        if remaining_gal <= tank + 1e-9:
            break  # finish reachable on current tank

        # Finish within a full tank from here. Top up at this stop only if it
        # is the cheapest fuel we can still reach; if a cheaper stop is
        # within range, fall through and buy just enough to reach it instead.
        if route_distance_miles - pos <= range_miles + 1e-9 and pos > 0:
            stop_here = by_mile.get(pos)
            window = [s for s in pts if pos + 1e-9 < s.mile_marker <= pos + range_miles + 1e-9]
            cheaper_ahead = any(s.stop.price < cur_price - 1e-12 for s in window)
            if stop_here is not None and not cheaper_ahead:
                buy = remaining_gal - tank
                prev = purchases.get(pos, (stop_here, 0.0))[1]
                purchases[pos] = (stop_here, prev + buy)
                break

        # Stops reachable if we fill to capacity here.
        window = [s for s in pts if pos + 1e-9 < s.mile_marker <= pos + range_miles + 1e-9]
        if not window:
            raise ValueError(
                f"No fuel stop within {range_miles:.0f} miles of mile {pos:.0f}; route infeasible with the given range."
            )

        first_cheaper = next((s for s in window if s.stop.price < cur_price), None)
        cheapest = min(window, key=lambda s: s.stop.price)

        if first_cheaper is not None:
            # Buy only what is needed to reach the cheaper fuel.
            target = first_cheaper
            need_gal = (target.mile_marker - pos) / mpg
            buy = max(0.0, need_gal - tank)
        else:
            # Cheapest fuel around is here: fill up and head to the cheapest stop.
            target = cheapest
            buy = capacity - tank

        if buy > 1e-9 and pos > 0:  # pos == 0 is the free starting tank, not a purchase
            stop_at_pos = by_mile.get(pos)
            if stop_at_pos is not None:
                prev = purchases.get(pos, (stop_at_pos, 0.0))[1]
                purchases[pos] = (stop_at_pos, prev + buy)

        # drive to target
        tank = tank + buy - (target.mile_marker - pos) / mpg
        pos = target.mile_marker
        cur_price = target.stop.price

        if tank < -1e-9:
            raise ValueError("Internal error: negative tank - route infeasible.")

    planned = []
    total = 0.0
    for mile, (stop, gallons) in purchases.items():
        stop.gallons = round(gallons, 2)
        stop.cost = round(gallons * stop.stop.price, 2)
        total += stop.cost
        planned.append(stop)
    planned.sort(key=lambda s: s.mile_marker)
    return planned, round(total, 2)
