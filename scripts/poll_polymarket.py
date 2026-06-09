"""
NBA Polymarket poller.

Runs every 10 minutes (via .github/workflows/poll.yml). On each run it:

  1. Pulls every currently-active NBA event from the Polymarket Gamma API
     (tag_id=745), paginating through all pages.
  2. For every child market inside those events, records a snapshot of the
     current state (implied probability, volume, liquidity, etc.).
  3. Appends that snapshot to the market's own history file under
     data/markets/<conditionId>.json.
  4. Compacts each history file so old points are thinned out (see the
     compaction tiers below) to keep files small forever.
  5. Detects markets that have just resolved (closed == true), writes a final
     snapshot, stamps them resolved, and freezes the file into
     data/archive/<YYYY-MM>/.
  6. Sweeps stale markets: any live file that dropped out of the active feed
     (its parent event went inactive/closed) is re-checked by event slug so a
     resolution that happened off-feed still gets archived. See
     sweep_stale_markets for the safety bounds.

Per-market files are written with metadata pretty-printed but each history
snapshot on its own compact line, so a normal poll only appends one line and
git stores almost nothing per commit.

This script does NOT build data/index.json — that is the job of build_index.py,
which the workflow runs immediately after this script.

Confirmed Polymarket API facts this script relies on (validated previously):
  - NBA filter: GET /events?tag_id=745&active=true&closed=false (paginate).
  - Futures events have series=null, so the tag is the only reliable hook.
  - A child market can be closed=true inside an active, not-closed event
    (a team/player eliminated mid-season). We snapshot every market in an
    active event regardless of the market's own closed flag.
  - outcomePrices and clobTokenIds come back as JSON-encoded STRINGS, not
    lists, so they must be json.loads()'d.
  - volume24hr is null on resolved markets; we treat null as 0.
  - The YES outcome price IS the implied probability.

Usage (normally run by CI, but works locally too):
    pip install requests
    python scripts/poll_polymarket.py
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --- Configuration ----------------------------------------------------------

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
NBA_TAG_ID = 745
PAGE_LIMIT = 100

# Sweep: how many stale events to re-check by slug per run (see
# sweep_stale_markets). Bounds the extra API calls a single run can make; any
# backlog drains over several runs, oldest-stale first.
SWEEP_MAX_EVENTS = 50

# Sweep safety valve: if the active-events feed returns markets covering less
# than this fraction of the live files we already have on disk, the feed almost
# certainly failed or came back partial. Re-checking everything would be wasted
# work against a broken feed, so we skip the sweep entirely that cycle.
SWEEP_MIN_FEED_FRACTION = 0.10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Repo layout. This file lives in <repo>/scripts/, so the repo root is its
# parent's parent.
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MARKETS_DIR = DATA_DIR / "markets"
ARCHIVE_DIR = DATA_DIR / "archive"

# Compaction tiers: for a snapshot of a given age, keep at most one point per
# this many seconds. Younger data stays dense; old data gets thinned.
#   <= 24h    -> 10-minute resolution (keep everything, we poll every 10 min)
#   1 - 7d    -> hourly
#   7 - 30d   -> every 6 hours
#   30d+      -> daily
TIER_24H = 10 * 60
TIER_7D = 60 * 60
TIER_30D = 6 * 60 * 60
TIER_OLD = 24 * 60 * 60


# --- Small helpers -----------------------------------------------------------

def iso(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC, e.g. 2026-06-08T14:30:00Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp (with trailing Z) back into a datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def to_float(value):
    """Best-effort float conversion. Returns None for empty/None/garbage."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def json_list(value):
    """Decode a JSON-encoded string (Polymarket sends lists as strings)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, list) else []
    except (TypeError, ValueError):
        return []


def http_get(url, params=None, retries=3):
    """GET JSON with a browser User-Agent and a few retries on failure."""
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - network calls fail many ways
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_error


def dumps_compact_array(head: dict, array_key: str, items: list) -> str:
    """
    Serialize a record as JSON where the identity/metadata fields (`head`) are
    pretty-printed for readability, but each object in the big list (`items`,
    stored under `array_key`) is rendered as ONE compact line.

    This keeps per-market files diff-friendly: a normal poll only appends a
    single new snapshot line and rewrites nothing else, so git stores almost
    nothing per commit. The output still round-trips cleanly through
    json.loads().
    """
    head_json = json.dumps(head, indent=2, ensure_ascii=False, default=str)
    # head_json always ends in "\n}" for a non-empty dict; splice the array in
    # just before that closing brace.
    body = head_json[:-2] if head_json.endswith("\n}") else "{"
    if items:
        rows = ",\n".join(
            json.dumps(item, ensure_ascii=False, separators=(",", ":"),
                       default=str)
            for item in items
        )
        block = f'  "{array_key}": [\n{rows}\n  ]'
    else:
        block = f'  "{array_key}": []'
    return f"{body},\n{block}\n}}\n"


def write_market_file(path: Path, record: dict):
    """Write a market record (metadata pretty, history one line per snapshot)."""
    history = record.get("history") or []
    head = {k: v for k, v in record.items() if k != "history"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dumps_compact_array(head, "history", history),
        encoding="utf-8",
    )


# --- Polymarket fetching -----------------------------------------------------

def fetch_nba_events():
    """Page through every active, not-closed NBA event."""
    events = []
    offset = 0
    while True:
        page = http_get(
            f"{GAMMA}/events",
            params={
                "tag_id": NBA_TAG_ID,
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            },
        )
        if not page:
            break
        events.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset > 10000:  # hard safety stop, should never be hit
            break
    return events


def fetch_event_by_slug(slug):
    """
    Look an event up directly by its slug. Unlike the active-events feed, this
    lookup works regardless of the event's active/closed state, so it can see
    an event (and its final market state) after the event has left the feed.

    Returns a list of event dicts (usually one) or [] on any failure.
    """
    if not slug:
        return []
    try:
        data = http_get(f"{GAMMA}/events", params={"slug": slug})
    except Exception:  # noqa: BLE001 - sweep is best-effort, never fatal
        return []
    return data if isinstance(data, list) else []


def backfill_history(clob_token_ids):
    """
    Best-effort: on the FIRST time we see a market, try to seed its history
    from the CLOB price-history endpoint so charts have some past context.

    This only fills timestamp + impliedProbability; the other snapshot fields
    are unknown for historical points and are stored as null. Returns [] on any
    failure (resolved markets in particular return nothing here).
    """
    if not clob_token_ids:
        return []
    yes_token = clob_token_ids[0]
    try:
        data = http_get(
            f"{CLOB}/prices-history",
            params={"market": yes_token, "interval": "max", "fidelity": 60},
        )
    except Exception:  # noqa: BLE001 - backfill is optional, never fatal
        return []

    points = data.get("history") or []
    snapshots = []
    for point in points:
        try:
            ts = datetime.fromtimestamp(int(point["t"]), tz=timezone.utc)
            price = float(point["p"])
        except (KeyError, TypeError, ValueError):
            continue
        snapshots.append(
            {
                "timestamp": iso(ts),
                "impliedProbability": price,
                "volume24hr": None,
                "volume": None,
                "liquidity": None,
                "spread": None,
                "oneDayPriceChange": None,
                "oneWeekPriceChange": None,
            }
        )
    return snapshots


# --- History compaction ------------------------------------------------------

def tier_granularity(age_seconds):
    """How densely to keep points of a given age (in seconds)."""
    if age_seconds <= 24 * 3600:
        return TIER_24H
    if age_seconds <= 7 * 86400:
        return TIER_7D
    if age_seconds <= 30 * 86400:
        return TIER_30D
    return TIER_OLD


def compact_history(history, now):
    """
    Thin a history list according to the compaction tiers. Within each time
    bucket we keep the most recent snapshot. The newest snapshot is always
    kept because it sits in its own 10-minute bucket.
    """
    if not history:
        return history

    ordered = sorted(history, key=lambda snap: snap["timestamp"])
    kept = {}  # bucket key -> snapshot (later one wins, since we go ascending)
    for snap in ordered:
        try:
            ts = parse_iso(snap["timestamp"])
        except (KeyError, ValueError):
            continue
        age = (now - ts).total_seconds()
        granularity = tier_granularity(age)
        bucket = (granularity, int(ts.timestamp() // granularity))
        kept[bucket] = snap

    return sorted(kept.values(), key=lambda snap: snap["timestamp"])


# --- File lookup -------------------------------------------------------------

def find_archived_file(condition_id):
    """Return the archive path for a conditionId if it is already frozen."""
    if not ARCHIVE_DIR.exists():
        return None
    for month_dir in ARCHIVE_DIR.iterdir():
        if not month_dir.is_dir():
            continue
        candidate = month_dir / f"{condition_id}.json"
        if candidate.exists():
            return candidate
    return None


def build_metadata(event, market, condition_id):
    """The per-market fields we store once, on first creation of the file."""
    return {
        "conditionId": condition_id,
        "slug": market.get("slug"),
        "question": market.get("question"),
        "eventSlug": event.get("slug"),
        "eventTitle": event.get("title"),
        "eventId": event.get("id"),
        "clobTokenIds": json_list(market.get("clobTokenIds")),
        "outcomes": json_list(market.get("outcomes")),
        "endDate": market.get("endDate"),
        "tags": event.get("tags") or [],
    }


def build_snapshot(market, now):
    """One reading of a market's current state."""
    prices = json_list(market.get("outcomePrices"))
    implied = to_float(prices[0]) if prices else None

    volume_24h = to_float(market.get("volume24hr"))
    if volume_24h is None:  # null on resolved markets -> treat as 0
        volume_24h = 0.0

    return {
        "timestamp": iso(now),
        "impliedProbability": implied,
        "volume24hr": volume_24h,
        "volume": to_float(market.get("volume")),
        "liquidity": to_float(market.get("liquidity")),
        "spread": to_float(market.get("spread")),
        "oneDayPriceChange": to_float(market.get("oneDayPriceChange")),
        "oneWeekPriceChange": to_float(market.get("oneWeekPriceChange")),
    }


