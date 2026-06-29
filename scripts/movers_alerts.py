"""
NBA Polymarket — Slack movers alerts.

Two channels, both off the existing poll data (no new market fetching):

  --instant  (run after each poll/build_index): every LIVE market whose implied
             probability swung >= 6 points over the last 6h fires a one-off Slack
             message, at most once per market per calendar day (Europe/Madrid).
  --digest   (run three times a day, 07:00/15:00/23:00 Madrid): the day's top 10
             movers, cumulative since Madrid-midnight, ranked by absolute swing,
             as natural-prose sentences for HoopsHype Rumors. A market may recur
             across the three digests (and alongside instant) — each digest is a
             fresh cumulative snapshot, so that repetition is intended.

The digest is gated on negRisk + unresolved + ALERT_VOLUME_FLOOR and a 2.0-point
floor (a quiet day showing one or two lines is correct). Resolved markets are
excluded; the instant channel additionally skips near-settled prices.

Dedup/calendar state lives in data/alerts_state.json:
  { "date": "YYYY-MM-DD", "instant_sent": { conditionId: {ts, swing} },
    "digest3": { "date": "YYYY-MM-DD", "slots": ["morning", ...] } }
instant_sent resets when the Madrid date rolls over; digest3.slots records which
of the day's three digest windows have already gone out.

The Slack webhook is read from $SLACK_MOVERS_WEBHOOK (a GitHub secret). With no
webhook set the script runs in dry-run mode and prints the messages, so it is
safe to run locally. Instant links point at our own /docs/market/<slug>/ pages;
the digest links point at HoopsMatic's per-outcome market pages.

Usage:
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --instant
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --digest
"""

import hashlib
import json
import os
import re
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
INSTANT_HOURS = 6
SETTLE_LO, SETTLE_HI = 0.02, 0.98   # outside this band a move is settlement, skip
MIN_HISTORY_HOURS = 5         # need ~6h of history to judge a 6h swing
# Alert-only volume floor: a swing must have real money behind it to fire a Slack
# alert. This does NOT touch the display floors — the movers/standings pages still
# show everything; this only gates what's loud enough to ping Slack.
ALERT_VOLUME_FLOOR = 10000

# --- digest (three daily, cumulative since Madrid-midnight) -------------------
DIGEST_FLOOR = 0.02           # 2.0 percentage points cumulative since midnight
DIGEST_TOP_N = 10             # at most this many movers per digest
DIGEST_BIG_MOVE = 8           # >= this many points gets a verb upgrade + "N-point move"
HOOPSMATIC_BASE = "https://hoopsmatic.com/polymarket/market"

# Madrid local-hour -> digest slot. The workflow fires UTC cron pairs (05/06,
# 13/14, 21/22) that bracket DST so exactly one fire lands on each target Madrid
# hour; the second hour in a set is the fallback when GitHub drops the first fire.
DIGEST_SLOTS = [
    ("morning",   {7, 8},   "07:00"),
    ("afternoon", {15, 16}, "15:00"),
    ("night",     {23},     "23:00"),
]


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


def loud_enough(m):
    """Alert-only volume floor — enough money behind the move to be worth a ping."""
    return (m.get("volume") or 0) >= ALERT_VOLUME_FLOOR


# --- modes -------------------------------------------------------------------

def run_instant():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    today = madrid_today()
    state = ensure_today(load_state(), today)
    sent = state["instant_sent"]

    fired = 0
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or cid in sent or not is_live_swingable(m) or not loud_enough(m):
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


# --- digest prose + links ----------------------------------------------------

def slugify(text):
    """Lowercase, runs of non-alphanumerics -> single hyphen, trimmed. Matches the
    slug HoopsMatic derives from a market's question (verified against live pages)."""
    s = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return re.sub(r"\s+", "-", s)


def hoopsmatic_url(m):
    """Per-outcome HoopsMatic page, from the market's question (NOT its slug field,
    which carries a dedup timestamp suffix that would 404)."""
    return f"{HOOPSMATIC_BASE}/{slugify(m.get('question'))}/"


# "Will <subject> <predicate>?" -> "<subject>'s odds of <gerund predicate> ...".
_GERUND = {
    "play": "playing", "win": "winning", "be": "being", "been": "being",
    "lead": "leading", "record": "recording", "sign": "signing", "make": "making",
    "finish": "finishing", "score": "scoring", "average": "averaging",
    "get": "getting", "reach": "reaching", "return": "returning", "start": "starting",
    "miss": "missing", "have": "having", "hit": "hitting", "name": "naming",
    "go": "going", "retire": "retiring",
}
_PREDICATE = (r"\b(be|been|win|wins|play|plays|lead|leads|record|records|sign|signs|"
              r"make|makes|finish|finishes|score|scores|average|averages|get|gets|"
              r"reach|reaches|return|returns|start|starts|miss|misses|have|has|hit|"
              r"hits|name|named|go|goes|retire|retires)\b")
# Polymarket questions case some nouns inconsistently ("NBA draft" vs "NBA Draft").
# Normalize the basketball nouns to one canonical casing in the prose.
_CASE_FIX = {"draft": "Draft", "finals": "Finals", "champion": "Champion",
             "champions": "Champions", "playoffs": "Playoffs", "mvp": "MVP"}


def _normalize_case(text):
    return re.sub(r"[A-Za-z]+",
                  lambda w: _CASE_FIX.get(w.group(0).lower(), w.group(0)), text)


