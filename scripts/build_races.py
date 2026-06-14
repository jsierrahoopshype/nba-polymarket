"""
Bar-chart-race data builder.

Runs in the poll workflow after build_index / build_entities. For each
multi-outcome negRisk event worth animating it emits a long-format file the
video tool consumes:

    data/race/<event-slug>.json   ->  [ {date, player, value, team?}, ... ]

and an index of the available races:

    data/race/index.json          ->  { "races": [ {slug, title, outcomeCount,
                                          dateRange:[first,last], lastUpdated} ] }

Definitions:
  - player : the entity label from data/entities.json (the name the bar-race
             headshots are keyed on). We reuse that exact mapping — NO new
             fuzzy matching here.
  - value  : the outcome's implied probability on that date, normalized to 100%
             across the event's qualifying live outcomes that day (the same
             negRisk normalization the site's race chart uses).
  - date   : one row per outcome per UTC day, resampled to that day's CLOSE
             (the last snapshot of the day). The in-progress (today) UTC day is
             excluded, so a day's value is final once written — which also keeps
             these files from churning every poll.
  - team   : the outcome's team (abbrev) for bar coloring, when derivable.

Selection: a live negRisk event qualifies if it has >= MIN_OUTCOMES outcomes
clearing the $10K volume floor (placeholder/init outcomes excluded), and either
carries a "named" tag (Champion / Finals MVP / MVP / Awards) or its qualifying
volume clears EVENT_VOL_FLOOR. Up to CAP outcomes (top by current probability)
are emitted; the rest are dropped but still count toward the daily denominator.

Usage:
    python scripts/build_races.py
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the team table from the entity builder (no new matching logic).
from build_entities import TEAMS

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MARKETS_DIR = DATA_DIR / "markets"
INDEX_PATH = DATA_DIR / "index.json"
ENTITIES_PATH = DATA_DIR / "entities.json"
RACE_DIR = DATA_DIR / "race"
ROSTER_PATH = Path(__file__).resolve().parent / "vendor" / "roster_players.json"

OUTCOME_VOL_FLOOR = 10000      # an outcome must clear this to be in a race
EVENT_VOL_FLOOR = 50000        # un-named events need this much qualifying volume
MIN_OUTCOMES = 3              # fewer than this is not a race
CAP = 12                     # emit at most this many bars (top by current prob)
NAMED_TAGS = {"102288", "104582", "707", "18"}   # champion, finals mvp, mvp, awards

SLUG_TO_ABBREV = {v[3]: k for k, v in TEAMS.items()}


# --- helpers -----------------------------------------------------------------

def write_if_changed(path, content, stats):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        stats["skipped"] += 1
        return
    path.write_text(content, encoding="utf-8")
    stats["written"] += 1


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def is_placeholder(m):
    """~0 volume, pinned at the 0.50 default, no movement — init noise."""
    v = m.get("volume")
    return ((not isinstance(v, (int, float)) or v < 1)
            and isinstance(m.get("impliedProbability"), (int, float))
            and abs(m["impliedProbability"] - 0.5) < 0.005
            and (not isinstance(m.get("delta24h"), (int, float)) or abs(m["delta24h"]) < 0.005))


def qualifies_outcome(m, ent):
    """Live, real volume, not a placeholder, and has a single primary entity."""
    if m.get("resolved") or not isinstance(m.get("impliedProbability"), (int, float)):
        return False
    if (m.get("volume") or 0) < OUTCOME_VOL_FLOOR or is_placeholder(m):
        return False
    return bool(ent and ent.get("primary"))


def daily_closes(condition_id, last_day):
    """{date_str: prob} = the last snapshot's probability for each UTC day, up to
    and including last_day (the most recent COMPLETE UTC day)."""
    path = MARKETS_DIR / f"{condition_id}.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8")).get("history") or []
    except (OSError, ValueError):
        return {}
    by_day = {}
    for s in hist:
        p = s.get("impliedProbability")
        ts = s.get("timestamp")
        if p is None or not ts:
            continue
        try:
            d = parse_ts(ts).astimezone(timezone.utc).date()
        except (ValueError, TypeError):
            continue
        if d.isoformat() > last_day:
            continue                       # skip the in-progress day
        by_day[d.isoformat()] = float(p)   # later snapshot in the day wins
    return by_day


def team_abbrev(primary, roster_team):
    if primary["t"] == "team":
        return SLUG_TO_ABBREV.get(primary["slug"])
    return roster_team.get(primary["slug"])


def date_range(a, b):
    """Inclusive list of YYYY-MM-DD strings from a..b."""
    out, d = [], datetime.fromisoformat(a).date()
    end = datetime.fromisoformat(b).date()
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# --- main --------------------------------------------------------------------

def main():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    entmap = json.loads(ENTITIES_PATH.read_text(encoding="utf-8")).get("markets", {})
    roster_team = {p["slug"]: p.get("team_abbrev")
                   for p in json.loads(ROSTER_PATH.read_text(encoding="utf-8")).get("players", [])}

    last_day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    # group qualifying live negRisk outcomes by event
    events = defaultdict(list)            # eventSlug -> [(market, entity), ...]
    event_named = defaultdict(bool)
    for m in index.get("markets", []):
        if not m.get("negRisk") or not m.get("eventSlug"):
            continue
        ent = entmap.get(m.get("conditionId"))
        if any(str(t.get("id")) in NAMED_TAGS for t in (m.get("tags") or [])):
            event_named[m["eventSlug"]] = True
        if qualifies_outcome(m, ent):
            events[m["eventSlug"]].append((m, ent))

    stats = {"written": 0, "skipped": 0}
    races = []

    for slug, outcomes in events.items():
        qvol = sum((m.get("volume") or 0) for m, _ in outcomes)
        if len(outcomes) < MIN_OUTCOMES:
            continue
        if not event_named[slug] and qvol < EVENT_VOL_FLOOR:
            continue

        # daily close series per outcome
        series = []   # (market, entity, {date: prob})
        for m, ent in outcomes:
            closes = daily_closes(m["conditionId"], last_day)
            if closes:
                series.append((m, ent, closes))
        if len(series) < MIN_OUTCOMES:
            continue

        # forward-fill each outcome across its active span (first day -> last_day)
        all_dates = set()
        for _, _, closes in series:
            all_dates.update(closes)
        if not all_dates:
            continue
        first = min(all_dates)
        span = date_range(first, last_day)
        filled = []   # (market, entity, {date: prob}) forward-filled within its life
        for m, ent, closes in series:
            start = min(closes)
            ff, last_v = {}, None
            for d in span:
                if d < start:
                    continue
                if d in closes:
                    last_v = closes[d]
                if last_v is not None:
                    ff[d] = last_v
            filled.append((m, ent, ff))

        # cap to the top CAP outcomes by current (latest) probability
        def current(ff):
            return ff.get(max(ff)) if ff else 0
        filled.sort(key=lambda x: current(x[2]), reverse=True)
        shown = filled[:CAP]

        # per-day normalization denominator over ALL qualifying outcomes (the
        # field), so each emitted bar is its true share of the live field
        day_total = defaultdict(float)
        for _, _, ff in filled:
            for d, p in ff.items():
                day_total[d] += p

        rows = []
        title = outcomes[0][0].get("eventTitle") or slug
        for m, ent, ff in shown:
            primary = ent["primary"]
            player = primary["name"]
            team = team_abbrev(primary, roster_team)
            for d in span:
                p = ff.get(d)
                tot = day_total.get(d)
                if p is None or not tot:
                    continue
                row = {"date": d, "player": player, "value": round(p / tot * 100, 2)}
                if team:
                    row["team"] = team
                rows.append(row)
        if not rows:
            continue
        rows.sort(key=lambda r: (r["date"], r["player"]))

        write_if_changed(RACE_DIR / f"{slug}.json",
                         json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n", stats)
        dates = [r["date"] for r in rows]
        races.append({"slug": slug, "title": title, "outcomeCount": len(shown),
                      "dateRange": [min(dates), max(dates)], "lastUpdated": max(dates)})

    races.sort(key=lambda r: r["slug"])
    write_if_changed(RACE_DIR / "index.json",
                     json.dumps({"races": races}, ensure_ascii=False, indent=2) + "\n", stats)

    print(f"Races: {len(races)} events emitted. "
          f"Files written: {stats['written']}, unchanged: {stats['skipped']}.")


if __name__ == "__main__":
    main()
