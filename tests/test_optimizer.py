"""Optimizer correctness: brute-force DP cross-check on randomized cases."""
import itertools
import random
import unittest

from fuelroute.services.fuel import FuelStop, PlannedStop, plan_refuelling

MPG = 10.0
RANGE = 500.0
CAP = RANGE / MPG  # 50 gal


def brute_force(stops, distance):
    """Exact optimum via LP. Variables: gallons bought at each stop.
    Minimize sum(b_i * p_i) s.t. tank never negative, never above capacity,
    finish reached. Tank after stop k = CAP - (mile_k)/MPG + sum_{i<=k} b_i
    (fuel purchased is usable immediately; fuel before stop k consumed)."""
    import numpy as np
    from scipy.optimize import linprog

    pts = sorted({s.mile_marker: s for s in stops if 0 < s.mile_marker < distance}.values(),
                 key=lambda s: s.mile_marker)
    positions = [0.0] + [s.mile_marker for s in pts] + [distance]
    n_stops = len(pts)
    for i in range(1, len(positions)):
        if positions[i] - positions[i - 1] > RANGE + 1e-9:
            return None
    c = np.array([s.stop.price for s in pts])
    # tank arriving at position j (before optional purchase) =
    #   CAP + sum_{i: mile_i < pos_j} b_i - pos_j / MPG   >= 0
    A_ub, b_ub = [], []
    for j in range(1, len(positions)):
        row = [0.0] * n_stops
        for i in range(n_stops):
            if pts[i].mile_marker < positions[j] - 1e-12:
                row[i] = -1.0
        A_ub.append(row)
        b_ub.append(CAP - positions[j] / MPG)
    # tank capacity: fuel on board right after leaving stop k <= CAP
    #   CAP + sum_{i<=k} b_i - mile_k / MPG <= CAP  ->  sum_{i<=k} b_i <= mile_k / MPG
    for k in range(n_stops):
        row = [1.0 if i <= k else 0.0 for i in range(n_stops)]
        A_ub.append(row)
        b_ub.append(pts[k].mile_marker / MPG)
    # must finish: CAP + sum b_i - distance / MPG >= 0
    A_ub.append([-1.0] * n_stops)
    b_ub.append(CAP - distance / MPG)
    res = linprog(c, A_ub=np.array(A_ub), b_ub=np.array(b_ub), bounds=(0, None), method="highs")
    if res.status != 0:
        return None
    return float(res.fun)


def make_stop(mile, price, idx):
    return PlannedStop(
        stop=FuelStop(str(idx), f"Stop {idx}", "1 Main St", "Town", "TX", price),
        lat=0.0, lon=0.0, mile_marker=mile,
    )


class OptimizerTest(unittest.TestCase):
    def test_no_stops_needed_short_route(self):
        planned, cost = plan_refuelling([], 300.0, RANGE, MPG)
        self.assertEqual(planned, [])
        self.assertEqual(cost, 0.0)

    def test_single_stop_fill(self):
        stops = [make_stop(200, 3.0, 1)]
        planned, cost = plan_refuelling(stops, 700.0, RANGE, MPG)
        # 700 miles = 70 gal; start has 50; buy 20 at the only stop
        self.assertEqual(len(planned), 1)
        self.assertAlmostEqual(planned[0].gallons, 20.0, places=1)
        self.assertAlmostEqual(cost, 60.0, places=1)

    def test_prefers_cheaper_stop(self):
        # expensive stop first, cheap stop later within range
        stops = [make_stop(100, 5.0, 1), make_stop(300, 3.0, 2)]
        planned, cost = plan_refuelling(stops, 800.0, RANGE, MPG)
        # optimal: start fuel covers 300 mi, buy all 30 remaining gallons at
        # the CHEAP stop (mile 300) - the expensive stop at mile 100 is skipped
        total = sum(p.gallons for p in planned)
        self.assertAlmostEqual(total, 30.0, places=1)  # 80 needed - 50 free
        self.assertAlmostEqual(cost, 30 * 3.0, places=0)

    def test_infeasible_gap_raises(self):
        stops = [make_stop(100, 3.0, 1)]
        with self.assertRaises(ValueError):
            plan_refuelling(stops, 1200.0, RANGE, MPG)

    def test_randomized_against_brute_force(self):
        rng = random.Random(42)
        for trial in range(60):
            distance = rng.uniform(600, 2000)
            n_stops = rng.randint(2, 8)
            miles = sorted(rng.uniform(50, distance - 50) for _ in range(n_stops))
            # keep gaps feasible
            if any(miles[i + 1] - miles[i] > RANGE for i in range(len(miles) - 1)):
                continue
            if miles[0] > RANGE or distance - miles[-1] > RANGE:
                continue
            stops = [make_stop(m, rng.uniform(2.5, 6.0), i) for i, m in enumerate(miles)]
            planned, cost = plan_refuelling(stops, distance, RANGE, MPG)
            expected = brute_force(stops, distance)
            self.assertIsNotNone(expected)
            # Displayed gallons are rounded UP per stop so the published plan
            # stays feasible; that adds <= 0.01 gal * price per stop over the
            # exact LP optimum.
            delta = 0.05 + 0.01 * 6.0 * max(1, len(planned))
            self.assertAlmostEqual(cost, round(expected, 2), delta=delta,
                                   msg=f"trial {trial}: got {cost}, LP optimum {expected}")


if __name__ == "__main__":
    unittest.main()


class AdjacentMergeTest(unittest.TestCase):
    """Adjacent same-price purchase rows fold into one fuelling event."""

    def _stop(self, name, mile, price):
        fs = FuelStop(name, name, "", "C", "TX", price)
        return PlannedStop(stop=fs, lat=32.0, lon=-97.0, mile_marker=mile)

    def _stops(self, with_adjacent):
        stops = [
            self._stop("A", 400.0, 3.00),
            self._stop("C", 850.0, 3.00),
            self._stop("D", 1300.0, 3.00),
        ]
        if with_adjacent:
            stops.insert(1, self._stop("B", 400.1, 3.00))  # dust splash next to A
        return stops

    def test_adjacent_same_price_rows_merge(self):
        # 1400 mi route, 500 mi range: retargeting past B leaves a 0.01-gal
        # micro-purchase at B, which must fold into A's row.
        planned, total = plan_refuelling(self._stops(True), 1400.0, range_miles=500.0, mpg=10.0)
        self.assertEqual([s.stop.name for s in planned], ["A", "C", "D"])
        self.assertAlmostEqual(planned[0].gallons, 40.0, places=1)
        self.assertAlmostEqual(sum(s.gallons for s in planned), 90.0, places=1)

    def test_distant_same_price_rows_stay_separate(self):
        planned, _ = plan_refuelling(self._stops(False), 1400.0, range_miles=500.0, mpg=10.0)
        self.assertEqual([s.stop.name for s in planned], ["A", "C", "D"])
