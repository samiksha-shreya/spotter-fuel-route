from django.urls import path

from fuelroute.api.views import health, route_fuel

urlpatterns = [
    path("api/health/", health, name="health"),
    path("api/route-with-fuel/", route_fuel, name="route-fuel"),
]
