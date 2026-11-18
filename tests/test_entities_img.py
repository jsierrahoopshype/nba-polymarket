"""
entities.json headshot URLs must point at hoopsmatic.com (same-origin mirror of
nba-headshots), never github.io. Team logos (cdn.nba.com) stay as they are.

Run:  python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_entities as be  # noqa: E402


class EntitiesImgTest(unittest.TestCase):
    def test_rewrites_headshot_prefix_keeps_path(self):
        gh = be.HEADSHOT_FACE.format(stem="1630595-cade-cunningham")
        self.assertEqual(
            be.entities_img(gh),
            "https://hoopsmatic.com/img/nba-headshots/players/headshots/face-fill/1630595-cade-cunningham.webp")

    def test_leaves_logos_and_none_alone(self):
        logo = be.TEAM_LOGO.format(id=1610612762)
        self.assertEqual(be.entities_img(logo), logo)
        self.assertIsNone(be.entities_img(None))

    def test_committed_entities_json_has_no_github_io(self):
        text = (ROOT / "data" / "entities.json").read_text(encoding="utf-8")
        self.assertNotIn("github.io", text)
        d = json.loads(text)
        imgs = [x["img"] for x in d["directory"] if x.get("img")]
        for m in d["markets"].values():
            imgs += [x["img"] for x in m["all"] if x.get("img")]
        self.assertTrue(imgs)
        for u in imgs:
            self.assertTrue(u.startswith(("https://hoopsmatic.com/img/nba-headshots/",
                                          "https://cdn.nba.com/")), u)


if __name__ == "__main__":
    unittest.main()
