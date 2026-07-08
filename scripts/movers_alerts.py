"""
NBA Polymarket — Slack movers alerts.

Two channels, both off the existing poll data (no new market fetching):

  --instant  (run after each poll/build_index): every LIVE market whose implied
             probability swung >= 6 points over the last 6h fires a one-off Slack
             message, at most once per market per calendar day (Europe/Madrid).
             Same prose + HoopsMatic link as the digest, worded for a 6h window.
  --digest   (run three times a day, 07:00/15:00/23:00 Madrid): the top 10 movers
             over a rolling 24-hour window, ranked by absolute swing, as
             natural-prose sentences for HoopsHype Rumors. The 24h windows overlap
             across the three daily digests (and with instant), so a move can recur
             in consecutive digests — that is expected.

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
safe to run locally. Both channels link to HoopsMatic's per-outcome market pages.

Usage:
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --instant
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --digest [--force]
        (--force, or any workflow_dispatch run, bypasses the digest slot-hour
        gate so it can be tested on demand at any hour.)
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

INSTANT_SWING = 0.06          # 6 points over 6h
INSTANT_HOURS = 6
SETTLE_LO, SETTLE_HI = 0.02, 0.98   # outside this band a move is settlement, skip
MIN_HISTORY_HOURS = 5         # need ~6h of history to judge a 6h swing
# Alert-only floors: a swing must have real money behind it to fire a Slack alert.
# These do NOT touch the display floors — the movers/standings pages still show
# everything; this only gates what's loud enough to ping Slack.
#   volume    — cumulative money traded (a market people actually care about).
#   liquidity — order-book depth. A thin book (a few hundred $) throws phantom
#               price spikes on ~no trades; requiring real depth filters those out.
ALERT_VOLUME_FLOOR = 10000
ALERT_LIQUIDITY_FLOOR = 1000

# --- digest (three daily, rolling 24-hour window) ----------------------------
DIGEST_FLOOR = 0.02           # 2.0 percentage points over the rolling 24h window
DIGEST_TOP_N = 10             # at most this many movers per digest
DIGEST_BIG_MOVE = 8           # >= this many points gets a stronger verb (surged vs risen)

# Alert links point at HoopsMatic's per-outcome market pages (per instruction:
# always HoopsMatic). HoopsMatic derives the slug from the market's question.
HOOPSMATIC_BASE = "https://hoopsmatic.com/polymarket/market"
# Our own GitHub Pages page for a market, keyed on its slug field. The poller
# builds one for every market, so it always exists — kept available as a reliable
# fallback / for the coverage-audit job, even though alerts link to HoopsMatic.
SITE_BASE = "https://jsierrahoopshype.github.io/nba-polymarket"

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
    """Alert-only floors — enough money AND a real order book behind the move to be
    worth a ping. The liquidity floor filters thin-book markets whose quoted price
    spikes on ~no trades (a $166-liquidity longshot 'jumping' to 40% and reverting)."""
    return ((m.get("volume") or 0) >= ALERT_VOLUME_FLOOR
            and (m.get("liquidity") or 0) >= ALERT_LIQUIDITY_FLOOR)


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
        msg = prose_sentence(m, start, end, cid + today,
                             "in the past 6 hours on Polymarket")
        if post_slack(msg):
            sent[cid] = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "swing": round(end - start, 4)}
            fired += 1

    save_state(state)
    print(f"Instant alerts: {fired} fired ({len(sent)} sent so far today {today}).")


# --- digest prose + links ----------------------------------------------------

def slugify(text):
    """Lowercase, delete apostrophes, then runs of non-alphanumerics -> single
    hyphen, trimmed. Matches the slug HoopsMatic derives from a market's question
    (apostrophes deleted, not hyphenated: "LeBron's" -> "lebrons")."""
    s = (text or "").lower().replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", "-", s)


def hoopsmatic_url(m):
    """Per-outcome HoopsMatic page, from the market's question. Primary alert link."""
    return f"{HOOPSMATIC_BASE}/{slugify(m.get('question'))}/"


def market_url(m):
    """Our own GitHub Pages page, keyed on the market's slug field (the exact slug
    the poller builds each page under), so it always exists. Not used for alert
    links (which go to HoopsMatic) — kept as a reliable fallback / for the audit."""
    return f"{SITE_BASE}/docs/market/{m.get('slug')}/"


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


# Past participles, rendered as "have <participle>" for the rolling-24h framing
# ("have fallen from ..."). Direction- and size-specific.
_UP_BIG = ["surged", "jumped"]
_UP_SMALL = ["risen", "climbed"]
_DOWN_BIG = ["tumbled", "fallen"]
_DOWN_SMALL = ["slipped", "eased"]


def move_verb(delta_pts, key):
    """Direction- and size-appropriate past participle, varied deterministically
    per market+slot (rendered as 'have <participle>')."""
    if delta_pts >= 0:
        pool = _UP_BIG if delta_pts >= DIGEST_BIG_MOVE else _UP_SMALL
    else:
        pool = _DOWN_BIG if abs(delta_pts) >= DIGEST_BIG_MOVE else _DOWN_SMALL
    return pool[int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16) % len(pool)]


def _article(n):
    """'a' vs 'an' for a spoken number: 'an' before 8, 11, 18 and 80-89 (eight /
    eleven / eighteen / eighty- start with a vowel sound); 'a' otherwise."""
    return "an" if str(n).startswith(("8", "11", "18")) else "a"


