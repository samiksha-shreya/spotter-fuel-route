# Architecture

## Problem

Given a start and end point in the USA, return the driving route on a map and
the most cost-effective set of fuel stops along it. Vehicle: 500-mile range,
10 mpg, starts with a full tank. Fuel prices come from a supplied CSV of
truckstops. Keep third-party routing API calls to a minimum.

## Design in one paragraph

One POST endpoint orchestrates four small services in a straight line:
geocode the two endpoints, get the route (one routing call, cached), find
which truckstops from the CSV sit on that route (corridor detection), then
compute the cheapest refuelling plan with a greedy that is provably optimal
for this cost model. No database schema of our own, no background workers,
no async jobs: the only state is a SQLite file used as a permanent geocode
cache, because geocoding is the slow, rate-limited dependency and its
answers never change.

## Components

- `fuelroute/geo.py` - pure geometry: haversine distances, cumulative mile
  markers along the polyline, point-to-polyline distance. No I/O, fully
  testable.
- `fuelroute/services/routing.py` - OSRM client. Exactly one HTTP call per
  unique (start, finish) pair, cached in-process for an hour. The same call
  also returns per-step highway refs (`I 20`, `US 69`), which we reuse for
  corridor detection instead of paying for extra API calls.
- `fuelroute/services/geocoding.py` - geocoding with two providers
  (Nominatim primary, Photon fallback when Nominatim throttles) behind one
  interface. Every lookup is cached forever in SQLite, so a second request
  for the same corridor costs zero network calls. Public Nominatim allows
  ~1 request/second; the client self-throttles and backs off on 429.
- `fuelroute/services/fuel.py` - the data model (`FuelStop`), CSV loader,
  corridor candidate selection, and the refuelling optimizer.
- `fuelroute/api/views.py` - thin HTTP layer: validate input, call the
  services in order, shape the JSON. All errors surface as clean 4xx/502
  responses, never tracebacks.

## Corridor detection (the interesting part)

The CSV has 8,151 truckstops but no coordinates. Geocoding all of them on
every request is impossible under a 1 req/s budget, so we only geocode
stops that could plausibly be on the route:

1. Reverse-geocode probe points along the route polyline to learn which
   states the route crosses (~one probe per 75 miles).
2. A stop becomes a candidate if its address mentions a highway the route
   actually uses (matched against the OSRM step refs, e.g. `I-20`), or its
   city matches a locality reported by the probes.
3. Cheap prefilter: geocode each candidate's city center once and drop
   stops whose town sits more than 12 miles off the route.
4. Geocode the survivors precisely (address, then business name, then
   road-junction variants) and keep those within 3 miles of the polyline.

Result: a transcontinental route geocodes a few hundred places exactly once,
and every repeat request is served entirely from cache.

## Refuelling optimizer

At each stop the rule is: if a cheaper stop is reachable on a full tank,
buy just enough fuel to reach the first such stop; otherwise this is the
cheapest fuel around, so fill the tank. The finish line is treated as a
free stop; the starting full tank as a stop with price 0. Correctness is
not asserted by inspection: the test suite checks the greedy against an
exact LP optimum (scipy) on 60 randomized route/stop configurations, plus
hand-built cases for the classic traps (skip the expensive early stop,
infeasible gap, no stops needed).

## Failure modes

- Ungeocodable endpoint: 422 with a clear message.
- Routing/geocoding provider down: 502 after retries and provider failover.
- Fuel gap larger than the vehicle range: 422 explaining where.
- Route under 500 miles: no stops, zero cost, stated explicitly.
