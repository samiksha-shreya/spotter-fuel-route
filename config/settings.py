"""Minimal Django settings for the fuel-route API service.

Stateless JSON API: no database models, no sessions, no admin.
Geocode results are cached in a plain SQLite file (see fuelroute.services.geocoding).
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

import os

SECRET_KEY = os.environ.get("SECRET_KEY", "assessment-local-only-not-a-secret")
DEBUG = os.environ.get("DEBUG", "") == "1"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "fuelroute",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Service configuration (overridable via environment)

FUEL_PRICE_CSV = os.environ.get(
    "FUEL_PRICE_CSV", str(BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv")
)
GEOCODE_CACHE_PATH = os.environ.get(
    "GEOCODE_CACHE_PATH", str(BASE_DIR / "data" / "geocode_cache.sqlite3")
)
# Public instances are rate limited (~1 req/s). Point these at a self-hosted
# Nominatim / OSRM deployment for higher throughput - both are drop-in.
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
PHOTON_URL = os.environ.get("PHOTON_URL", "https://photon.komoot.io")
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org")
# Nominatim usage policy requires a descriptive User-Agent.
HTTP_USER_AGENT = os.environ.get(
    "HTTP_USER_AGENT", "spotter-fuel-route-assessment/1.0"
)  # Nominatim policy asks for a descriptive UA; set contact info via env in real deployments.

VEHICLE_RANGE_MILES = 500.0
VEHICLE_MPG = 10.0
