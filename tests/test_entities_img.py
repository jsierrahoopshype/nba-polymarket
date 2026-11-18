"""
Headshot URLs in entities.json AND everything under docs/ must point at
hoopsmatic.com (same-origin mirror of nba-headshots), never github.io or
raw.githubusercontent.com. Team logos (cdn.nba.com) stay as they are.

Run:  python -m unittest discover -s tests
"""

import json
import os
import re
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


# Any github-hosted nba-headshots URL (github.io pages or raw.githubusercontent).
GH_HEADSHOT = re.compile(
    rb"github\.io/nba-headshots|githubusercontent\.com/[^\"'\s]*nba-headshots")


class DocsHeadshotsTest(unittest.TestCase):
    def test_headshot_constant_is_hoopsmatic(self):
        self.assertTrue(be.HEADSHOT_FACE.startswith("https://hoopsmatic.com/img/nba-headshots/"))

    def test_no_github_headshot_urls_anywhere_in_docs(self):
        hits, scanned = [], 0
        for d, _dirs, files in os.walk(ROOT / "docs"):
            for f in files:
                path = Path(d) / f
                scanned += 1
                if GH_HEADSHOT.search(path.read_bytes()):
                    hits.append(str(path.relative_to(ROOT)))
        self.assertGreater(scanned, 0)
        self.assertEqual(hits, [], f"{len(hits)} docs/ files still use github headshot URLs, "
                                   f"e.g. {hits[:5]}")

    def test_player_pages_use_hoopsmatic_headshots(self):
        page = (ROOT / "docs" / "player" / "cade-cunningham" / "index.html").read_text(encoding="utf-8")
        self.assertIn("https://hoopsmatic.com/img/nba-headshots/players/headshots/face-fill/"
                      "1630595-cade-cunningham.webp", page)


if __name__ == "__main__":
    unittest.main()
