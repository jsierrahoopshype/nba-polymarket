"""
Tests for the new-market, flip, crossing, volume-spike and repetition rules in
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

    def test_new_market_bootstrap_silent_then_announced(self):
        base = [mk("0xa", "Will A win the 2027 NBA Finals?", 0.30)]
        self.assertEqual(self.h.run(base, {}), [])                       # bootstrap: silent
        seen = json.loads((self.h.dir / "seen_markets.json").read_text())
        self.assertIn("0xa", seen["markets"])
        # a tiny new market: recorded, not announced
        small = base + [mk("0xb", "Will B be traded?", 0.2, event="E2", neg=False, volume=100)]
        self.assertEqual(self.h.run(small, {}), [])
        # it reaches the floor -> announced once
        big = base + [mk("0xb", "Will B be traded?", 0.2, event="E2", neg=False, volume=6000)]
        posted = self.h.run(big, {})
        self.assertEqual(len(posted), 1)
        self.assertIn("New market", posted[0])
        self.assertEqual(self.h.run(big, {}), [])                        # never again

    def test_new_event_is_one_message(self):
        self.h.run([mk("0xa", "Will A win the 2027 NBA Finals?", 0.30)], {})
        new = [mk(f"0xc{i}", f"Will Player {i} win the 2026-27 NBA MVP?", 0.3 - i * 0.05,
                  event="MVP", title="NBA: 2026-27 MVP") for i in range(4)]
        posted = self.h.run([mk("0xa", "Will A win the 2027 NBA Finals?", 0.30)] + new, {})
        self.assertEqual(len(posted), 1)
        self.assertIn("NBA: 2026-27 MVP (4 outcomes", posted[0])

    def test_flip(self):
        a, b = "Will Alpha win the 2026-27 NBA MVP?", "Will Beta win the 2026-27 NBA MVP?"
        self.assertEqual(self.h.run([mk("0x1", a, 0.40), mk("0x2", b, 0.35)], {}), [])
        posted = self.h.run([mk("0x1", a, 0.36), mk("0x2", b, 0.42)], {})
        self.assertEqual(len(posted), 1)
        self.assertIn("New favorite", posted[0])
        self.assertIn("Beta (42.0%) has overtaken Alpha (36.0%)", posted[0])

    def test_flip_needs_margin(self):
        a, b = "Will Alpha win the 2026-27 NBA MVP?", "Will Beta win the 2026-27 NBA MVP?"
        self.h.run([mk("0x1", a, 0.40), mk("0x2", b, 0.35)], {})
        self.assertEqual(self.h.run([mk("0x1", a, 0.385), mk("0x2", b, 0.39)], {}), [])

    def test_crossing_with_hysteresis(self):
        q = "Will Gamma win the 2027 NBA Finals?"
        self.h.run([mk("0x1", q, 0.45, neg=False)], {})
        self.assertEqual(self.h.run([mk("0x1", q, 0.51, neg=False)], {}), [])   # inside 1.5pt band
        posted = self.h.run([mk("0x1", q, 0.53, neg=False)], {})
        self.assertEqual(len(posted), 1)
        self.assertIn("crossed above 50%", posted[0])

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

    def test_volume_spike(self):
        q = "Will Epsilon be traded before the 2027 trade deadline?"
        hist = [{"timestamp": ts(24 * d), "impliedProbability": 0.4, "volume24hr": 5000} for d in range(7, 0, -1)]
        hist.append({"timestamp": ts(0), "impliedProbability": 0.4, "volume24hr": 30000})
        self.h.run([mk("0x1", q, 0.4, neg=False, vol24=30000)], {"0x1": hist})   # bootstrap run also checks spikes
        st = self.h.state()
        self.assertEqual(st["instant_sent"]["0x1"]["kind"], "volume")

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
