"""
NBA Polymarket entity-page builder.

Runs in the polling workflow right after build_index.py. It:

  1. Reads data/index.json (live + recently-resolved markets).
  2. Matches each market's question against the 30 NBA teams (name aliases) and
     the active player roster (vendored from jsierrahoopshype/nba-headshots) by
     FULL NAME ONLY — see match_question for why last names were dropped.
  3. Statically generates an SEO page per entity under docs/player/<slug>/ and
     docs/team/<slug>/, plus the docs/players/ and docs/teams/ directories and
     docs/sitemap.xml.
  4. Writes data/entities.json — a market -> primary-entity map the standings
     rows use to show a headshot/logo next to a name.

Churn control: entity pages are static SEO shells (title, meta, heading,
image, and a crawlable list of the entity's market questions). The live numbers
are filled in the browser from index.json, so a page's bytes change only when
its *market membership* changes — not when prices move. Every output is written
only if its content actually differs (hash compare), so a normal poll rewrites
nothing here.

Usage:
    python scripts/build_entities.py
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
INDEX_PATH = DATA_DIR / "index.json"
ENTITIES_PATH = DATA_DIR / "entities.json"
ROSTER_PATH = Path(__file__).resolve().parent / "vendor" / "roster_players.json"

# Public site base (GitHub Pages serves the repo root; the app lives in /docs).
# Change this if a custom domain is configured.
SITE_BASE = "https://jsierrahoopshype.github.io/nba-polymarket"

# Image sources. Headshots are keyed by the roster's headshot filename
# (<nba_id>-<slug>.png, stored per player) on the nba-headshots Pages site;
# confirmed 200 for face + thumb. HEADSHOT_RAW is the verified-reachable
# raw.githubusercontent mirror used as the <img> onerror fallback.
HEADSHOT_FACE = "https://jsierrahoopshype.github.io/nba-headshots/players/headshots/face/{file}"
HEADSHOT_THUMB = "https://jsierrahoopshype.github.io/nba-headshots/players/headshots/thumb/{file}"
HEADSHOT_RAW = "https://raw.githubusercontent.com/jsierrahoopshype/nba-headshots/main/players/headshots/face/{file}"
TEAM_LOGO = "https://cdn.nba.com/logos/nba/{id}/global/L/logo.svg"
# Optional Cloudflare Worker proxy fallback for team logos — fill in to enable.
TEAM_LOGO_PROXY = ""

# The 30 teams: abbrev -> (NBA team id, full name, nickname, slug, match aliases).
# NBA ids confirmed from the headshots roster. Aliases are deliberately
# unambiguous — bare "LA"/"Los Angeles" are omitted (Lakers vs Clippers).
TEAMS = {
    "ATL": (1610612737, "Atlanta Hawks", "Hawks", "atlanta-hawks", ["hawks", "atlanta"]),
    "BKN": (1610612751, "Brooklyn Nets", "Nets", "brooklyn-nets", ["nets", "brooklyn"]),
    "BOS": (1610612738, "Boston Celtics", "Celtics", "boston-celtics", ["celtics", "boston"]),
    "CHA": (1610612766, "Charlotte Hornets", "Hornets", "charlotte-hornets", ["hornets", "charlotte"]),
    "CHI": (1610612741, "Chicago Bulls", "Bulls", "chicago-bulls", ["bulls", "chicago"]),
    "CLE": (1610612739, "Cleveland Cavaliers", "Cavaliers", "cleveland-cavaliers", ["cavaliers", "cavs", "cleveland"]),
    "DAL": (1610612742, "Dallas Mavericks", "Mavericks", "dallas-mavericks", ["mavericks", "mavs", "dallas"]),
    "DEN": (1610612743, "Denver Nuggets", "Nuggets", "denver-nuggets", ["nuggets", "denver"]),
    "DET": (1610612765, "Detroit Pistons", "Pistons", "detroit-pistons", ["pistons", "detroit"]),
    "GSW": (1610612744, "Golden State Warriors", "Warriors", "golden-state-warriors", ["warriors", "golden state"]),
    "HOU": (1610612745, "Houston Rockets", "Rockets", "houston-rockets", ["rockets", "houston"]),
    "IND": (1610612754, "Indiana Pacers", "Pacers", "indiana-pacers", ["pacers", "indiana"]),
    "LAC": (1610612746, "LA Clippers", "Clippers", "la-clippers", ["clippers"]),
    "LAL": (1610612747, "Los Angeles Lakers", "Lakers", "los-angeles-lakers", ["lakers"]),
    "MEM": (1610612763, "Memphis Grizzlies", "Grizzlies", "memphis-grizzlies", ["grizzlies", "grizz", "memphis"]),
    "MIA": (1610612748, "Miami Heat", "Heat", "miami-heat", ["heat", "miami"]),
    "MIL": (1610612749, "Milwaukee Bucks", "Bucks", "milwaukee-bucks", ["bucks", "milwaukee"]),
    "MIN": (1610612750, "Minnesota Timberwolves", "Timberwolves", "minnesota-timberwolves", ["timberwolves", "wolves", "minnesota"]),
    "NOP": (1610612740, "New Orleans Pelicans", "Pelicans", "new-orleans-pelicans", ["pelicans", "pels", "new orleans"]),
    "NYK": (1610612752, "New York Knicks", "Knicks", "new-york-knicks", ["knicks", "new york"]),
    "OKC": (1610612760, "Oklahoma City Thunder", "Thunder", "oklahoma-city-thunder", ["thunder", "oklahoma city", "okc"]),
    "ORL": (1610612753, "Orlando Magic", "Magic", "orlando-magic", ["magic", "orlando"]),
    "PHI": (1610612755, "Philadelphia 76ers", "76ers", "philadelphia-76ers", ["76ers", "sixers", "philadelphia", "philly"]),
    "PHX": (1610612756, "Phoenix Suns", "Suns", "phoenix-suns", ["suns", "phoenix"]),
    "POR": (1610612757, "Portland Trail Blazers", "Trail Blazers", "portland-trail-blazers", ["trail blazers", "blazers", "portland"]),
    "SAC": (1610612758, "Sacramento Kings", "Kings", "sacramento-kings", ["kings", "sacramento"]),
    "SAS": (1610612759, "San Antonio Spurs", "Spurs", "san-antonio-spurs", ["spurs", "san antonio"]),
    "TOR": (1610612761, "Toronto Raptors", "Raptors", "toronto-raptors", ["raptors", "raps", "toronto"]),
    "UTA": (1610612762, "Utah Jazz", "Jazz", "utah-jazz", ["jazz", "utah"]),
    "WAS": (1610612764, "Washington Wizards", "Wizards", "washington-wizards", ["wizards", "wiz", "washington"]),
}

# --- text matching -----------------------------------------------------------

def tokens(text):
    """Lowercase token list with punctuation flattened to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def contains_seq(haystack, needle):
    """True if the token list `needle` appears contiguously in `haystack`."""
    n, h = len(needle), len(haystack)
    if not n or n > h:
        return False
    first = needle[0]
    for i in range(h - n + 1):
        if haystack[i] == first and haystack[i:i + n] == needle:
            return True
    return False


