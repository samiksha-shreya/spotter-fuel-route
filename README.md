# Fuel Route Planner

Django API that plans the cheapest fuel stops for a US road trip.

Given a start and end point anywhere in the USA, it returns the driving
route (as GeoJSON, ready to drop on a map), the most cost-effective fuel
stops from the supplied truckstop price file, and the total fuel cost.
Vehicle assumptions: 500-mile range, 10 mpg, starts with a full tank.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py runserver
```

Requires Python 3.10+. No database migrations, no external services to
install - the only state is a self-created SQLite geocode cache in `data/`.

## Usage

```bash
curl -X POST http://127.0.0.1:8000/api/route-with-fuel/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Dallas, TX", "finish": "Atlanta, GA"}'
```

Response (trimmed):

```json
{
  "route": {"distance_miles": 782.9, "duration_hours": 11.4, "geometry": {"type": "LineString", "coordinates": "..."}},
  "fuel_stops": [
    {"name": "...", "address": "...", "mile_marker": 243.1,
     "price_per_gallon": 2.859, "gallons": 21.3, "cost": 60.9}
  ],
  "fuel": {"total_gallons": 78.3, "total_cost": 224.61},
  "map_url": "https://www.google.com/maps/dir/?api=1&...",
  "vehicle": {"range_miles": 500, "mpg": 10}
}
```

`geometry` is a standard GeoJSON LineString: paste it into any map viewer,
or open `map_url` to see the route and chosen stops in Google Maps.

Health check: `GET /api/health/`.

## Design decisions

- **One routing call per route.** OSRM answers with the full polyline and
  per-step highway refs; both are cached. Repeat requests for the same
  endpoints make zero routing calls.
- **Lazy geocoding with a permanent cache.** The price CSV has no
  coordinates, and geocoding all 8,151 stops up front would take hours on a
  rate-limited public API. Instead only stops near the actual route are
  geocoded, and every geocode result is cached forever in SQLite
  (`data/geocode_cache.sqlite3`), so the second request for a corridor is
  instant. See ARCHITECTURE.md for the corridor-detection pipeline.
- **Provider fallback.** Nominatim is the primary geocoder; Photon takes
  over automatically when Nominatim throttles. `NOMINATIM_URL` /
  `PHOTON_URL` env vars allow self-hosted instances.
- **Provably optimal refuelling.** The greedy "buy just enough to reach
  cheaper fuel, otherwise fill up" rule is validated in tests against an
  exact linear-programming optimum on randomized inputs.

## Tests

```bash
python manage.py test
# or without Django's runner:
python -m unittest discover -s tests
```

## Warming the cache before a demo

The first request on a brand-new corridor pays the real geocoding cost
(rate-limited, a few minutes). To pre-warm a state's stops:

```bash
python manage.py warm_geocache TX LA MS AL GA
```

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `FUEL_PRICE_CSV` | `data/fuel-prices-for-be-assessment.csv` | truckstop price file |
| `GEOCODE_CACHE_PATH` | `data/geocode_cache.sqlite3` | permanent geocode cache |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | primary geocoder |
| `PHOTON_URL` | `https://photon.komoot.io` | fallback geocoder |
| `OSRM_URL` | `https://router.project-osrm.org` | routing engine |
| `HTTP_USER_AGENT` | project UA | sent to all external APIs |
