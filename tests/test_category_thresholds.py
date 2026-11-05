"""
Tests for per-category alert thresholds in scripts/movers_alerts.py.

Run:  python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import movers_alerts as ma  # noqa: E402
from test_alert_kinds import Harness, mk, ts  # noqa: E402

AWARDS = ["NBA: 2026-27 MVP", "NBA: 2026-27 Rookie of the Year",
          "NBA: 2026-27 Defensive Player of the Year", "NBA: 2026-27 Sixth Man of the Year",
          "NBA: 2026-27 Most Improved Player", "NBA: 2026-27 Coach of the Year",
          "NBA: 2026-27 Clutch Player of the Year", "NBA: 2026 NBA Cup MVP",
          # stat leaders
          "NBA: 2026-27 Points Per Game Leader", "NBA: 2026-27 Rebounds Per Game Leader",
          "NBA: 2026-27 Assists Per Game Leader", "NBA: 2026-27 Steals Per Game Leader",
          "NBA: 2026-27 Blocks Per Game Leader",
          "NBA: 2026-27 Three-Pointers Made Per Game Leader",
          "NBA: 2026-27 Efficiency (EFF) Leader",
          "NBA: 2026-27 Player Efficiency Rating (PER) Leader",
          # team / star selections
          "NBA: 2026-27 All-NBA First Team", "NBA: 2026-27 All-NBA Teams (Any)",
          "NBA: 2026-27 All-Defensive First Team", "NBA: 2026-27 All-Rookie First Team",
          "NBA All-Stars 2027",
          # stat thresholds
          "NBA: Players to Average 25+ PPG in 2026-27", "NBA: Players to Average 30+ PPG in 2026-27",
          "NBA: Players to Average 10+ RPG in 2026-27", "NBA: Players to Average 10+ APG in 2026-27",
          "NBA: Players to Average 2+ SPG in 2026-27"]
FUTURES = ["NBA: 2027 Champion", "NBA: 2027 Eastern Conference Champion",
           "NBA: 2027 Western Conference Champion", "NBA: Team to Make Playoffs",
           "NBA: Team to Make Play-In", "NBA: 2026-27 Western Conference #1 Seed",
           "NBA: 2026-27 Regular Season Win Totals", "NBA: Teams to Win 50+ Games",
           "NBA Playoffs: Team to advance to Eastern Conference Finals",
           "NBA Playoffs: 2027 Finals Exact Matchup"]
OTHER = ["NBA: Players to be Traded Before the 2027 Trade Deadline",
         "NBA: Steph Curry Next Team", "NBA Free Agency: DeMar DeRozan Next Team",
         "NBA: Team to reach 2026 NBA Cup Championship Game",
         "NBA: Team to reach 2026 NBA Cup Knockout Rounds", "NBA: 2026 NBA Cup Winner",
         "NBA: 2026-27 Worst Regular Season Record",
         "Las Vegas NBA franchise and arena price?",
         "NBA to announce expansion team in Las Vegas by...?",
         "2027 NBA Draft: 1st Overall Pick", "2027 NBA Draft Lottery: 1st Pick",
         "NBA: LeBron James 2026-27 Regular Season Triple-Doubles", ""]


def hist(hours_back, a, b):
    return [{"timestamp": ts(h), "impliedProbability": p}
            for h, p in ((hours_back + 1, a), (hours_back, a), (0, b))]


class CategoryDetectionTest(unittest.TestCase):
    def test_event_titles(self):
        for t in AWARDS:
            self.assertEqual(ma.market_category({"eventTitle": t}), "awards", t)
        for t in FUTURES:
            self.assertEqual(ma.market_category({"eventTitle": t}), "futures", t)
        for t in OTHER:
            self.assertEqual(ma.market_category({"eventTitle": t}), "other", t)

    def test_question_fallback(self):
        self.assertEqual(ma.market_category(
            {"eventTitle": "Something", "question": "Will X win the 2026-27 NBA MVP?"}), "awards")
        self.assertEqual(ma.market_category(
            {"question": "Will the Celtics win the 2027 NBA Finals?"}), "futures")
        self.assertEqual(ma.market_category({"question": "Will Y be traded?"}), "other")
        self.assertEqual(ma.market_category({}), "other")

    def test_event_title_wins_over_question(self):
        m = {"eventTitle": "NBA: 2026-27 MVP", "question": "Will X make the Playoffs?"}
        self.assertEqual(ma.market_category(m), "awards")

    def test_new_award_groups_use_award_thresholds(self):
        for t in ("NBA: 2026-27 Points Per Game Leader", "NBA: 2026-27 All-NBA First Team",
                  "NBA All-Stars 2027", "NBA: Players to Average 10+ RPG in 2026-27"):
            self.assertEqual(ma.category_threshold({"eventTitle": t}, "digest"), 0.04, t)
            self.assertEqual(ma.category_threshold({"eventTitle": t}, "instant"), 0.08, t)

    def test_thresholds(self):
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: 2026-27 MVP"}, "digest"), 0.04)
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: 2026-27 MVP"}, "instant"), 0.08)
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: 2027 Champion"}, "digest"), 0.02)
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: 2027 Champion"}, "instant"), 0.05)
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: Steph Curry Next Team"}, "digest"), 0.02)
        self.assertEqual(ma.category_threshold({"eventTitle": "NBA: Steph Curry Next Team"}, "instant"), 0.04)

    def test_clears_boundary(self):
        self.assertTrue(ma.clears(0.28 - 0.20, 0.08))          # float 0.0800000x
        self.assertTrue(ma.clears(0.53 - 0.49, 0.04))
        self.assertFalse(ma.clears(0.0799, 0.08))
        self.assertTrue(ma.clears(-0.05, 0.05))

    def test_cooldown_override_uses_threshold_passed(self):
        la = {"c": {"ts": ts(1), "p": 0.30}}
        self.assertFalse(ma.cooldown_ok(la, "c", 0.45, 0.08))  # 15 < 2x8
        self.assertTrue(ma.cooldown_ok(la, "c", 0.46, 0.08))   # 16 = 2x8
        self.assertTrue(ma.cooldown_ok(la, "c", 0.38, 0.04))   # 8 = 2x4


class InstantCategoryTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def _run(self, title, a, b, cid="0x1", event="E1"):
        m = mk(cid, "Will Omega win it?", b, event=event, neg=False, title=title)
        return self.h.run([m], {cid: hist(6, a, b)})

    def test_awards_needs_8(self):
        self.assertEqual(self._run("NBA: 2026-27 MVP", 0.30, 0.37), [])        # 7pt
        h2 = Harness()
        try:
            m = mk("0x1", "Will Omega win it?", 0.38, neg=False, title="NBA: 2026-27 MVP")
            self.assertEqual(len(h2.run([m], {"0x1": hist(6, 0.30, 0.38)})), 1)   # 8pt
        finally:
            h2.close()

    def test_futures_needs_5(self):
        self.assertEqual(self._run("NBA: 2027 Champion", 0.10, 0.14, cid="0xa", event="A"), [])
        self.assertEqual(len(self._run("NBA: 2027 Champion", 0.10, 0.15, cid="0xb", event="B")), 1)

    def test_other_needs_4(self):
        self.assertEqual(self._run("NBA: Steph Curry Next Team", 0.10, 0.13, cid="0xa", event="A"), [])
        self.assertEqual(len(self._run("NBA: Steph Curry Next Team", 0.10, 0.14, cid="0xb", event="B")), 1)

    def test_awards_early_realert_needs_16(self):
        title = "NBA: 2026-27 MVP"
        q = "Will Omega win it?"
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.38, neg=False, title=title)],
                                        {"0x1": hist(6, 0.30, 0.38)})), 1)
        # +15 since the alert: blocked (needs 2 x 8 = 16)
        self.assertEqual(self.h.run([mk("0x1", q, 0.53, neg=False, title=title)],
                                    {"0x1": hist(6, 0.38, 0.53)}), [])
        # +16 since the alert: re-alerts
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.54, neg=False, title=title)],
                                        {"0x1": hist(6, 0.38, 0.54)})), 1)

    def test_other_early_realert_needs_8(self):
        title = "NBA: Steph Curry Next Team"
        q = "Will Omega win it?"
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.15, neg=False, title=title)],
                                        {"0x1": hist(6, 0.10, 0.15)})), 1)
        self.assertEqual(self.h.run([mk("0x1", q, 0.22, neg=False, title=title)],
                                    {"0x1": hist(6, 0.15, 0.22)}), [])      # +7
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.23, neg=False, title=title)],
                                        {"0x1": hist(6, 0.15, 0.23)})), 1)  # +8


class DigestCategoryTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_digest_floors(self):
        ms = [mk("0x1", "Will A win the MVP?", 0.33, event="MVP", title="NBA: 2026-27 MVP"),     # 3pt award
              mk("0x2", "Will B win the ROY?", 0.44, event="ROY", title="NBA: 2026-27 Rookie of the Year"),  # 4pt
              mk("0x3", "Will C win it all?", 0.12, event="CH", title="NBA: 2027 Champion"),      # 2pt
              mk("0x4", "Will D be traded?", 0.215, event="TR",
                 title="NBA: Players to be Traded Before the 2027 Trade Deadline"),               # 1.5pt
              mk("0x5", "Will E sign with X?", 0.22, event="NT", title="NBA: Steph Curry Next Team")]  # 2pt
        h = {"0x1": hist(24, 0.30, 0.33), "0x2": hist(24, 0.40, 0.44), "0x3": hist(24, 0.10, 0.12),
             "0x4": hist(24, 0.20, 0.215), "0x5": hist(24, 0.20, 0.22)}
        posted = self.h.run(ms, h, fn="digest")
        self.assertEqual(len(posted), 1)
        text = posted[0]
        self.assertNotIn("A's odds", text)
        self.assertIn("B's odds", text)
        self.assertIn("C's odds", text)
        self.assertNotIn("D's odds", text)
        self.assertIn("E's odds", text)

    def test_digest_award_realert_needs_8(self):
        ms = [mk("0x1", "Will A win the MVP?", 0.37, event="MVP", title="NBA: 2026-27 MVP")]
        (self.h.dir / "alerts_state.json").write_text(json.dumps(
            {"last_alert": {"0x1": {"ts": ts(2), "p": 0.30}}}))
        self.assertEqual(self.h.run(ms, {"0x1": hist(24, 0.25, 0.37)}, fn="digest"), [])   # +7 since alert
        ms[0]["impliedProbability"] = 0.38
        self.assertEqual(len(self.h.run(ms, {"0x1": hist(24, 0.25, 0.38)}, fn="digest")), 1)  # +8

    def test_empty_digest_posts_nothing(self):
        ms = [mk("0x1", "Will A win the MVP?", 0.33, event="MVP", title="NBA: 2026-27 MVP"),
              mk("0x2", "Will D be traded?", 0.21, event="TR", title="NBA: Steph Curry Next Team")]
        posted = self.h.run(ms, {"0x1": hist(24, 0.30, 0.33), "0x2": hist(24, 0.20, 0.21)}, fn="digest")
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