def prose_sentence(m, start_p, end_p, key, window):
    """Shared HoopsHype-Rumors prose + HoopsMatic link, used by BOTH the digest and
    the instant channel. Identical style; only `window` differs (the digest passes
    a 24h phrase, instant a 6h one). Direction lives in the verb (surged/slipped),
    so the tail is a directionless magnitude: ', a N-point move.' — always shown."""
    subject, outcome = subject_outcome(m.get("question"))
    a, b = round(start_p * 100, 1), round(end_p * 100, 1)
    delta = b - a
    verb = move_verb(delta, key)
    head = possessive(subject)
    if outcome:
        core = f"{head} odds of {outcome} have {verb} from {a:.1f}% to {b:.1f}% {window}"
    else:
        core = f"{head} odds have {verb} from {a:.1f}% to {b:.1f}% {window}"
    # Accurate magnitude: the swing is over the shown 1-decimal odds (b - a), so a
    # 57.0 -> 66.5 move is 9.5 points, not a rounded "10". Show one decimal when the
    # swing isn't a whole number; drop the ".0" when it is.
    mag = f"{abs(delta):.1f}".removesuffix(".0")
    tail = f", {_article(mag)} {mag}-point move."
    return core + tail + "\n" + hoopsmatic_url(m)


# --- digest swing window -----------------------------------------------------

def swing_last_24h(condition_id, cutoff_utc):
    """(baseline_prob, latest_prob) over a rolling 24h window, or None.

    Baseline = the snapshot closest in time to cutoff_utc (now - 24h); end = the
    latest snapshot. For a market with >= 24h of history the baseline is the point
    nearest the 24h mark (history older than 24h is thinned to ~hourly, so it lands
    within ~an hour). For a market younger than 24h the closest point to the cutoff
    is simply its earliest, so the window gracefully shrinks to the market's life
    rather than dropping it. None when there are < 2 usable snapshots."""
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
    baseline = min(pts, key=lambda tp: abs((tp[0] - cutoff_utc).total_seconds()))[1]
    return baseline, pts[-1][1]


def current_slot(now_madrid):
    """The digest slot whose window contains the current Madrid hour, or (None, None)."""
    for slot_id, hours, label in DIGEST_SLOTS:
        if now_madrid.hour in hours:
            return slot_id, label
    return None, None


def run_digest(force=False):
    now_madrid = datetime.now(MADRID)
    today = now_madrid.strftime("%Y-%m-%d")
    slot_id, slot_label = current_slot(now_madrid)

    # Debug line so a run's log states definitively whether force was active and
    # why (see main() for the --force / GITHUB_EVENT_NAME detection).
    print(f"Digest: force={force}, GITHUB_EVENT_NAME={os.environ.get('GITHUB_EVENT_NAME')!r}, "
          f"argv={sys.argv[1:]}, Madrid={now_madrid:%H:%M} (slot={slot_id}).")

    # per-day slot record; leave the instant channel's state untouched
    state = load_state()
    digest = state.get("digest3") or {}
    if digest.get("date") != today:
        digest = {"date": today, "slots": []}
    state["digest3"] = digest
    state.setdefault("instant_sent", {})

    # A manual workflow_dispatch (force) bypasses the slot-hour gate and the
    # once-per-slot dedup so the digest can be tested on demand at any hour. A
    # forced run posts but records no slot, so it never consumes a scheduled slot
    # and can be repeated. Scheduled crons still respect both gates.
    if force:
        slot_id, slot_label = "manual", f"manual {now_madrid:%H:%M}"
    elif not slot_id:
        print(f"Digest: {now_madrid:%H:%M} Madrid is outside the 07/15/23 windows, skipping.")
        save_state(state)
        return
    elif slot_id in digest["slots"]:
        print(f"Digest: {slot_label} slot already sent for {today}, skipping.")
        return

    cutoff_utc = now_madrid.astimezone(timezone.utc) - timedelta(hours=24)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    movers = []
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or not m.get("negRisk") or m.get("resolved") or not loud_enough(m):
            continue
        sw = swing_last_24h(cid, cutoff_utc)
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
        header = f"*NBA Polymarket — biggest moves · last 24h ({today}, {slot_label} Madrid)*"
        blocks = [header] + [
            prose_sentence(m, start_p, end_p, m.get("conditionId", "") + today + slot_id,
                           "over the past 24 hours on Polymarket")
            for m, start_p, end_p, _ in movers
        ]
        post_slack("\n\n".join(blocks))

    if not force:                                  # forced runs record no slot
        digest["slots"].append(slot_id)
        save_state(state)
    print(f"Digest: {len(movers)} movers posted for {today} {slot_label} slot.")


def main():
    args = sys.argv[1:]
    mode = args[0] if args else ""
    # Force the digest past its slot-hour gate on a manual run. Two independent
    # triggers: an explicit --force arg, or GitHub setting GITHUB_EVENT_NAME to
    # "workflow_dispatch" (set automatically on every manual-dispatch step).
    force = "--force" in args or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if mode == "--instant":
        run_instant()
    elif mode == "--digest":
        run_digest(force=force)
    else:
        print("usage: movers_alerts.py --instant | --digest [--force]", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