# --- Per-market processing ---------------------------------------------------

def process_market(event, market, now):
    """
    Snapshot a single child market, append to its history, compact, and
    freeze it into the archive if it has just resolved.
    """
    condition_id = market.get("conditionId")
    if not condition_id:
        return

    # If this market was already resolved and archived in a previous run,
    # leave the frozen file alone.
    if find_archived_file(condition_id):
        return

    live_path = MARKETS_DIR / f"{condition_id}.json"
    is_new = not live_path.exists()

    if is_new:
        record = build_metadata(event, market, condition_id)
        record["resolved"] = False
        record["resolvedAt"] = None
        # Try to seed some past history the first time we see this market.
        record["history"] = backfill_history(record["clobTokenIds"])
    else:
        record = json.loads(live_path.read_text(encoding="utf-8"))
        record.setdefault("history", [])
        record.setdefault("resolved", False)
        record.setdefault("resolvedAt", None)

    record["history"].append(build_snapshot(market, now))
    record["history"] = compact_history(record["history"], now)

    just_resolved = bool(market.get("closed")) and not record.get("resolved")
    if just_resolved:
        record["resolved"] = True
        record["resolvedAt"] = iso(now)
        month = now.strftime("%Y-%m")
        archive_path = ARCHIVE_DIR / month / f"{condition_id}.json"
        write_market_file(archive_path, record)
        if live_path.exists():
            live_path.unlink()  # remove the live copy; it now lives in archive
    else:
        write_market_file(live_path, record)


