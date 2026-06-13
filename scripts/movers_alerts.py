"""
NBA Polymarket — Slack movers alerts.

Two channels, both off the existing poll data (no new market fetching):

  --instant  (run after each poll/build_index): every LIVE market whose implied
             probability swung >= 6 points over the last 6h fires a one-off Slack
             message, at most once per market per calendar day (Europe/Madrid).
  --digest   (run by the digest workflow ~09:00 Madrid): every LIVE market with a
             24h swing >= 3 points, ranked by swing desc, in one message — minus
             any market already sent as an instant alert today.

LIVE only: resolved markets are excluded, and so are markets sitting at a
near-settled price (a move to ~100/0 is settlement, not a swing worth alerting).

Dedup/calendar state lives in data/alerts_state.json:
  { "date": "YYYY-MM-DD", "instant_sent": { conditionId: {ts, swing} },
    "digest_sent_for": "YYYY-MM-DD" }
instant_sent resets when the Madrid date rolls over.

The Slack webhook is read from $SLACK_MOVERS_WEBHOOK (a GitHub secret). With no
webhook set the script runs in dry-run mode and prints the messages, so it is
safe to run locally. Links point at our own /docs/market/<slug>/ pages, never at
polymarket.com.

Usage:
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --instant
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --digest
"""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    MADRID = ZoneInfo("Europe/Madrid")
except Exception:                                  # pragma: no cover - tzdata missing
    MADRID = timezone(timedelta(hours=2))          # CEST fallback; note in PR

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INDEX_PATH = DATA_DIR / "index.json"
MARKETS_DIR = DATA_DIR / "markets"
STATE_PATH = DATA_DIR / "alerts_state.json"

SITE_BASE = "https://jsierrahoopshype.github.io/nba-polymarket"
GAME_TAG = "100639"

INSTANT_SWING = 0.06          # 6 points over 6h
DIGEST_SWING = 0.03           # 3 points over 24h
INSTANT_HOURS = 6
SETTLE_LO, SETTLE_HI = 0.02, 0.98   # outside this band a move is settlement, skip
MIN_HISTORY_HOURS = 5         # need ~6h of history to judge a 6h swing


# --- time / state ------------------------------------------------------------

def madrid_today():
    return datetime.now(MADRID).strftime("%Y-%m-%d")


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ensure_today(state, today):
    """Roll the per-day instant dedup set when the Madrid date changes."""
    if state.get("date") != today:
        state["date"] = today
        state["instant_sent"] = {}
    state.setdefault("instant_sent", {})
    state.setdefault("digest_sent_for", None)
    return state


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --- slack -------------------------------------------------------------------

def post_slack(text):
    """POST a message to the Slack webhook; dry-run (print) when unset."""
    url = os.environ.get("SLACK_MOVERS_WEBHOOK")
    if not url:
        print("[dry-run] " + text + "\n" + ("-" * 40))
        return True
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        return True
    except Exception as exc:  # noqa: BLE001 - network can fail many ways
        print("Slack post failed:", exc, file=sys.stderr)
        return False


# --- formatting --------------------------------------------------------------

def pct(p):
    return round((p or 0) * 100)


def fmt_vol(v):
    v = v or 0
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${round(v / 1000)}K"
    return f"${round(v)}"


def display_title(m):
    """Readable question; game markets get the game date appended to disambiguate."""
    q = (m.get("question") or "this market").strip()
    tags = m.get("tags") or []
    if any(str(t.get("id")) == GAME_TAG for t in tags) and m.get("endDate"):
        try:
            d = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).astimezone(MADRID)
            q = f"{q} ({d.strftime('%b %-d')})"
        except (ValueError, TypeError):
            pass
    return q


def market_url(m):
    return f"{SITE_BASE}/docs/market/{m.get('slug')}/"


# Sentence templates. Direction-specific verbs (never mismatch up/down). No
# em-dashes, no filler verbs, one idea each. {window} is "6 hours" / "24 hours".
UP_TEMPLATES = [
    "Per Polymarket, {market}'s odds have climbed from {a}% to {b}% over the last {window}.",
    "Polymarket money is moving toward {market}: up to {b}% from {a}% in the last {window}.",
    "{market} has jumped {delta} points on Polymarket in the last {window}, now at {b}%.",
    "Bettors are piling into {market} on Polymarket, up {delta} points to {b}% in the last {window}.",
    "{market} surged from {a}% to {b}% on Polymarket over the last {window}.",
    "Polymarket now puts {market} at {b}%, up from {a}% in the last {window}.",
    "{market} has risen to {b}% from {a}% on Polymarket in the last {window}.",
]
DOWN_TEMPLATES = [
    "Per Polymarket, {market}'s odds have slipped from {a}% to {b}% over the last {window}.",
    "Polymarket money is leaving {market}: down to {b}% from {a}% in the last {window}.",
    "{market} has dropped {delta} points on Polymarket in the last {window}, now at {b}%.",
    "Bettors are cooling on {market}, down {delta} points to {b}% in the last {window}.",
    "{market} faded from {a}% to {b}% on Polymarket over the last {window}.",
    "Polymarket now puts {market} at {b}%, down from {a}% in the last {window}.",
    "{market} has fallen to {b}% from {a}% on Polymarket in the last {window}.",
]


