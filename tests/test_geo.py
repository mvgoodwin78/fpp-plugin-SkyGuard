import unittest

from skyguard.geo import from_nm, haversine_nm, to_nm


class DistanceTests(unittest.TestCase):
    def test_same_point_is_zero(self):
        self.assertEqual(haversine_nm(40.0, -75.0, 40.0, -75.0), 0.0)

    def test_one_degree_of_latitude_is_about_60_nm(self):
        self.assertAlmostEqual(haversine_nm(40.0, -75.0, 41.0, -75.0), 60.04, places=1)

    def test_longitude_degrees_shrink_toward_the_poles(self):
        at_equator = haversine_nm(0.0, 0.0, 0.0, 1.0)
        at_60_north = haversine_nm(60.0, 0.0, 60.0, 1.0)
        self.assertAlmostEqual(at_60_north, at_equator / 2, places=1)

    def test_known_city_pair(self):
        # JFK to LHR is about 2,990 nm.
        self.assertAlmostEqual(haversine_nm(40.6413, -73.7781, 51.47, -0.4543), 2990, delta=10)

    def test_crossing_the_antimeridian_takes_the_short_way(self):
        self.assertAlmostEqual(haversine_nm(0.0, 179.5, 0.0, -179.5), 60.04, places=1)

    def test_antipodal_points_do_not_raise(self):
        self.assertAlmostEqual(haversine_nm(0.0, 0.0, 0.0, 180.0), 10807, delta=1)


class UnitTests(unittest.TestCase):
    def test_conversions(self):
        self.assertAlmostEqual(to_nm(1.0, "nm"), 1.0)
        self.assertAlmostEqual(to_nm(1.852, "km"), 1.0)
        self.assertAlmostEqual(to_nm(1.15078, "mi"), 1.0, places=4)

    def test_round_trip(self):
        for unit in ("mi", "nm", "km"):
            self.assertAlmostEqual(from_nm(to_nm(12.5, unit), unit), 12.5)

    def test_unknown_unit_is_rejected(self):
        with self.assertRaises(KeyError):
            to_nm(1.0, "furlongs")


if __name__ == "__main__":
    unittest.main()
