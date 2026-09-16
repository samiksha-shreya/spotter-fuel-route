"""Corridor pipeline tests: highway normalization, candidate selection,
point-to-polyline geometry, and the HTTP view with stubbed services."""
import json
import math
import unittest
from unittest import mock

from fuelroute.geo import distance_to_polyline_miles, mile_marker_for_point, cumulative_distances
from fuelroute.services import fuel
from fuelroute.services.fuel import FuelStop, candidate_stops, highway_tokens
from fuelroute.services.routing import normalize_highway


class NormalizeHighwayTest(unittest.TestCase):
    CASES = {
        "I-20": "I20", "I 20": "I20", "IH 20": "I20", "Interstate 20": "I20",
        "I-35W": "I35W", "US 69": "US69", "US-287": "US287",
        "Historic US 66": "US66",  # the two normalizers must agree
        "FM 1709": "FM1709", "TX 31": "SH31", "Highway 5": "SH5",
        "I-20 BUS": "I20", "Main Street": None, "": None,
    }

    def test_table(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_highway(raw), expected)

    def test_agrees_with_address_tokenizer(self):
        for raw in ("Historic US 66", "I-20", "US 69"):
            self.assertIn(normalize_highway(raw), highway_tokens(raw))


class HighwayTokensTest(unittest.TestCase):
    def test_address_tokens(self):
        self.assertEqual(highway_tokens("I-20 & US-69, EXIT 283"), {"I20", "US69"})
        self.assertEqual(highway_tokens("1200 Highway 121"), {"SH121"})
        self.assertEqual(highway_tokens("FM 1709"), {"FM1709"})
        self.assertEqual(highway_tokens("123 Main St"), set())


def _stop(city, state, address, price=3.0):
    return FuelStop(opis_id=f"{city}-{state}", name="TA", address=address,
                    city=city, state=state, price=price)


class CandidateStopsTest(unittest.TestCase):
    def setUp(self):
        self._saved = fuel._stops_by_state
        fuel._stops_by_state = {
            "TX": [_stop("Dallas", "TX", "I-20"), _stop("Nowhere", "TX", "CR 5")],
            "MS": [_stop("Vicksburg", "MS", "I-20 Frontage Rd")],  # state missed by probes
            "CA": [_stop("Blythe", "CA", "I-10"), _stop("Fresno", "CA", "SH 99")],
        }

    def tearDown(self):
        fuel._stops_by_state = self._saved

    def test_city_match_within_state(self):
        out = candidate_stops({"TX"}, {"Dallas"}, set())
        self.assertEqual([s.city for s in out], ["Dallas"])

    def test_highway_match_within_state(self):
        out = candidate_stops({"CA"}, set(), {"I10"})
        self.assertEqual([s.city for s in out], ["Blythe"])

    def test_interstate_matches_across_undetected_state(self):
        # Mississippi never appeared in the probes, but I-20 runs through it.
        out = candidate_stops({"TX"}, {"Dallas"}, {"I20"})
        self.assertIn(("Vicksburg", "MS"), {(s.city, s.state) for s in out})

    def test_state_highway_does_not_leak_across_states(self):
        out = candidate_stops({"TX"}, set(), {"SH99"})
        self.assertEqual(out, [])


class GeometryTest(unittest.TestCase):
    # A 10-mile straight east-west segment with a point 1 mile off its midpoint.
    LINE = [(32.0, -97.0), (32.0, -96.8555)]
    MID_OFF = (32.01446, -96.92775)  # ~1.0 mi north of the midpoint

    def test_segment_projection_not_vertex(self):
        d = distance_to_polyline_miles(self.MID_OFF, self.LINE)
        self.assertLess(d, 1.2)   # vertex-only distance here is ~5 miles
        self.assertGreater(d, 0.8)

    def test_mile_marker_interpolates(self):
        markers = cumulative_distances(self.LINE)
        mile = mile_marker_for_point(self.MID_OFF, self.LINE, markers)
        self.assertAlmostEqual(mile, markers[-1] / 2, delta=0.3)


class ViewValidationTest(unittest.TestCase):
    def _post(self, body):
        from django.test import Client
        return Client().post(
            "/api/route-with-fuel/", data=body, content_type="application/json"
        )

    def test_non_object_json_400(self):
        self.assertEqual(self._post("[1, 2]").status_code, 400)

    def test_numeric_fields_400(self):
        self.assertEqual(self._post('{"start": 123, "finish": "x"}').status_code, 400)

    def test_malformed_json_400(self):
        self.assertEqual(self._post("{bad").status_code, 400)

    def test_missing_field_400(self):
        self.assertEqual(self._post('{"start": "Dallas, TX"}').status_code, 400)


class ViewE2EStubbedTest(unittest.TestCase):
    """Full view path with the geocoder and router stubbed out."""

    GEOMETRY = [(32.0, -97.0), (32.0, -96.5), (32.0, -96.0)]  # ~57 mi east

    def _geocoder(self):
        g = mock.Mock()
        g.forward.side_effect = lambda query="", **kw: {
            "startville": (32.0, -97.0), "endville": (32.0, -96.0),
        }.get(query.split(",")[0].lower(), (32.0, -96.7))
        g.reverse.return_value = {"state": "Texas", "city": "Midville"}
        return g

    def test_route_with_fuel_200(self):
        from django.test import Client
        route = {
            "geometry": self.GEOMETRY,
            "distance_miles": 57.0,
            "duration_seconds": 3600,
            "highways": {"I20"},
        }
        with mock.patch("fuelroute.api.views.get_geocoder", return_value=self._geocoder()), \
             mock.patch("fuelroute.api.views.get_route", return_value=route):
            resp = Client().post(
                "/api/route-with-fuel/",
                data=json.dumps({"start": "Startville", "finish": "Endville"}),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["fuel_stops"], [])  # under one tank of range
        self.assertEqual(body["meta"]["routing_api_calls"], 0)


if __name__ == "__main__":
    unittest.main()
