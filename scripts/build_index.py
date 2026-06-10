"""
NBA Polymarket index builder.

Reads every per-market history file and writes a single summary file,
data/index.json, that the frontend pages load. It is deliberately standalone:
the poller (poll_polymarket.py) writes the raw history files, and this script
turns them into a compact, display-ready index. The workflow runs the poller
first, then this script.

What it reads:
  - All live markets:      data/markets/*.json
  - The two most recent calendar months of the archive:
                           data/archive/<YYYY-MM>/*.json
    (two months always covers the 7-day "recently resolved" window, even
    across a month boundary.)

Index policy (locked):
  - Live markets: always included.
  - Recently resolved markets (resolvedAt within the last 7 days): included
    with resolved=true and resolvedAt set.
  - Older resolved markets: NOT included (they remain in the archive only).

Each index entry carries the current numbers, a 24h and 7d delta, and a
time-distributed sparkline (at most 30 points spread across the last 7 days).
Live markets are sorted above resolved ones.

Output shape (data/index.json):
  {
    "lastUpdated": "2026-06-08T14:30:00Z",
    "count": 123,
    "markets": [ <entry>, ... ]
  }

The file is written with the top-level fields pretty-printed and each market
entry on its own compact line (see render_index). Tags are trimmed to just
id/label/slug. Both choices keep this every-cycle full rewrite small; the
frontend JSON.parses the file, so the formatting is invisible to it.

Usage:
    python scripts/build_index.py
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
MARKETS_DIR = DATA_DIR / "markets"
ARCHIVE_DIR = DATA_DIR / "archive"
INDEX_PATH = DATA_DIR / "index.json"
RESOLVED_PATH = DATA_DIR / "resolved.json"

RECENTLY_RESOLVED_DAYS = 7
SPARKLINE_MAX_POINTS = 30
SPARKLINE_WINDOW_DAYS = 7


# --- Small helpers -----------------------------------------------------------

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def trim_tags(tags):
    """
    Keep only the fields the UI needs (id, label, slug). The raw tag objects
    from Polymarket carry ~11 fields each; index.json is rebuilt and committed
    in full every cycle, so dropping the unused fields is a large, repeated
    saving in commit size.
    """
    trimmed = []
    for tag in tags or []:
        if isinstance(tag, dict):
            trimmed.append({
                "id": tag.get("id"),
                "label": tag.get("label"),
                "slug": tag.get("slug"),
            })
    return trimmed


def render_index(index):
    """
    Serialize index.json with the top-level fields pretty-printed but each
    market entry on its own compact line. index.json is regenerated wholesale
    every cycle (sparklines shift, so git cannot delta it), which makes its raw
    byte size the dominant churn cost; compacting each entry (plus trimming
    tags) cuts that size by roughly 60%. Whitespace is irrelevant to the
    frontend, which
    JSON.parses the file. Output round-trips through json.loads().
    """
    markets = index.get("markets") or []
    head = {k: v for k, v in index.items() if k != "markets"}
    head_json = json.dumps(head, indent=2, ensure_ascii=False, default=str)
    body = head_json[:-2] if head_json.endswith("\n}") else "{"
    if markets:
        rows = ",\n".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":"),
                       default=str)
            for entry in markets
        )
        block = f'  "markets": [\n{rows}\n  ]'
    else:
        block = '  "markets": []'
    return f"{body},\n{block}\n}}\n"


def recent_archive_months(now):
    """The current and previous calendar month as 'YYYY-MM' strings."""
    current = now.strftime("%Y-%m")
    first_of_month = now.replace(day=1)
    previous = (first_of_month - timedelta(days=1)).strftime("%Y-%m")
    return [current, previous]


def load_market_files(now):
    """Yield parsed records from live markets plus the last two archive months."""
    records = []

    if MARKETS_DIR.exists():
        for path in MARKETS_DIR.glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue

    for month in recent_archive_months(now):
        month_dir = ARCHIVE_DIR / month
        if not month_dir.exists():
            continue
        for path in month_dir.glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue

    return records


# --- Derived values per market ----------------------------------------------

def price_points(history):
    """Sorted (datetime, probability) pairs with a usable probability."""
    points = []
    for snap in history:
        prob = snap.get("impliedProbability")
        ts = snap.get("timestamp")
        if prob is None or ts is None:
            continue
        try:
            points.append((parse_iso(ts), float(prob)))
        except ValueError:
            continue
    points.sort(key=lambda pair: pair[0])
    return points


def latest_snapshot(history):
    """The most recent snapshot by timestamp, or None."""
    valid = [s for s in history if s.get("timestamp")]
    if not valid:
        return None
    return max(valid, key=lambda s: s["timestamp"])


def delta_over(points, now, hours, fallback):
    """
    Change in implied probability over the given number of hours, computed
    from our own snapshots. If we don't yet have a snapshot older than the
    window, fall back to the API-provided change for that period.
    """
    if not points:
        return fallback
    cutoff = now - timedelta(hours=hours)
    before = [p for (t, p) in points if t <= cutoff]
    if not before:
        if fallback is not None:
            return round(fallback, 4)
        base = points[0][1]
    else:
        base = before[-1]
    return round(points[-1][1] - base, 4)


def build_sparkline(points, now):
    """
    At most SPARKLINE_MAX_POINTS points spread evenly across the last
    SPARKLINE_WINDOW_DAYS days. Each point is {"t": iso, "p": probability}.
    """
    cutoff = now - timedelta(days=SPARKLINE_WINDOW_DAYS)
    window = [(t, p) for (t, p) in points if t >= cutoff]
    if not window:
        return []

    if len(window) <= SPARKLINE_MAX_POINTS:
        return [{"t": iso(t), "p": round(p, 4)} for (t, p) in window]

    start = window[0][0].timestamp()
    end = window[-1][0].timestamp()
    span = (end - start) or 1.0
    bucket_size = span / SPARKLINE_MAX_POINTS

    chosen = {}  # bucket index -> (t, p); later point in a bucket wins
    for (t, p) in window:
        idx = int((t.timestamp() - start) / bucket_size)
        if idx >= SPARKLINE_MAX_POINTS:
            idx = SPARKLINE_MAX_POINTS - 1
        chosen[idx] = (t, p)

    return [
        {"t": iso(t), "p": round(p, 4)}
        for (t, p) in (chosen[i] for i in sorted(chosen))
    ]


def build_entry(record, now):
    """Turn one stored market record into a compact index entry."""
    history = record.get("history") or []
    points = price_points(history)
    latest = latest_snapshot(history) or {}

    return {
        "conditionId": record.get("conditionId"),
        "slug": record.get("slug"),
        "question": record.get("question"),
        "eventSlug": record.get("eventSlug"),
        "eventTitle": record.get("eventTitle"),
        "eventId": record.get("eventId"),
        "endDate": record.get("endDate"),
        "negRisk": record.get("negRisk"),
        "tags": trim_tags(record.get("tags")),
        "impliedProbability": latest.get("impliedProbability"),
        "volume24hr": latest.get("volume24hr"),
        "volume": latest.get("volume"),
        "liquidity": latest.get("liquidity"),
        "delta24h": delta_over(points, now, 24, latest.get("oneDayPriceChange")),
        "delta7d": delta_over(points, now, 24 * 7, latest.get("oneWeekPriceChange")),
        "sparkline": build_sparkline(points, now),
        "resolved": bool(record.get("resolved")),
        "resolvedAt": record.get("resolvedAt"),
    }


# --- Inclusion + ordering ----------------------------------------------------

def is_recently_resolved(record, now):
    resolved_at = record.get("resolvedAt")
    if not resolved_at:
        return False
    try:
        when = parse_iso(resolved_at)
    except ValueError:
        return False
    return (now - when) <= timedelta(days=RECENTLY_RESOLVED_DAYS)


def sort_key(entry):
    """Live markets first (by 24h volume desc), then resolved (newest first)."""
    if not entry["resolved"]:
        volume = entry.get("volume24hr") or 0
        return (0, -volume, "")
    # Resolved: sort after live, most recently resolved first.
    return (1, 0, entry.get("resolvedAt") or "")


def build_resolved_entry(record):
    """
    A slim entry for the resolved/archive directory page. The full history (and
    so the chart) still lives in the per-market archive file; this is just the
    listing row, so it deliberately omits the sparkline to stay small.
    """
    latest = latest_snapshot(record.get("history") or []) or {}
    final = latest.get("impliedProbability")
    outcome = None
    if final is not None:
        outcome = "Yes" if final >= 0.5 else "No"
    return {
        "conditionId": record.get("conditionId"),
        "slug": record.get("slug"),
        "question": record.get("question"),
        "eventSlug": record.get("eventSlug"),
        "eventTitle": record.get("eventTitle"),
        "eventId": record.get("eventId"),
        "tags": trim_tags(record.get("tags")),
        "resolvedAt": record.get("resolvedAt"),
        "finalProbability": final,
        "outcome": outcome,
        "volume": latest.get("volume"),
    }


# --- Entry point -------------------------------------------------------------

def main():
    now = datetime.now(timezone.utc)
    records = load_market_files(now)

    entries = []
    for record in records:
        if record.get("resolved"):
            if not is_recently_resolved(record, now):
                continue  # older resolved markets stay in the archive only
        entries.append(build_entry(record, now))

    # Live first (volume desc); resolved last (most recent first).
    live = sorted(
        (e for e in entries if not e["resolved"]),
        key=sort_key,
    )
    resolved = sorted(
        (e for e in entries if e["resolved"]),
        key=lambda e: e.get("resolvedAt") or "",
        reverse=True,
    )
    markets = live + resolved

    index = {
        "lastUpdated": iso(now),
        "count": len(markets),
        "markets": markets,
    }

    # resolved.json: the directory for docs/resolved.html. Unlike the index
    # (which only keeps the last 7 days of resolved markets), this lists every
    # resolved market load_market_files saw across the last two archive months.
    resolved_records = [r for r in records if r.get("resolved")]
    resolved_entries = sorted(
        (build_resolved_entry(r) for r in resolved_records),
        key=lambda e: e.get("resolvedAt") or "",
        reverse=True,
    )
    resolved_manifest = {
        "lastUpdated": iso(now),
        "count": len(resolved_entries),
        "markets": resolved_entries,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(render_index(index), encoding="utf-8")
    RESOLVED_PATH.write_text(render_index(resolved_manifest), encoding="utf-8")
    print(f"Wrote {INDEX_PATH} with {len(markets)} markets ({len(live)} live, "
          f"{len(resolved)} recently resolved). "
          f"Wrote {RESOLVED_PATH} with {len(resolved_entries)} archived.")


if __name__ == "__main__":
    main()