def subject_outcome(question):
    """'Will [the] <subject> <predicate>?' -> (subject, gerund outcome). Returns
    (subject, None) when no predicate verb is found (then phrase as bare odds)."""
    s = re.sub(r"^\s*will\s+", "", (question or "").strip(), flags=re.I).rstrip("?. ").strip()
    m = re.search(_PREDICATE, s, flags=re.I)
    if not m:
        return _normalize_case(s), None
    subject = re.sub(r"^the\s+", "", s[:m.start()].strip(), flags=re.I).strip()
    parts = s[m.start():].strip().split(" ", 1)
    gerund = _GERUND.get(parts[0].lower(), parts[0].lower() + "ing")
    outcome = gerund + ((" " + parts[1]) if len(parts) > 1 else "")
    return _normalize_case(subject), _normalize_case(outcome)


def possessive(name):
    return name + ("'" if name.endswith("s") else "'s")


_UP_BIG = ["surged", "jumped"]
_UP_SMALL = ["rose", "climbed"]
_DOWN_BIG = ["tumbled", "fell"]
_DOWN_SMALL = ["slipped", "eased"]


def move_verb(delta_pts, key):
    """Direction- and size-appropriate verb, varied deterministically per market+slot."""
    if delta_pts >= 0:
        pool = _UP_BIG if delta_pts >= DIGEST_BIG_MOVE else _UP_SMALL
    else:
        pool = _DOWN_BIG if abs(delta_pts) >= DIGEST_BIG_MOVE else _DOWN_SMALL
    return pool[int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % len(pool)]


def digest_sentence(m, start_p, end_p, key):
    subject, outcome = subject_outcome(m.get("question"))
    a, b = round(start_p * 100, 1), round(end_p * 100, 1)
    delta = b - a
    verb = move_verb(delta, key)
    head = possessive(subject)
    if outcome:
        core = (f"{head} odds of {outcome} {verb} from "
                f"{a:.1f}% to {b:.1f}% on Polymarket today")
    else:
        core = f"{head} odds {verb} from {a:.1f}% to {b:.1f}% on Polymarket today"
    tail = f", a {round(abs(delta))}-point move." if abs(delta) >= DIGEST_BIG_MOVE else "."
    return core + tail + "\n" + hoopsmatic_url(m)


# --- digest swing window -----------------------------------------------------

def madrid_midnight_utc(now_madrid):
    """The UTC instant of the most recent Madrid 00:00 (start of today, Madrid)."""
    midnight = now_madrid.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def swing_since_midnight(condition_id, midnight_utc):
    """(baseline_prob, latest_prob) cumulative since Madrid-midnight, or None.

    Baseline = the standing odds at midnight (last snapshot at/before it), or the
    first snapshot after midnight for a market that only started trading today.
    Returns None when there is no usable history, or none of it lands today (a
    market that hasn't updated since midnight is stale and skipped)."""
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
    if pts[-1][0] < midnight_utc:                  # nothing since midnight -> stale
        return None
    baseline = None
    for t, p in pts:
        if t <= midnight_utc:
            baseline = p                           # last reading at/before midnight wins
    if baseline is None:
        baseline = next(p for t, p in pts if t > midnight_utc)
    return baseline, pts[-1][1]


def current_slot(now_madrid):
    """The digest slot whose window contains the current Madrid hour, or (None, None)."""
    for slot_id, hours, label in DIGEST_SLOTS:
        if now_madrid.hour in hours:
            return slot_id, label
    return None, None


def run_digest():
    now_madrid = datetime.now(MADRID)
    today = now_madrid.strftime("%Y-%m-%d")
    slot_id, slot_label = current_slot(now_madrid)

    # per-day slot record; leave the instant channel's state untouched
    state = load_state()
    digest = state.get("digest3") or {}
    if digest.get("date") != today:
        digest = {"date": today, "slots": []}
    state["digest3"] = digest
    state.setdefault("instant_sent", {})

    if not slot_id:
        print(f"Digest: {now_madrid:%H:%M} Madrid is outside the 07/15/23 windows, skipping.")
        save_state(state)
        return
    if slot_id in digest["slots"]:
        print(f"Digest: {slot_label} slot already sent for {today}, skipping.")
        return

    midnight_utc = madrid_midnight_utc(now_madrid)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    movers = []
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or not m.get("negRisk") or m.get("resolved") or not loud_enough(m):
            continue
        sw = swing_since_midnight(cid, midnight_utc)
        if not sw:
            continue
        start_p, end_p = sw
        if artificial_swing(start_p, m):
            continue                               # skip 0.50-default initialization noise
        if abs(end_p - start_p) < DIGEST_FLOOR:
            continue
        movers.append((m, start_p, end_p, abs(end_p - start_p)))

    movers.sort(key=lambda x: x[3], reverse=True)
    movers = movers[:DIGEST_TOP_N]

    if movers:
        header = f"*NBA Polymarket — biggest moves so far today ({today}, {slot_label} Madrid)*"
        blocks = [header] + [
            digest_sentence(m, start_p, end_p, m.get("conditionId", "") + today + slot_id)
            for m, start_p, end_p, _ in movers
        ]
        post_slack("\n\n".join(blocks))

    digest["slots"].append(slot_id)
    save_state(state)
    print(f"Digest: {len(movers)} movers posted for {today} {slot_label} slot.")


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
