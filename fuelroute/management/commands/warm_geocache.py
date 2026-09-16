"""Pre-geocode truckstops for given state codes into the persistent cache.

Usage: python manage.py warm_geocache TX OK MO
Handy before a demo so the first API call is fast on the public Nominatim
instance (~1 request/second rate limit).
"""
from django.core.management.base import BaseCommand

from fuelroute.services.fuel import geocode_stop, load_stops
from fuelroute.services.geocoding import get_geocoder


class Command(BaseCommand):
    help = "Pre-geocode fuel stops for the given state codes into the cache."

    def add_arguments(self, parser):
        parser.add_argument("states", nargs="+", help="Two-letter state codes, e.g. TX OK")

    def handle(self, *args, **options):
        geocoder = get_geocoder()
        by_state = load_stops()
        for code in options["states"]:
            stops = by_state.get(code.upper(), [])
            self.stdout.write(f"{code.upper()}: geocoding {len(stops)} stops (rate limited)...")
            hits = 0
            for stop in stops:
                point = geocode_stop(geocoder, stop)
                if point:
                    hits += 1
            self.stdout.write(self.style.SUCCESS(f"{code.upper()}: {hits}/{len(stops)} geocoded"))
