"""
Per-URL <lastmod> for docs/sitemap.xml, driven by content hashes.

State lives in data/sitemap_lastmod.json (committed): one entry per sitemap URL,
keyed by its path under https://hoopsmatic.com/polymarket/ ("" is the section
root), value [content_hash, lastmod]. Each build hashes every sitemap page:
  - same hash as stored  -> keep the stored lastmod
  - new or changed hash  -> lastmod = the build time
so a page's date only moves when its bytes change, never "today" on every run.
URLs that leave the sitemap are dropped from the state.

The file is written one entry per line, sorted, so a poll commit only touches
the lines of pages that actually changed.
"""

import hashlib
import json
from datetime import datetime, timezone
from xml.sax.saxutils import escape

# Public address of this section (served by the hoopsmatic.com Worker from docs/).
SITEMAP_BASE = "https://hoopsmatic.com/polymarket/"


def content_hash(data):
    """Short, stable hash of a page's bytes (str is encoded as UTF-8)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


def now_iso():
    """Build time as a W3C datetime (UTC, seconds)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path):
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def dump_state(state):
    """Deterministic, line-per-URL JSON (small diffs)."""
    lines = [f"{json.dumps(k, ensure_ascii=False)}:{json.dumps(state[k])}" for k in sorted(state)]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def update_state(entries, state, now):
    """entries: [(key, hash)] in sitemap order. Returns (lastmods, new_state)."""
    new_state, lastmods = {}, []
    for key, h in entries:
        old = state.get(key)
        lm = old[1] if (isinstance(old, list) and len(old) == 2 and old[0] == h) else now
        new_state[key] = [h, lm]
        lastmods.append(lm)
    return lastmods, new_state


def render_sitemap(keys, lastmods):
    body = "".join(
        f"<url><loc>{escape(SITEMAP_BASE + k)}</loc><lastmod>{lm}</lastmod></url>\n"
        for k, lm in zip(keys, lastmods))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + body + "</urlset>\n")