# --- Sweep for stale markets -------------------------------------------------

def sweep_stale_markets(seen_ids, now):
    """
    Re-check live market files that fell out of the active-events feed.

    A market leaves the feed when its parent event goes inactive/closed (a game
    finishes, the championship resolves, etc.). We never see closed==true for
    it in the normal pass, so without this it would linger as "live" forever and
    keep polluting the index. Here we group such stale files by their stored
    eventSlug, look each event up directly by slug (which works regardless of
    active/closed state), and run any matching markets back through
    process_market so the ones that have resolved get a final snapshot and are
    archived.

    Defensive by design:
      - A market merely missing from one feed is NOT treated as resolved. We act
        only on what the slug lookup actually returns; failed or empty lookups
        leave files untouched.
      - At most SWEEP_MAX_EVENTS events are checked per run, oldest-stale first,
        so one run can never make an unbounded number of API calls and a backlog
        drains over successive runs.
      - If the active feed came back empty or badly partial (covering less than
        SWEEP_MIN_FEED_FRACTION of the live files on disk), the whole sweep is
        skipped that cycle, since nearly everything would look stale at once.

    Returns the number of markets re-processed.
    """
    if not MARKETS_DIR.exists():
        return 0

    market_files = list(MARKETS_DIR.glob("*.json"))

    # Safety valve: a market counts as "stale" only because it is missing from
    # this cycle's feed. If the feed itself came back empty or badly partial,
    # almost every live file would look stale at once. The sweep is still safe
    # (it re-verifies each market by slug before archiving, so the worst case is
    # wasted lookups, never a wrong archive), but re-checking 50 markets against
    # a feed that clearly failed is pointless. Skip the sweep this cycle instead.
    total_live = len(market_files)
    if not seen_ids or len(seen_ids) < SWEEP_MIN_FEED_FRACTION * total_live:
        print(
            f"Sweep skipped: active feed returned {len(seen_ids)} market(s) vs "
            f"{total_live} live file(s) on disk (feed looks empty/partial)."
        )
        return 0

    # Bucket stale live files by eventSlug, tracking each group's most recent
    # snapshot so we can prioritise the most-stale groups first.
    groups = {}  # eventSlug -> {"ids": set(conditionId), "latest": iso-or-""}
    for path in market_files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        condition_id = record.get("conditionId")
        if not condition_id or condition_id in seen_ids:
            continue  # freshly polled this run, not stale
        slug = record.get("eventSlug")
        if not slug:
            continue  # nothing to look up
        history = record.get("history") or []
        latest = max((s.get("timestamp") or "" for s in history), default="")
        group = groups.setdefault(slug, {"ids": set(), "latest": ""})
        group["ids"].add(condition_id)
        if latest > group["latest"]:
            group["latest"] = latest

    if not groups:
        return 0

    # Oldest-stale groups first; cap how many events we hit per run.
    ordered = sorted(groups.items(), key=lambda kv: kv[1]["latest"])
    swept = 0
    for slug, group in ordered[:SWEEP_MAX_EVENTS]:
        wanted = group["ids"]
        for event in fetch_event_by_slug(slug):
            for market in event.get("markets") or []:
                if market.get("conditionId") in wanted:
                    process_market(event, market, now)
                    swept += 1
    return swept


# --- Entry point -------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    events = fetch_nba_events()

    seen_ids = set()
    market_count = 0
    for event in events:
        for market in event.get("markets") or []:
            condition_id = market.get("conditionId")
            if condition_id:
                seen_ids.add(condition_id)
            process_market(event, market, now)
            market_count += 1

    swept = sweep_stale_markets(seen_ids, now)

    print(
        f"Polled {len(events)} NBA events / {market_count} markets, "
        f"swept {swept} stale market(s) at {iso(now)}"
    )


if __name__ == "__main__":
    main()