def sentence(m, start, end, today, window_label):
    """Pick a template deterministically (varied) by hashing conditionId+date."""
    up = end >= start
    pool = UP_TEMPLATES if up else DOWN_TEMPLATES
    key = (m.get("conditionId", "") + today).encode("utf-8")
    idx = int(hashlib.md5(key).hexdigest(), 16) % len(pool)
    return pool[idx].format(
        market=display_title(m), a=pct(start), b=pct(end),
        delta=abs(pct(end) - pct(start)), window=window_label)


def alert_text(m, start, end, today, window_label):
    line = sentence(m, start, end, today, window_label)
    line += f" Volume {fmt_vol(m.get('volume'))}."
    return line + "\n" + market_url(m)


# --- swings ------------------------------------------------------------------

def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def swing_6h(condition_id):
    """
    (start_prob, end_prob) over the last 6h from a live market's history file, or
    None when there isn't enough history (so a brand-new market can't false-fire).
    """
    path = MARKETS_DIR / f"{condition_id}.json"
    try:
        hist = json.loads(path.read_text(encoding="utf-8")).get("history") or []
    except (OSError, ValueError):
        return None
    pts = []
    for s in hist:
        p = s.get("impliedProbability")
        ts = s.get("timestamp")
        if p is None or not ts:
            continue
        try:
            pts.append((parse_ts(ts), float(p)))
        except (ValueError, TypeError):
            continue
    if len(pts) < 2:
        return None
    pts.sort(key=lambda x: x[0])
    last_t, last_p = pts[-1]
    if (last_t - pts[0][0]) < timedelta(hours=MIN_HISTORY_HOURS):
        return None                                # not enough history for a 6h read
    cutoff = last_t - timedelta(hours=INSTANT_HOURS)
    base = None
    for t, p in pts:
        if t <= cutoff:
            base = p
    if base is None:
        base = pts[0][1]
    return base, last_p


def is_live_swingable(m):
    """Live, has a usable price, and not sitting at a near-settled extreme."""
    if m.get("resolved"):
        return False
    p = m.get("impliedProbability")
    return isinstance(p, (int, float)) and SETTLE_LO <= p <= SETTLE_HI


def artificial_swing(start, m):
    """
    A swing that BEGINS at the 0.50 listing default on a market with ~no volume
    is the market initializing, not money moving — alerting "money is moving" on
    a $0 market would be false. This extends the same artificial-move guard as
    the settle filter; it is NOT a volume floor (any market with real volume and
    a genuine prior still qualifies).
    """
    return abs((start or 0) - 0.5) < 0.025 and (m.get("volume") or 0) < 1


# --- modes -------------------------------------------------------------------

def run_instant():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    today = madrid_today()
    state = ensure_today(load_state(), today)
    sent = state["instant_sent"]

    fired = 0
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or cid in sent or not is_live_swingable(m):
            continue
        sw = swing_6h(cid)
        if not sw:
            continue
        start, end = sw
        if abs(end - start) < INSTANT_SWING or artificial_swing(start, m):
            continue
        if post_slack(alert_text(m, start, end, today, "6 hours")):
            sent[cid] = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "swing": round(end - start, 4)}
            fired += 1

    save_state(state)
    print(f"Instant alerts: {fired} fired ({len(sent)} sent so far today {today}).")


def run_digest():
    today = madrid_today()
    now_madrid = datetime.now(MADRID)
    state = ensure_today(load_state(), today)

    # only post in the morning window, and at most once per day
    if now_madrid.hour < 9:
        print(f"Digest: {now_madrid:%H:%M} Madrid is before 09:00, skipping.")
        save_state(state)
        return
    if state.get("digest_sent_for") == today:
        print(f"Digest: already sent for {today}, skipping.")
        return

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    sent = state["instant_sent"]
    movers = []
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or cid in sent or not is_live_swingable(m):
            continue                               # suppress already-instant-alerted
        d = m.get("delta24h")
        if not isinstance(d, (int, float)) or abs(d) < DIGEST_SWING:
            continue
        if artificial_swing(m.get("impliedProbability") - d, m):
            continue                               # skip 0.50-default initialization noise
        movers.append((m, d))
    movers.sort(key=lambda x: abs(x[1]), reverse=True)

    if movers:
        lines = [f"*NBA Polymarket — biggest 24h moves ({today})*"]
        for m, d in movers:
            end = m.get("impliedProbability")
            start = end - d
            arrow = "▲" if d >= 0 else "▼"
            lines.append(
                f"{arrow} {display_title(m)}  {pct(start)}% → {pct(end)}% "
                f"({'+' if d >= 0 else ''}{pct(d)} pts, Vol {fmt_vol(m.get('volume'))})\n"
                f"{market_url(m)}")
        post_slack("\n".join(lines))

    state["digest_sent_for"] = today
    save_state(state)
    print(f"Digest: {len(movers)} movers posted for {today}.")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--instant":
        run_instant()
    elif mode == "--digest":
        run_digest()
    else:
        print("usage: movers_alerts.py --instant | --digest", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