def load_roster():
    data = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    players = data.get("players", [])
    for p in players:
        p["_full_tokens"] = tokens(p.get("full_name"))
    return players


def headshot_file(p):
    """Headshot image filename for a player (<nba_id>-<slug>.png)."""
    return p.get("headshot_filename") or f'{p["nba_id"]}-{p["slug"]}.png'


def match_question(q_tokens, players):
    """
    Return (player_slugs, team_slugs) matched in a question's tokens.

    Players are matched on FULL NAME ONLY. Last-name matching was implemented
    and tested first (with roster-level ambiguity + a common-surname blocklist),
    but every last-name-only hit on real data was a false positive: surnames
    collide with non-roster people ("Caleb Wilson" -> Jalen Wilson), team cities
    ("Washington Wizards" -> P.J. Washington) and celebrities ("Jon Stewart" ->
    Isaiah Stewart). A wrong player on a public page is worse than a miss, so we
    require the full name. Teams match on their (unambiguous) name aliases.
    """
    pl, tm = [], []
    for abbrev, (_id, _name, _nick, slug, aliases) in TEAMS.items():
        if any(contains_seq(q_tokens, tokens(a)) for a in aliases):
            tm.append(slug)
    for p in players:
        if contains_seq(q_tokens, p["_full_tokens"]):
            pl.append(p["slug"])
    return pl, tm


