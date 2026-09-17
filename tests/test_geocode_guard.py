"""Fuzzy-match guard on the primary geocoder path."""
import unittest

from fuelroute.services.geocoding import _nominatim_match_ok


class NominatimMatchGuardTest(unittest.TestCase):
    def test_garbage_rejected(self):
        res = {"display_name": "Santa Cruz County, California, United States",
               "importance": 0.31}
        self.assertFalse(_nominatim_match_ok(res, "asdkfjhqwekj zzz, usa"))

    def test_typo_accepted(self):
        res = {"display_name": "Seattle, King County, Washington, United States",
               "importance": 0.7}
        self.assertTrue(_nominatim_match_ok(res, "Seattel, WA, USA"))

    def test_normal_city_accepted(self):
        res = {"display_name": "Chicago, Cook County, Illinois, United States",
               "importance": 0.65}
        self.assertTrue(_nominatim_match_ok(res, "Chicago, IL, USA"))

    def test_single_token_accepted(self):
        res = {"display_name": "Springfield, Illinois, United States",
               "importance": 0.6}
        self.assertTrue(_nominatim_match_ok(res, "Springfield"))

    def test_high_importance_accepted(self):
        res = {"display_name": "New York, United States", "importance": 0.72}
        self.assertTrue(_nominatim_match_ok(res, "new york"))

    def test_low_importance_no_overlap_rejected(self):
        res = {"display_name": "Some Random Barn, Iowa, United States",
               "importance": 0.2}
        self.assertFalse(_nominatim_match_ok(res, "Denver, Colorado"))


if __name__ == "__main__":
    unittest.main()
