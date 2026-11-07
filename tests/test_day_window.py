"""
Tests for the 24-hour instant odds-move window in scripts/movers_alerts.py
(runs alongside the 6-hour window, same per-category thresholds, one cooldown).

Run:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from test_alert_kinds import Harness, mk, ts  # noqa: E402

Q = "Will Denver Nuggets win the 2027 NBA Finals?"   # futures: 5pt instant threshold
TITLE = "NBA: 2027 Champion"


def hist(points):
    """[(hours_ago, prob)] -> history snapshots."""
    return [{"timestamp": ts(h), "impliedProbability": p} for h, p in points]


class DayWindowTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def run_one(self, p, points):
        return self.h.run([mk("0x1", Q, p, neg=False, title=TITLE)], {"0x1": hist(points)})

    def test_slow_24h_drift_with_flat_6h_sends_one(self):
        # 10% -> 16% over a day, flat (16%) for the last 6h: only the 24h window clears 5pt
        posted = self.run_one(0.16, [(25, 0.10), (24, 0.10), (18, 0.12), (12, 0.14),
                                     (7, 0.16), (6, 0.16), (0, 0.16)])
        self.assertEqual(len(posted), 1)
        first, url = posted[0].split("\n")
        self.assertIn("10.0% to 16.0%", first)
        self.assertRegex(first, r"in the past day|over the past 24 hours")
        self.assertNotIn("6 hours", first)
        self.assertEqual(url, "https://hoopsmatic.com/polymarket/market/"
                              "will-denver-nuggets-win-the-2027-nba-finals/")

    def test_both_windows_over_threshold_sends_one_with_larger_move(self):
        # 24h: 5% -> 17% (12pt); 6h: 10% -> 17% (7pt). Both clear 5pt -> one line, 24h.
        posted = self.run_one(0.17, [(25, 0.05), (24, 0.05), (7, 0.10), (6, 0.10), (0, 0.17)])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0].count("hoopsmatic.com"), 1)
        self.assertIn("5.0% to 17.0%", posted[0])
        self.assertRegex(posted[0], r"in the past day|over the past 24 hours")

    def test_six_hour_move_larger_uses_six_hour_wording(self):
        # 24h: 12% -> 20% (8pt); 6h: 10% -> 20% (10pt) -> 6h wins
        posted = self.run_one(0.20, [(25, 0.12), (24, 0.12), (7, 0.10), (6, 0.10), (0, 0.20)])
        self.assertEqual(len(posted), 1)
        self.assertIn("10.0% to 20.0%", posted[0])
        self.assertRegex(posted[0], r"(in the past|over the last) 6 hours")

    def test_cooldown_hit_sends_none(self):
        # Run 1: a 6h move alerts. Run 2: the same move is still inside the 24h
        # window (6h now flat) -> the shared cooldown blocks a second post.
        self.assertEqual(len(self.run_one(0.17, [(25, 0.10), (24, 0.10), (7, 0.10),
                                                 (6, 0.10), (0, 0.17)])), 1)
        self.assertEqual(self.run_one(0.17, [(25, 0.10), (24, 0.10), (7, 0.17),
                                             (6, 0.17), (0, 0.17)]), [])

    def test_young_market_has_no_24h_window(self):
        # only 10h of history: no "past day" claim, and the flat 6h sends nothing
        self.assertEqual(self.run_one(0.16, [(10, 0.10), (7, 0.16), (6, 0.16), (0, 0.16)]), [])


if __name__ == "__main__":
    unittest.main()
