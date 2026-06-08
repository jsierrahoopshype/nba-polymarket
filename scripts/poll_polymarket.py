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


def write_json(path: Path, obj):
    """Write pretty JSON, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False, default=str),
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
        write_json(archive_path, record)
        if live_path.exists():
            live_path.unlink()  # remove the live copy; it now lives in archive
    else:
        write_json(live_path, record)


# --- Entry point -------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    events = fetch_nba_events()

    market_count = 0
    for event in events:
        for market in event.get("markets") or []:
            process_market(event, market, now)
            market_count += 1

    print(
        f"Polled {len(events)} NBA events / {market_count} markets "
        f"at {iso(now)}"
    )


if __name__ == "__main__":
    main()