# --- HTML helpers ------------------------------------------------------------

def esc(s):
    return (str(s) if s is not None else "").replace("&", "&amp;").replace(
        "<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


def write_if_changed(path, content, stats):
    """Write only when the bytes differ, so unchanged pages don't churn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        stats["skipped"] += 1
        return
    path.write_text(content, encoding="utf-8")
    stats["written"] += 1


# slug -> abbrev reverse lookup for TEAMS
_SLUG_TO_ABBREV = {v[3]: k for k, v in TEAMS.items()}


def entity_page(kind, slug, name, sub, img, fallback_img, markets):
    """One entity page: static SEO shell + client-filled live table."""
    desc = (f"Live Polymarket betting odds and implied probabilities for {name} — "
            f"championship, awards, props and more, updated continuously on the "
            f"HoopsMatic NBA Polymarket tracker.")
    # static, crawlable list of the entity's markets (no volatile numbers)
    if markets:
        items = "".join(
            f'<li><a href="../../market.html?id={esc(m["conditionId"])}">'
            f'{esc(m["question"])}</a></li>' for m in markets)
        body = f'<ul class="seo-list">{items}</ul>'
    else:
        body = (f'<div class="empty">No active markets mention {esc(name)} '
                f'right now. <a href="../../index.html">Browse all markets →</a></div>')
    payload = {
        "type": kind, "slug": slug, "name": name,
        "ids": [m["conditionId"] for m in markets],
    }
    onerr = (f"this.onerror=null;this.src='{fallback_img}'"
             if fallback_img else "this.onerror=null;this.style.display='none'")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{esc(name)} NBA odds — HoopsMatic Polymarket tracker</title>\n'
        f'<meta name="description" content="{esc(desc)}">\n'
        f'<link rel="canonical" href="{SITE_BASE}/docs/{kind}/{slug}/">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<link rel="stylesheet" href="../../styles.css">\n'
        '</head>\n<body>\n<div class="container narrow">\n'
        '<a class="back" href="../../index.html">← All markets</a>\n'
        + search_box("../../") +
        '<div class="ehead">\n'
        f'<img class="eimg" src="{img}" alt="{esc(name)}" loading="lazy" '
        f'width="72" height="72" onerror="{onerr}">\n'
        f'<div><h1>{esc(name)}</h1><div class="esub">{esc(sub)}</div></div>\n'
        '</div>\n'
        f'<div id="markets">{body}</div>\n'
        + search_box("../../") +
        '<div class="foot" id="foot"></div>\n</div>\n'
        f'<script>window.ENTITY={json.dumps(payload)};</script>\n'
        '<script src="../../app.js"></script>\n'
        '<script src="../../entity.js"></script>\n'
        '<script src="../../search.js"></script>\n'
        '</body>\n</html>\n'
    )


def search_box(base):
    """Global search input markup (wired by search.js). Used top and bottom."""
    return ('<div class="gsearch-wrap"><input class="gsearch" type="text" '
            'placeholder="Search markets, players, teams…" autocomplete="off" '
            'aria-label="Search"><div class="gsearch-results"></div></div>')


def fmt_vol(v):
    """Compact USD volume, e.g. $1.2M / $48K / $930."""
    v = v or 0
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${round(v / 1000)}K"
    return f"${round(v)}"


def directory_page(kind, title, entries):
    """
    A /players/ or /teams/ ranked leaderboard — one entity per row: rank,
    headshot/logo, name, total betting volume across the entity's markets, and
    market count. Default sort is volume desc; client-side toggles re-sort by
    market count or alphabetically (and renumber the rank). Same table.lb styling
    as the standings tables for consistency. Global search top + bottom.
    """
    rows = "".join(
        f'<tr data-href="../{kind}/{esc(e["slug"])}/" data-name="{esc(e["name"].lower())}" '
        f'data-vol="{int(e["vol"])}" data-count="{e["count"]}">'
        f'<td class="rank">{i + 1}</td>'
        f'<td class="name lb-ent"><img src="{e["thumb"]}" alt="" loading="lazy" '
        f'width="28" height="28" onerror="this.style.visibility=\'hidden\'">'
        f'<span>{esc(e["name"])}</span></td>'
        f'<td class="vol">{fmt_vol(e["vol"])}</td>'
        f'<td class="pct">{e["count"]}</td></tr>'
        for i, e in enumerate(entries))
    label = "players" if kind == "player" else "teams"
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{esc(title)} by betting volume — HoopsMatic NBA Polymarket tracker</title>\n'
        f'<meta name="description" content="Every NBA {kind} ranked by total Polymarket betting volume across their markets, updated continuously.">\n'
        f'<link rel="canonical" href="{SITE_BASE}/docs/{kind}s/">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<link rel="stylesheet" href="../styles.css">\n'
        '</head>\n<body>\n<div class="container narrow">\n'
        '<div class="tabs"><a href="../index.html">Standings</a>'
        '<a href="../movers.html">Movers</a><a href="../resolved.html">Resolved</a>'
        f'<a href="../players/"{" class=active" if kind=="player" else ""}>Players</a>'
        f'<a href="../teams/"{" class=active" if kind=="team" else ""}>Teams</a></div>\n'
        + search_box("../") +
        f'<div class="hdr"><h1>{esc(title)}</h1><span class="brand">HoopsHype</span></div>\n'
        f'<div class="subtitle">Ranked by total betting volume across each {kind}’s markets.</div>\n'
        '<div class="toggle" id="esort"><button class="active" data-sort="vol">Volume</button>'
        '<button data-sort="count">Markets</button><button data-sort="name">A–Z</button></div>\n'
        '<div class="dirtable"><table class="lb"><thead><tr>'
        '<th class="rank"></th><th class="left">' + esc(title.split()[-1]) + '</th>'
        '<th>Volume</th><th>Markets</th></tr></thead>'
        f'<tbody id="lbbody">{rows}</tbody></table></div>\n'
        + search_box("../") +
        '</div>\n'
        '<script>(function(){var t=document.getElementById("esort"),g=document.getElementById("lbbody");'
        't.addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;'
        't.querySelectorAll("button").forEach(function(x){x.classList.toggle("active",x===b);});'
        'var k=b.dataset.sort,cs=[].slice.call(g.children);'
        'cs.sort(function(a,c){'
        'if(k==="name")return a.dataset.name.localeCompare(c.dataset.name);'
        'var ka=+a.dataset[k],kc=+c.dataset[k];'
        'return (kc-ka)||a.dataset.name.localeCompare(c.dataset.name);});'
        'cs.forEach(function(c,i){c.firstChild.textContent=i+1;g.appendChild(c);});});'
        'g.addEventListener("click",function(e){var r=e.target.closest("tr");'
        'if(r&&r.dataset.href)location.href=r.dataset.href;});})();</script>\n'
        '<script src="../app.js"></script>\n<script src="../search.js"></script>\n'
        '</body>\n</html>\n'
    )


def market_page(m):
    """
    A pre-generated SEO page for one market at docs/market/<slug>/. Static shell
    (title, meta description, crawlable question heading) + window.MARKET so
    market.js renders the live detail. Only the question/identity is baked in, so
    the bytes change only when the market's question changes — not when prices
    move (keeps the churn guard meaningful).
    """
    slug = m.get("slug")
    cid = m.get("conditionId")
    q = m.get("question") or "NBA market"
    ev = m.get("eventTitle") or ""
    desc = (f"Live implied probability, price history and the full field for "
            f"“{q}” on the HoopsMatic NBA Polymarket tracker.")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{esc(q)} — NBA Polymarket odds | HoopsMatic</title>\n'
        f'<meta name="description" content="{esc(desc)}">\n'
        f'<link rel="canonical" href="{SITE_BASE}/docs/market/{esc(slug)}/">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">\n'
        '<link rel="stylesheet" href="../../styles.css">\n'
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>\n'
        '</head>\n<body>\n<div class="container narrow">\n'
        '<a class="back" href="../../index.html">← All markets</a>\n'
        + search_box("../../") +
        f'<div id="app"><h1>{esc(q)}</h1>'
        f'<p class="esub">{esc(ev)}</p><div class="loading">Loading market…</div></div>\n'
        + search_box("../../") +
        '<div class="foot" id="foot"></div>\n</div>\n'
        f'<script>window.MARKET={json.dumps({"conditionId": cid})};</script>\n'
        '<script src="../../app.js"></script>\n'
        '<script src="../../market.js"></script>\n'
        '<script src="../../search.js"></script>\n'
        '</body>\n</html>\n'
    )


# --- main --------------------------------------------------------------------

def main():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    markets = index.get("markets", [])
    players = load_roster()

    players_by_slug = {p["slug"]: p for p in players}

    def player_entity(slug):
        p = players_by_slug[slug]
        return {"t": "player", "slug": p["slug"], "name": p["full_name"],
                "img": HEADSHOT_THUMB.format(file=headshot_file(p))}

    def team_entity(slug):
        tid, name, _nick, tslug, _a = TEAMS[_SLUG_TO_ABBREV[slug]]
        return {"t": "team", "slug": tslug, "name": name,
                "img": TEAM_LOGO.format(id=tid)}

    # match every market once
    by_player = {}        # slug -> [market, ...]
    by_team = {}          # slug -> [market, ...]
    market_entities = {}  # conditionId -> {primary: {..}|null, all: [{..}, ...]}
    for m in markets:
        q = tokens(m.get("question"))
        pl, tm = match_question(q, players)
        for slug in pl:
            by_player.setdefault(slug, []).append(m)
        for slug in tm:
            by_team.setdefault(slug, []).append(m)
        if not pl and not tm:
            continue
        all_ents = [player_entity(s) for s in pl] + [team_entity(s) for s in tm]
        # primary (for the row thumbnail): exactly one player, else exactly one
        # team, else none — never show a single wrong/ambiguous icon.
        if len(pl) == 1:
            primary = all_ents[0]
        elif not pl and len(tm) == 1:
            primary = all_ents[0]
        else:
            primary = None
        market_entities[m["conditionId"]] = {"primary": primary, "all": all_ents}

    stats = {"written": 0, "skipped": 0}

    def vol_sorted(ms):
        return sorted(ms, key=lambda m: (m.get("volume") or 0), reverse=True)

    def total_vol(ms):
        return sum((m.get("volume") or 0) for m in ms)

    # player pages (whole roster)
    player_dir_entries = []
    for p in players:
        ms = vol_sorted(by_player.get(p["slug"], []))
        sub = p.get("team_abbrev") or "NBA"
        img = HEADSHOT_FACE.format(file=headshot_file(p))
        raw = HEADSHOT_RAW.format(file=headshot_file(p))
        write_if_changed(DOCS_DIR / "player" / p["slug"] / "index.html",
                         entity_page("player", p["slug"], p["full_name"], sub, img, raw, ms), stats)
        player_dir_entries.append({"slug": p["slug"], "name": p["full_name"],
                                   "thumb": HEADSHOT_THUMB.format(file=headshot_file(p)),
                                   "count": len(ms), "vol": total_vol(ms)})

    # team pages (all 30)
    team_dir_entries = []
    for abbrev, (tid, name, nick, slug, _a) in TEAMS.items():
        ms = vol_sorted(by_team.get(slug, []))
        img = TEAM_LOGO.format(id=tid)
        write_if_changed(DOCS_DIR / "team" / slug / "index.html",
                         entity_page("team", slug, name, "NBA team", img, "", ms), stats)
        team_dir_entries.append({"slug": slug, "name": name,
                                 "thumb": img, "count": len(ms), "vol": total_vol(ms)})

    # directory leaderboards — default order is most betting volume first; the
    # page has client-side toggles to market count and alphabetical.
    player_dir_entries.sort(key=lambda e: (-e["vol"], -e["count"], e["name"]))
    team_dir_entries.sort(key=lambda e: (-e["vol"], -e["count"], e["name"]))
    write_if_changed(DOCS_DIR / "players" / "index.html",
                     directory_page("player", "NBA Players", player_dir_entries), stats)
    write_if_changed(DOCS_DIR / "teams" / "index.html",
                     directory_page("team", "NBA Teams", team_dir_entries), stats)

    # entities.json: per-market matches (markets) + the full roster (directory)
    # so global search resolves EVERY player/team, even ones with no current
    # markets (their pages exist and say "no active markets").
    # Sorted by NAME (stable) so this file doesn't churn when volumes shift the
    # leaderboard order each poll — search doesn't care about order.
    directory = (
        [{"t": "player", "slug": e["slug"], "name": e["name"], "img": e["thumb"]}
         for e in player_dir_entries] +
        [{"t": "team", "slug": e["slug"], "name": e["name"], "img": e["thumb"]}
         for e in team_dir_entries]
    )
    directory.sort(key=lambda x: x["name"])
    write_if_changed(ENTITIES_PATH,
                     json.dumps({"markets": market_entities, "directory": directory},
                                ensure_ascii=False, separators=(",", ":")) + "\n", stats)

    # SEO market pages (item 7) — one per live + recently-resolved market.
    market_slugs = []
    seen_slug = set()
    for m in markets:
        slug = m.get("slug")
        if not slug or not m.get("conditionId") or slug in seen_slug:
            continue
        seen_slug.add(slug)
        market_slugs.append(slug)
        write_if_changed(DOCS_DIR / "market" / slug / "index.html", market_page(m), stats)

    # sitemap.xml (urls only — no lastmod, so it stays churn-free)
    urls = [f"{SITE_BASE}/docs/index.html", f"{SITE_BASE}/docs/movers.html",
            f"{SITE_BASE}/docs/resolved.html", f"{SITE_BASE}/docs/players/",
            f"{SITE_BASE}/docs/teams/"]
    urls += [f"{SITE_BASE}/docs/player/{p['slug']}/" for p in players]
    urls += [f"{SITE_BASE}/docs/team/{t[3]}/" for t in TEAMS.values()]
    urls += [f"{SITE_BASE}/docs/market/{s}/" for s in sorted(market_slugs)]
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "".join(f"<url><loc>{u}</loc></url>\n" for u in urls)
               + "</urlset>\n")
    write_if_changed(DOCS_DIR / "sitemap.xml", sitemap, stats)

    matched_players = sum(1 for p in players if by_player.get(p["slug"]))
    matched_teams = sum(1 for s in (t[3] for t in TEAMS.values()) if by_team.get(s))
    print(f"Entities: {len(players)} players ({matched_players} with markets), "
          f"30 teams ({matched_teams} with markets), "
          f"{len(market_entities)} markets tagged, {len(market_slugs)} market pages. "
          f"Files written: {stats['written']}, unchanged: {stats['skipped']}.")


if __name__ == "__main__":
    main()
