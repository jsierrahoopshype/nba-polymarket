"""
Tests for the odds-move-only instant alerts (new markets, flips, crossings and
volume never trigger on their own) and repetition rules in
scripts/movers_alerts.py.

Run:  python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import movers_alerts as ma  # noqa: E402


def ts(hours_ago, now=None):
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def flat_history(p, vol24=None):
    return [{"timestamp": ts(h), "impliedProbability": p, "volume24hr": vol24}
            for h in (7, 6, 0)]


def mk(cid, q, p, event="E1", neg=True, volume=50000, liquidity=5000, vol24=0, title="NBA: Test Event"):
    return {"conditionId": cid, "question": q, "negRisk": neg, "resolved": False,
            "impliedProbability": p, "volume": volume, "liquidity": liquidity,
            "volume24hr": vol24, "eventId": event, "eventTitle": title}


class Harness:
    """Runs run_instant / run_digest repeatedly against one temp data dir."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "markets").mkdir()

    def close(self):
        self._tmp.cleanup()

    def _patches(self, posted):
        return [mock.patch.multiple(ma, INDEX_PATH=self.dir / "index.json",
                                    MARKETS_DIR=self.dir / "markets",
                                    STATE_PATH=self.dir / "alerts_state.json",
                                    SEEN_PATH=self.dir / "seen_markets.json",
                                    TRACKER_PATH=self.dir / "alerts_tracker.json"),
                mock.patch.object(ma, "post_slack", side_effect=lambda t: posted.append(t) or True),
                mock.patch("builtins.print")]

    def run(self, markets, histories, fn="instant"):
        (self.dir / "index.json").write_text(json.dumps({"markets": markets}))
        for m in markets:
            (self.dir / "markets" / f"{m['conditionId']}.json").write_text(
                json.dumps({"history": histories.get(m["conditionId"], flat_history(m["impliedProbability"]))}))
        posted = []
        ps = self._patches(posted)
        for p in ps:
            p.start()
        try:
            ma.run_instant() if fn == "instant" else ma.run_digest(force=True)
        finally:
            for p in reversed(ps):
                p.stop()
        return posted

    def state(self):
        return json.loads((self.dir / "alerts_state.json").read_text())


class AlertKindsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_new_market_is_recorded_but_never_posted(self):
        base = [mk("0xa", "Will A win the 2027 NBA Finals?", 0.30)]
        self.assertEqual(self.h.run(base, {}), [])                       # bootstrap: silent
        seen = json.loads((self.h.dir / "seen_markets.json").read_text())
        self.assertIn("0xa", seen["markets"])
        # a new market past the old $5K floor: still silent (volume is not a trigger)
        big = base + [mk("0xb", "Will B be traded?", 0.2, event="E2", neg=False, volume=60000)]
        self.assertEqual(self.h.run(big, {}), [])
        seen = json.loads((self.h.dir / "seen_markets.json").read_text())
        self.assertIn("0xb", seen["markets"])

    def test_flip_alone_sends_nothing(self):
        a, b = "Will Alpha win the 2026-27 NBA MVP?", "Will Beta win the 2026-27 NBA MVP?"
        self.assertEqual(self.h.run([mk("0x1", a, 0.40), mk("0x2", b, 0.35)], {}), [])
        # Beta overtakes Alpha, but neither 6h move clears the 8pt awards threshold
        self.assertEqual(self.h.run([mk("0x1", a, 0.36), mk("0x2", b, 0.42)], {}), [])

    def test_flip_rides_along_on_a_threshold_move(self):
        a, b = "Will Alpha win the 2026-27 NBA MVP?", "Will Beta win the 2026-27 NBA MVP?"
        self.h.run([mk("0x1", a, 0.40), mk("0x2", b, 0.30)], {})
        hb = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.30), (6, 0.30), (0, 0.45))]
        posted = self.h.run([mk("0x1", a, 0.38), mk("0x2", b, 0.45)], {"0x2": hb})
        self.assertEqual(len(posted), 1)
        self.assertIn("from 30.0% to 45.0%", posted[0])
        self.assertIn("making Beta the new favorite", posted[0])

    def test_crossing_alone_sends_nothing(self):
        q = "Will Gamma win the 2027 NBA Finals?"
        self.h.run([mk("0x1", q, 0.45, neg=False)], {})
        self.assertEqual(self.h.run([mk("0x1", q, 0.53, neg=False)], {}), [])   # 50% crossed, flat 6h

    def test_cooldown_and_double_threshold(self):
        q = "Will Delta win the 2027 NBA Finals?"
        swing = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.30), (6, 0.30), (0, 0.37))]
        self.h.run([mk("0x1", q, 0.30, neg=False)], {})                  # bootstrap
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.37, neg=False)], {"0x1": swing})), 1)
        # same 6h swing next run: cooled down
        self.assertEqual(self.h.run([mk("0x1", q, 0.37, neg=False)], {"0x1": swing}), [])
        # another 6pt move only (< 2x6): still blocked
        swing2 = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.37), (6, 0.37), (0, 0.43))]
        self.assertEqual(self.h.run([mk("0x1", q, 0.43, neg=False)], {"0x1": swing2}), [])
        # 13pt beyond the last alert price: allowed again
        swing3 = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.37), (6, 0.37), (0, 0.495))]
        self.assertEqual(len(self.h.run([mk("0x1", q, 0.495, neg=False)], {"0x1": swing3})), 1)

    def test_volume_spike_with_flat_odds_sends_nothing(self):
        q = "Will Epsilon be traded before the 2027 trade deadline?"
        hist = [{"timestamp": ts(24 * d), "impliedProbability": 0.4, "volume24hr": 5000} for d in range(7, 0, -1)]
        hist.append({"timestamp": ts(0), "impliedProbability": 0.4, "volume24hr": 80000})   # 16x volume
        self.assertEqual(self.h.run([mk("0x1", q, 0.4, neg=False, vol24=80000)], {"0x1": hist}), [])
        self.assertEqual(self.h.run([mk("0x1", q, 0.4, neg=False, vol24=80000)], {"0x1": hist}), [])
        self.assertEqual(self.h.state()["instant_sent"], {})

    def test_odds_move_sends_one_message_with_url_last(self):
        q = "Will Denver Nuggets win the 2027 NBA Finals?"
        hist = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.10), (6, 0.10), (0, 0.17))]
        m = mk("0x1", q, 0.17, neg=False, vol24=45000, title="NBA: 2027 Champion")
        posted = self.h.run([m], {"0x1": hist})
        self.assertEqual(len(posted), 1)
        lines = posted[0].split("\n")
        self.assertEqual(lines[-1], "https://hoopsmatic.com/polymarket/market/"
                                    "will-denver-nuggets-win-the-2027-nba-finals/")
        self.assertNotIn(" ", lines[-1])                          # URL alone on its line
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("The Nuggets' odds of winning the 2027 NBA Finals "))
        self.assertIn("10.0% to 17.0%", lines[0])
        self.assertRegex(lines[0], r"(in the past|over the last) 6 hours")
        self.assertIn("with $45K traded in the past day", lines[0])  # supporting clause only
        self.assertNotIn("surge", lines[0].lower().replace("surged", ""))
        self.assertNotIn("*", posted[0])                         # plain text

    def test_wording_varies(self):
        m = {"question": "Will Denver Nuggets win the 2027 NBA Finals?"}
        texts = {ma.instant_sentence(m, 0.10, 0.17, f"k{i}").split("\n")[0] for i in range(40)}
        self.assertGreater(len(texts), 3)
        for t in texts:
            self.assertIn("10.0%", t)
            self.assertIn("17.0%", t)

    def test_one_message_per_event(self):
        qa, qb = "Will Zeta win the 2026-27 NBA MVP?", "Will Eta win the 2026-27 NBA MVP?"
        sa = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.20), (6, 0.20), (0, 0.28))]
        # 9pt drop: clears the 8pt awards instant threshold
        sb = [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((7, 0.17), (6, 0.17), (0, 0.08))]
        posted = self.h.run([mk("0x1", qa, 0.28), mk("0x2", qb, 0.08)], {"0x1": sa, "0x2": sb})
        self.assertEqual(len(posted), 1)
        self.assertIn("Zeta", posted[0])
        self.assertIn("Eta", posted[0])

    def test_digest_one_per_event_and_cooldown(self):
        mk_h = lambda a, b: [{"timestamp": ts(h), "impliedProbability": p} for h, p in ((25, a), (24, a), (0, b))]
        ms = [mk("0x1", "Will Theta win the 2026-27 NBA MVP?", 0.30),
              mk("0x2", "Will Iota win the 2026-27 NBA MVP?", 0.20),
              mk("0x3", "Will Kappa win the 2027 NBA Finals?", 0.10, event="E9"),
              mk("0x4", "Will Lambda win the 2026-27 NBA Rookie of the Year?", 0.40, event="ROY")]
        hist = {"0x1": mk_h(0.20, 0.30), "0x2": mk_h(0.25, 0.20),
                "0x3": mk_h(0.05, 0.10), "0x4": mk_h(0.30, 0.40)}
        (self.h.dir / "alerts_state.json").write_text(json.dumps(
            {"last_alert": {"0x4": {"ts": ts(1), "p": 0.40}}}))
        posted = self.h.run(ms, hist, fn="digest")
        self.assertEqual(len(posted), 1)
        self.assertIn("Theta", posted[0])          # biggest mover in event E1
        self.assertNotIn("Iota", posted[0])        # same event: dropped
        self.assertIn("Kappa", posted[0])
        self.assertNotIn("Lambda", posted[0])      # alerted 1h ago, no 2x move since


if __name__ == "__main__":
    unittest.main()
