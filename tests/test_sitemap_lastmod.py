"""
Sitemap lastmod: per-URL, content-hash driven, hoopsmatic.com URLs only.

Run:  python -m unittest discover -s tests
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_entities as be  # noqa: E402
import sitemap_lastmod as slm  # noqa: E402

W3C = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class UpdateStateTest(unittest.TestCase):
    def test_only_changed_pages_move(self):
        old = {"": ["aaa", "2026-01-01T00:00:00Z"], "player/x/": ["bbb", "2026-02-02T00:00:00Z"],
               "gone/": ["ccc", "2026-03-03T00:00:00Z"]}
        lms, new = slm.update_state([("", "aaa"), ("player/x/", "CHANGED"), ("market/new/", "ddd")],
                                    old, "2026-11-18T12:00:00Z")
        self.assertEqual(lms, ["2026-01-01T00:00:00Z", "2026-11-18T12:00:00Z", "2026-11-18T12:00:00Z"])
        self.assertNotIn("gone/", new)                       # dropped URLs leave the state
        self.assertEqual(new["player/x/"], ["CHANGED", "2026-11-18T12:00:00Z"])

    def test_rerun_with_same_content_is_stable(self):
        entries = [("", "aaa"), ("teams/", "bbb")]
        _, s1 = slm.update_state(entries, {}, "2026-11-01T00:00:00Z")
        lms, s2 = slm.update_state(entries, s1, "2026-11-18T00:00:00Z")   # later run, no change
        self.assertEqual(s1, s2)
        self.assertEqual(set(lms), {"2026-11-01T00:00:00Z"})

    def test_dump_is_deterministic_json(self):
        s = {"b/": ["2", "2026-01-01T00:00:00Z"], "": ["1", "2026-01-01T00:00:00Z"]}
        self.assertEqual(json.loads(slm.dump_state(s)), s)
        self.assertEqual(slm.dump_state(s), slm.dump_state(dict(reversed(list(s.items())))))

    def test_render_uses_hoopsmatic(self):
        xml = slm.render_sitemap(["", "player/x/"], ["2026-01-01T00:00:00Z"] * 2)
        self.assertIn("<loc>https://hoopsmatic.com/polymarket/</loc>", xml)
        self.assertIn("<loc>https://hoopsmatic.com/polymarket/player/x/</loc>", xml)

    def test_key_to_file(self):
        self.assertEqual(be.key_to_file(""), be.DOCS_DIR / "index.html")
        self.assertEqual(be.key_to_file("movers.html"), be.DOCS_DIR / "movers.html")
        self.assertEqual(be.key_to_file("team/utah-jazz/"), be.DOCS_DIR / "team/utah-jazz/index.html")


class CommittedSitemapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.xml = (ROOT / "docs" / "sitemap.xml").read_text(encoding="utf-8")
        cls.urls = re.findall(r"<url><loc>([^<]+)</loc><lastmod>([^<]+)</lastmod></url>", cls.xml)
        cls.state = json.loads((ROOT / "data" / "sitemap_lastmod.json").read_text(encoding="utf-8"))

    def test_no_github_io(self):
        self.assertNotIn("github.io", self.xml)

    def test_every_url_is_hoopsmatic_with_valid_lastmod(self):
        self.assertGreater(len(self.urls), 100)
        self.assertEqual(len(self.urls), self.xml.count("<url>"))     # every <url> has a lastmod
        self.assertLess(len(self.urls), 50000)                          # sitemap protocol limit
        for loc, lm in self.urls:
            self.assertTrue(loc.startswith(slm.SITEMAP_BASE), loc)
            self.assertRegex(lm, W3C)

    def test_state_matches_sitemap(self):
        keys = [loc[len(slm.SITEMAP_BASE):] for loc, _ in self.urls]
        self.assertEqual(sorted(keys), sorted(self.state))
        for (loc, lm), k in zip(self.urls, keys):
            self.assertEqual(self.state[k][1], lm, loc)

    def test_every_sitemap_url_has_a_page(self):
        for k in self.state:
            self.assertTrue(be.key_to_file(k).is_file(), k)


if __name__ == "__main__":
    unittest.main()
