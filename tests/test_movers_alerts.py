"""
Tests for the Slack market-type mute list in scripts/movers_alerts.py.

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

MUTED = ["Pistons vs. Wizards", "Clippers vs. Raptors", "Hawks vs. Pacers",
         "Bulls vs. Nuggets: 1H Moneyline", "Hawks vs. Pacers: 1H Moneyline"]
ALERTED = [
    "Spread: Raptors (-4.5)",
    "Spread: Pacers (-1.5)",
    "1H Spread: Nuggets (-4.5)",
    "Clippers vs. Raptors: O/U 219.5",
    "Bulls vs. Nuggets: 1H O/U 118.5",
    "Will Boston Celtics win the 2027 NBA Finals?",                 # futures
    "Will Cameron Boozer win the 2026-27 NBA Rookie of the Year?",  # award
    "Heat vs. Magic: Team to Score First",                          # other game prop
    "Trail Blazers vs. Kings: Odd/Even Score",
]


class SlackMarketTypeTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(ma.slack_market_type({"question": "Pistons vs. Wizards"}), "moneyline")
        self.assertEqual(ma.slack_market_type({"question": "Spread: Raptors (-4.5)"}), "spreads")
        self.assertEqual(ma.slack_market_type({"question": "1H Spread: Nuggets (-4.5)"}), "spreads")
        self.assertEqual(ma.slack_market_type({"question": "Bulls vs. Nuggets: 1H Moneyline"}), "moneyline")
        self.assertEqual(ma.slack_market_type({"question": "Bulls vs. Nuggets: 1H O/U 118.5"}), "totals")
        self.assertEqual(ma.slack_market_type({"question": "Clippers vs. Raptors: O/U 219.5"}), "totals")
        self.assertIsNone(ma.slack_market_type({"question": "Will Boston Celtics win the 2027 NBA Finals?"}))
        self.assertIsNone(ma.slack_market_type({}))

    def test_default_mute_list(self):
        self.assertEqual(ma.SLACK_MUTED_MARKET_TYPES, ["moneyline"])
        for q in MUTED:
            self.assertTrue(ma.slack_muted({"question": q}), q)
        for q in ALERTED:
            self.assertFalse(ma.slack_muted({"question": q}), q)

    def test_list_is_extendable(self):
        with mock.patch.object(ma, "SLACK_MUTED_MARKET_TYPES", ["moneyline", "spreads", "totals"]):
            self.assertTrue(ma.slack_muted({"question": "Spread: Pacers (-1.5)"}))
            self.assertTrue(ma.slack_muted({"question": "Clippers vs. Raptors: O/U 219.5"}))
            self.assertFalse(ma.slack_muted({"question": "Will Boston Celtics win the 2027 NBA Finals?"}))


class RunInstantMuteTest(unittest.TestCase):
    """End to end: every market below has a 20-point 6h swing and clears the
    floors, so only the mute list decides whether it posts."""

    def test_muted_markets_skip_post_and_state(self):
        now = datetime.now(timezone.utc)
        history = [
            {"timestamp": (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "impliedProbability": p}
            for h, p in ((7, 0.40), (6, 0.40), (0, 0.60))
        ]
        markets = []
        for i, q in enumerate(MUTED + ALERTED):
            markets.append({"conditionId": f"0x{i:02d}", "question": q, "negRisk": False,
                            "resolved": False, "impliedProbability": 0.60,
                            "volume": 50000, "liquidity": 5000})

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "markets").mkdir()
            (tmp / "index.json").write_text(json.dumps({"markets": markets}))
            for m in markets:
                (tmp / "markets" / f"{m['conditionId']}.json").write_text(
                    json.dumps({"history": history}))
            posted = []
            with mock.patch.multiple(ma, INDEX_PATH=tmp / "index.json",
                                     MARKETS_DIR=tmp / "markets",
                                     STATE_PATH=tmp / "alerts_state.json"), \
                 mock.patch.object(ma, "post_slack", side_effect=lambda t: posted.append(t) or True), \
                 mock.patch("builtins.print"):
                ma.run_instant()
            state = json.loads((tmp / "alerts_state.json").read_text())

        self.assertEqual(len(posted), len(ALERTED))
        text = "\n".join(posted)
        for q in MUTED:
            self.assertNotIn(ma.slugify(q) + "/", text)
        for q in ALERTED:
            self.assertIn(ma.slugify(q) + "/", text)
        muted_ids = {m["conditionId"] for m in markets if m["question"] in MUTED}
        self.assertFalse(muted_ids & set(state["instant_sent"]))
        self.assertEqual(len(state["instant_sent"]), len(ALERTED))


if __name__ == "__main__":
    unittest.main()
