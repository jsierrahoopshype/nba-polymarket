"""
NBA Polymarket — Slack movers alerts.

Two channels, both off the existing poll data (no new market fetching):

  --instant  (run after each poll/build_index): every LIVE market whose implied
             probability swung >= its category's instant threshold (awards 8,
             futures 5, other 4 points; see CATEGORY_THRESHOLDS) over the last 6h
             fires a one-off plain-text Slack message (subject to the
             cooldown below), with the HoopsMatic URL alone on the line under it.
  --digest   (run three times a day, 07:00/15:00/23:00 Madrid): the top 10 movers
             over a rolling 24-hour window, ranked by absolute swing, as
             natural-prose sentences for HoopsHype Rumors. The 24h windows overlap
             across the three daily digests (and with instant), so a move can recur
             in consecutive digests — that is expected.

--instant posts ONLY on odds moves: a market's 6h OR 24h swing must clear its
category's instant threshold (the larger qualifying window is used and named in
the wording; one shared cooldown means a move posts once, not once per window). Nothing else can trigger a message on its own:
  * Volume never triggers an alert. When the odds have already moved past the
    threshold, the market's 24h volume can appear as a short supporting clause.
  * New favorite (negRisk event) and 25% / 50% / 75% crossings are tracked in
    data/alerts_tracker.json and only show up as a supporting clause on a
    threshold move ("... crossing the 50% mark").
  * New listings are still recorded in data/seen_markets.json but never posted.
Messages are plain text, grouped one Slack message per event per run, and each
market's HoopsMatic URL sits alone on the line under its sentence.
Repetition control for every channel: one alert per market per COOLDOWN_HOURS
unless it moves 2x the market's category threshold for that channel again
(state: alerts_state.last_alert),
and the digest keeps only the biggest mover per event.

The digest is gated on negRisk + unresolved + ALERT_VOLUME_FLOOR and a per-category
24h floor (awards 4, futures/other 2 points; a quiet day showing one or two lines is
correct, and a slot with no market clearing its floor posts nothing). Resolved markets are
excluded; the instant channel additionally skips near-settled prices.

Dedup/calendar state lives in data/alerts_state.json:
  { "date": "YYYY-MM-DD", "instant_sent": { conditionId: {ts, swing} },
    "digest3": { "sent": ["YYYY-MM-DD-<slot>", ...],
                 "last_slot": "<ISO-UTC>", "last_warn": "YYYY-MM-DD" } }
instant_sent resets when the Madrid date rolls over; digest3.sent is a bounded
rolling list of the slot-instances already posted (so a run is deduped even when
GitHub fires its cron hours late or past midnight); last_slot/last_warn drive the
staleness tripwire (see digest_healthcheck).

The Slack webhook is read from $SLACK_MOVERS_WEBHOOK (a GitHub secret). With no
webhook set the script runs in dry-run mode and prints the messages, so it is
safe to run locally. Both channels link to HoopsMatic's per-outcome market pages.

Usage:
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --instant
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --digest [--force]
        (--force, or any workflow_dispatch run, bypasses the digest slot-hour
        gate so it can be tested on demand at any hour.)
    SLACK_MOVERS_WEBHOOK=... python scripts/movers_alerts.py --digest-healthcheck
        (staleness tripwire, run from the poll workflow; warns if the digest has
        gone quiet, catching a digest workflow that stops firing entirely.)
"""

import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
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
# Committed by the poll workflow (it already `git add data`):
SEEN_PATH = DATA_DIR / "seen_markets.json"        # every market ID seen, for "New market"
TRACKER_PATH = DATA_DIR / "alerts_tracker.json"   # last 25/50/75 band + event favorite

# =============================================================================
# PER-CATEGORY ALERT THRESHOLDS (edit here)
# -----------------------------------------------------------------------------
# Minimum move, in probability points (0.04 = 4 points), a market needs to make
#   "digest"  — the three-daily 24h digest
#   "instant" — the 6h instant swing alert
# The "re-alert early" cooldown override (COOLDOWN_OVERRIDE_MULT, 2x) also uses
# the market's own category value for the channel it's alerting on.
# A market's category comes from its event title (else its question), see
# market_category(). Anything matching no category uses "other".
CATEGORY_THRESHOLDS = {
    # MVP, ROY, DPOY, Sixth Man, MIP, COY, Clutch Player, stat leaders,
    # All-NBA / All-Defensive / All-Rookie / All-Star selections
    "awards":  {"digest": 0.04, "instant": 0.08},
    # Champion, conference champion, playoffs, play-in, seeding, win totals
    "futures": {"digest": 0.02, "instant": 0.05},
    # Trade, next-team and everything else
    "other":   {"digest": 0.02, "instant": 0.04},
}
# Title patterns, checked in this order (first match wins), case-insensitive.
CATEGORY_PATTERNS = [
    ("awards", r"\bMVP\b|Most Valuable Player|Rookie of the Year|\bROY\b|"
               r"Defensive Player of the Year|\bDPOY\b|Sixth Man|6th Man|"
               r"Most Improved|\bMIP\b|Coach of the Year|\bCOY\b|Clutch Player|"
               # stat leaders (points/rebounds/assists/steals/blocks/3PM/EFF/PER ... Leader)
               r"\bLeaders?\b|"
               # All-NBA / All-Defensive / All-Rookie team and All-Star selections
               r"All-NBA|All-Defensive|All-Rookie|All-Stars?\b|"
               # stat thresholds ("Players to Average 25+ PPG / 10+ RPG ...")
               r"\bto average\b"),
    ("futures", r"\bchampions?\b|NBA Finals|\bFinals\b|playoffs?|play-in|\bseed(s|ing)?\b|"
                r"win totals?|to win \d+\+? games"),
]
# =============================================================================

# (6h instant swing threshold is per category: CATEGORY_THRESHOLDS above.)
INSTANT_HOURS = 6
# Second instant window: a slow drift over a day, same per-category thresholds.
# The shared cooldown covers both windows, so a move posts once, never twice.
INSTANT_DAY_HOURS = 24
DAY_BASELINE_TOLERANCE_HOURS = 3   # 24h baseline must sit within 3h of the 24h mark
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

# Slack-only mute list: market types listed here never post a Slack alert
# (instant or digest). The website and market pages are unaffected. Types come
# from slack_market_type(), all single-game (full game and first half):
# "moneyline" ("Pistons vs. Wizards"), "spreads" ("Spread: Raptors (-4.5)"),
# "totals" ("Clippers vs. Raptors: O/U 219.5"), "player_props" ("Jalen Brunson:
# Points O/U 26.5" in a game event), "game_props" (any other market in a game
# event: "Heat vs. Magic: Team to Score First", "...: Odd/Even Score").
# Futures and awards have no type and always alert.
SLACK_MUTED_MARKET_TYPES = ["moneyline", "spreads", "totals", "player_props", "game_props"]

# --- repetition control (all channels) ---------------------------------------
# A market alerts at most once per COOLDOWN_HOURS (instant and digest all share one record), unless its price has moved at least
# COOLDOWN_OVERRIDE_MULT x the market's own category threshold for that channel
# since the last alert (e.g. awards instant 8pt -> 16pt, awards digest 4pt -> 8pt).
COOLDOWN_HOURS = 24
COOLDOWN_OVERRIDE_MULT = 2
INSTANT_MAX_MESSAGES = 12     # flood cap per poll run (one message per event)
EVENT_MAX_LINES = 3           # max market lines inside one event's message

# --- new markets --------------------------------------------------------------
# A market not seen before is announced once its cumulative volume reaches this
# floor, if that happens within NEW_MARKET_WINDOW_DAYS of first sighting.
NEW_MARKET_VOLUME_FLOOR = 5000
NEW_MARKET_WINDOW_DAYS = 7
SEEN_PRUNE_DAYS = 60          # forget IDs gone from the index this long

# --- flips --------------------------------------------------------------------
CROSS_LEVELS = (0.25, 0.50, 0.75)
CROSS_HYSTERESIS = 0.015      # must clear a line by 1.5pt to count as crossed
FLIP_MARGIN = 0.015           # new favorite must lead the old one by 1.5pt
FLIP_MIN_PROB = 0.10          # ignore favorite changes among 5% longshots
FLIP_CANDIDATE_VOLUME = 1000  # outcome needs this much traded to count as favorite

# --- supporting volume clause (never a trigger) -----------------------------
# On an alert that already cleared its odds threshold, mention 24h volume when at
# least this much traded. Volume on its own never sends a message.
VOLUME_CLAUSE_MIN_24H = 10000

# --- digest (three daily, rolling 24-hour window) ----------------------------
# (24h digest floor is per category: CATEGORY_THRESHOLDS above.)
DIGEST_TOP_N = 10             # at most this many movers per digest
DIGEST_BIG_MOVE = 8           # >= this many points gets a stronger verb (surged vs risen)
DIGEST_STALE_HOURS = 36       # tripwire: warn if no slot has been processed in this long
# Through this date only, a 0-mover slot posts a one-line "markets quiet" note
# (with the actual biggest 24h move) instead of staying silent — useful while the
# pre-season market is flat. Self-expires: after it, 0-mover slots go silent again.
QUIET_LINE_UNTIL = date(2026, 10, 15)

# Alert links point at HoopsMatic's per-outcome market pages (per instruction:
# always HoopsMatic). HoopsMatic derives the slug from the market's question.
HOOPSMATIC_BASE = "https://hoopsmatic.com/polymarket/market"
# Our own GitHub Pages page for a market, keyed on its slug field. The poller
# builds one for every market, so it always exists — kept available as a reliable
# fallback / for the coverage-audit job, even though alerts link to HoopsMatic.
SITE_BASE = "https://jsierrahoopshype.github.io/nba-polymarket"

# Digest slots as (id, target Madrid hour, label). A run is attributed to the
# most recent slot whose target hour has already passed, and that slot stays
# claimable until the NEXT slot's target -- so a cron delayed by GitHub's
# scheduler (routinely 1-4h, sometimes past midnight) still posts the right slot
# instead of falling outside a narrow hour window and skipping. See due_slot().
DIGEST_SLOTS = [
    ("morning",   7,  "07:00"),
    ("afternoon", 15, "15:00"),
    ("night",     23, "23:00"),
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


def swing_24h(condition_id):
    """(start_prob, end_prob) over the last 24h, or None when the market lacks a
    snapshot near the 24h mark (a younger market can't pass off a shorter window
    as "the past day"). Baseline = latest snapshot at or before now-24h."""
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
    cutoff = last_t - timedelta(hours=INSTANT_DAY_HOURS)
    before = [(t, p) for t, p in pts if t <= cutoff]
    if not before or cutoff - before[-1][0] > timedelta(hours=DAY_BASELINE_TOLERANCE_HOURS):
        return None
    return before[-1][1], last_p


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


# Game-winner title: exactly "<Team> vs. <Team>" with no colon suffix, so the
# other per-game markets ("...: O/U 219.5", "...: Team to Score First",
# "...: Odd/Even Score") don't match.
_MONEYLINE_TITLE = re.compile(r"^[^:]+ vs\. [^:]+$")
# Any per-game market title: "<Team> vs. <Team>: <something>".
_GAME_PREFIX = re.compile(r"^[^:]+ vs\. [^:]+: ")


def _single_game(m, q):
    """True when the market belongs to one specific game: its title is
    "<Team> vs. <Team>[: ...]", or its Polymarket event is a game ("Thunder vs.
    Hawks") or carries the "games" tag. Futures/awards events ("NBA: 2027
    Champion", "NBA: 2026-27 Rookie of the Year") match none of these."""
    if _MONEYLINE_TITLE.match(q) or _GAME_PREFIX.match(q):
        return True
    if _MONEYLINE_TITLE.match((m.get("eventTitle") or "").strip()):
        return True
    return any(isinstance(t, dict) and t.get("slug") == "games" for t in m.get("tags") or [])


def slack_market_type(m):
    """Market type for the Slack mute list, or None for everything else (futures,
    awards, season-long markets). Our stored data has no Polymarket
    sportsMarketType field, so this reads the question title (plus the event
    title / tags to tell a single-game market from a season-long one)."""
    q = (m.get("question") or "").strip()
    if q.startswith(("Spread:", "1H Spread:")):    # full-game and first-half spreads
        return "spreads"
    if not _single_game(m, q):
        return None                                # futures, awards, draft O/U, season win totals...
    if "O/U" in q:                                 # full-game and first-half game totals vs player lines
        return "totals" if _GAME_PREFIX.match(q) else "player_props"
    if _MONEYLINE_TITLE.match(q) or q.endswith(": 1H Moneyline"):   # full-game and first-half winner
        return "moneyline"
    return "game_props"                            # Team to Score First, Odd/Even Score, ...


def slack_muted(m):
    return slack_market_type(m) in SLACK_MUTED_MARKET_TYPES


# --- per-category thresholds ---------------------------------------------------

_CATEGORY_RES = [(name, re.compile(pat, re.I)) for name, pat in CATEGORY_PATTERNS]


def _match_category(text):
    for name, rx in _CATEGORY_RES:
        if text and rx.search(text):
            return name
    return None


def market_category(m):
    """"awards" | "futures" | "other", from the event title, falling back to the
    market question when the event title matches nothing."""
    return (_match_category((m.get("eventTitle") or "").strip())
            or _match_category((m.get("question") or "").strip())
            or "other")


def category_threshold(m, channel):
    """The market's own threshold for channel "digest" or "instant"."""
    t = CATEGORY_THRESHOLDS.get(market_category(m)) or CATEGORY_THRESHOLDS["other"]
    return t[channel]


def clears(delta, threshold):
    """|delta| >= threshold, compared at 0.01-point precision so a float like
    0.0799999 for an 8.0-point move still counts as 8 points."""
    return round(abs(delta), 4) >= round(threshold, 4)


# --- shared cooldown (cuts repetition across every channel) --------------------

def _utc_now():
    return datetime.now(timezone.utc)


def _iso_utc(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def event_key(m):
    """Grouping key for "one alert per event": Polymarket event id, else slug,
    else the market itself (a standalone market is its own event)."""
    return str(m.get("eventId") or m.get("eventSlug") or m.get("conditionId") or "")


def last_alerts(state):
    """Per-market record of the latest Slack alert (any channel):
    {conditionId: {"ts": ISO-UTC, "p": prob at alert}}. Pruned past 48h."""
    la = state.get("last_alert")
    if not isinstance(la, dict):
        la = {}
    cutoff = _utc_now() - timedelta(hours=48)
    for cid in [c for c, v in la.items()
                if not isinstance(v, dict) or _parse_or_epoch(v.get("ts")) < cutoff]:
        la.pop(cid, None)
    state["last_alert"] = la
    return la


def _parse_or_epoch(ts):
    try:
        return parse_ts(ts)
    except (TypeError, ValueError, AttributeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def cooldown_ok(la, cid, prob, threshold, now=None):
    """One alert per market per COOLDOWN_HOURS, unless the price has moved at least
    COOLDOWN_OVERRIDE_MULT x the channel's normal threshold since that alert."""
    rec = la.get(cid)
    if not rec:
        return True
    now = now or _utc_now()
    if now - _parse_or_epoch(rec.get("ts")) >= timedelta(hours=COOLDOWN_HOURS):
        return True
    last_p = rec.get("p")
    if not isinstance(last_p, (int, float)) or not isinstance(prob, (int, float)):
        return False
    return clears(prob - last_p, COOLDOWN_OVERRIDE_MULT * threshold)


def mark_alerted(la, cid, prob, now=None):
    la[cid] = {"ts": _iso_utc(now or _utc_now()),
               "p": round(prob, 4) if isinstance(prob, (int, float)) else None}


# --- tracker files (seen markets, crossing bands, event favorites) -------------

def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_maps(path, head, maps):
    """Write {head..., name: {k: v}, ...} with one map entry per line (sorted), so
    a poll that touches a few markets rewrites a few lines and git stays small.
    Round-trips through json.loads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in head.items()]
    for name, mapping in maps.items():
        rows = ",\n".join(f"    {json.dumps(k)}: {json.dumps(mapping[k], separators=(',', ':'))}"
                          for k in sorted(mapping))
        parts.append(f"  {json.dumps(name)}: {{\n{rows}\n  }}" if rows else f"  {json.dumps(name)}: {{}}")
    path.write_text("{\n" + ",\n".join(parts) + "\n}\n", encoding="utf-8")


def _save_map_file(path, head, map_key, mapping):
    _save_maps(path, head, {map_key: mapping})


def alertable_universe(index):
    """Live, unresolved, non-muted NBA markets (the existing single-game mute rule
    is what excludes game winner / spreads / totals / props)."""
    return [m for m in index.get("markets", [])
            if m.get("conditionId") and not m.get("resolved") and not slack_muted(m)]


def crossing_band(old_band, p):
    """Which 25/50/75 band a price sits in, with CROSS_HYSTERESIS so a price
    wobbling on a line doesn't flip back and forth. Band k = k thresholds passed."""
    raw = sum(p >= t for t in CROSS_LEVELS)
    if old_band is None:
        return raw
    if raw > old_band:
        for k in range(raw, old_band, -1):            # highest band cleanly passed
            if p >= CROSS_LEVELS[k - 1] + CROSS_HYSTERESIS:
                return k
    elif raw < old_band:
        for k in range(raw, old_band):                 # lowest band cleanly dropped to
            if p <= CROSS_LEVELS[k] - CROSS_HYSTERESIS:
                return k
    return old_band


def _money(x):
    if x >= 1_000_000:
        return f"${x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"${x / 1_000:.0f}K"
    return f"${x:.0f}"


# Team nicknames, so "Denver Nuggets" reads as "the Nuggets" in instant alerts.
_TEAM_NICKNAMES = ("Hawks", "Celtics", "Nets", "Hornets", "Bulls", "Cavaliers", "Mavericks",
                   "Nuggets", "Pistons", "Warriors", "Rockets", "Pacers", "Clippers", "Lakers",
                   "Grizzlies", "Heat", "Bucks", "Timberwolves", "Pelicans", "Knicks", "Thunder",
                   "Magic", "76ers", "Suns", "Trail Blazers", "Kings", "Spurs", "Raptors",
                   "Jazz", "Wizards")


def _short_subject(subject):
    """'Denver Nuggets' -> 'the Nuggets'; players and anything else unchanged."""
    for nick in _TEAM_NICKNAMES:
        if subject == nick or subject.endswith(" " + nick):
            return "the " + nick
    return subject


# Simple-past verbs for instant alerts, by direction and size.
_I_UP_BIG = ["jumped", "surged", "shot up"]
_I_UP_SMALL = ["rose", "climbed", "moved up"]
_I_DOWN_BIG = ["dropped", "tumbled", "sank"]
_I_DOWN_SMALL = ["slipped", "dipped", "fell"]
_I_WINDOWS = ["in the past {h} hours", "over the last {h} hours"]
_I_WINDOWS_DAY = ["in the past day", "over the past 24 hours"]


def _pick(pool, key, salt):
    return pool[int(hashlib.md5((key + salt).encode("utf-8")).hexdigest(), 16) % len(pool)]


def instant_sentence(m, start_p, end_p, key, hours=INSTANT_HOURS, clauses=()):
    """Plain-text instant alert: leads with the move, gives before -> now and the
    window, then the HoopsMatic URL alone on the next line. Wording (verb, window
    phrase, sentence shape) varies deterministically per market+day via `key`.
    `clauses` are short supporting phrases (crossing, new favorite, volume)."""
    subject, outcome = subject_outcome(m.get("question"))
    who = possessive(_short_subject(subject))
    odds = f"{who} odds of {outcome}" if outcome else f"{who} odds"
    a, b = round(start_p * 100, 1), round(end_p * 100, 1)
    delta = b - a
    if delta >= 0:
        verb = _pick(_I_UP_BIG if delta >= DIGEST_BIG_MOVE else _I_UP_SMALL, key, "v")
    else:
        verb = _pick(_I_DOWN_BIG if -delta >= DIGEST_BIG_MOVE else _I_DOWN_SMALL, key, "v")
    pool = _I_WINDOWS_DAY if hours == INSTANT_DAY_HOURS else _I_WINDOWS
    window = _pick(pool, key, "w").format(h=hours)
    mag = f"{abs(delta):.1f}".removesuffix(".0")
    pts = "point" if mag == "1" else "points"
    shapes = [
        f"{odds} {verb} from {a:.1f}% to {b:.1f}% {window}",
        f"{odds} {verb} {mag} {pts} {window}, from {a:.1f}% to {b:.1f}%",
    ]
    text = _pick(shapes, key, "s")
    text = text[0].upper() + text[1:]
    text += "".join(", " + c for c in clauses if c) + "."
    return text + "\n" + hoopsmatic_url(m)


def volume_clause(m, hours=INSTANT_HOURS):
    vol = m.get("volume24hr")
    if isinstance(vol, (int, float)) and vol >= VOLUME_CLAUSE_MIN_24H:
        if hours == INSTANT_DAY_HOURS:             # window already says "past day"
            return f"with {_money(vol)} traded"
        return f"with {_money(vol)} traded in the past day"
    return None


def crossing_clause(old_band, new_band):
    if old_band is None or new_band == old_band:
        return None
    if new_band > old_band:
        return f"crossing above {CROSS_LEVELS[new_band - 1] * 100:.0f}%"
    return f"dropping below {CROSS_LEVELS[new_band] * 100:.0f}%"


# --- modes -------------------------------------------------------------------

def check_new_markets(universe, now):
    """New-market detection against data/seen_markets.json.

    File shape: {"bootstrapped": ISO, "markets": {cid: {"first": ISO, "ann": 0|1}}}.
    First run (file missing) records every current market as already announced and
    posts nothing. Afterwards a market not seen before is recorded with ann=0 and
    is announced once its cumulative volume reaches NEW_MARKET_VOLUME_FLOOR, as long
    as that happens within NEW_MARKET_WINDOW_DAYS of first sighting (older ones are
    retired silently). Returns {event_key: [markets to announce]}."""
    data = _load_json(SEEN_PATH)
    stamp = _iso_utc(now)
    if not isinstance(data, dict) or not isinstance(data.get("markets"), dict):
        seen = {m["conditionId"]: {"first": stamp, "ann": 1} for m in universe}
        _save_map_file(SEEN_PATH, {"bootstrapped": stamp}, "markets", seen)
        print(f"New markets: bootstrapped {len(seen)} market(s) silently.")
        return {}
    seen = data["markets"]

    for m in universe:
        seen.setdefault(m["conditionId"], {"first": stamp, "ann": 0})

    by_event = {}
    for m in universe:
        by_event.setdefault(event_key(m), []).append(m)

    announce = {}
    window = timedelta(days=NEW_MARKET_WINDOW_DAYS)
    for ek, ms in by_event.items():
        pending = [m for m in ms if not seen[m["conditionId"]].get("ann")]
        if not pending:
            continue
        fresh = [m for m in pending
                 if now - _parse_or_epoch(seen[m["conditionId"]].get("first")) <= window]
        if not fresh:
            for m in pending:                      # too old to be "new": retire silently
                seen[m["conditionId"]]["ann"] = 1
            continue
        if any((m.get("volume") or 0) >= NEW_MARKET_VOLUME_FLOOR for m in fresh):
            announce[ek] = fresh
            # Mark all pending outcomes of this event announced, so the rest of a
            # newly listed event doesn't trickle in one alert at a time later.
            for m in pending:
                seen[m["conditionId"]]["ann"] = 1

    # Prune markets gone from the index for a long time (resolved and archived).
    live_ids = {m["conditionId"] for m in universe}
    old = now - timedelta(days=SEEN_PRUNE_DAYS)
    for cid in [c for c, v in seen.items()
                if c not in live_ids and _parse_or_epoch(v.get("first")) < old]:
        seen.pop(cid, None)

    _save_map_file(SEEN_PATH, {"bootstrapped": data.get("bootstrapped") or stamp},
                   "markets", seen)
    return announce


def run_instant():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    now = _utc_now()
    today = madrid_today()
    state = ensure_today(load_state(), today)
    sent = state["instant_sent"]
    la = last_alerts(state)
    universe = alertable_universe(index)

    tracker = _load_json(TRACKER_PATH)
    if not isinstance(tracker, dict):
        tracker = {}
    bands = tracker.get("bands") if isinstance(tracker.get("bands"), dict) else {}
    leaders = tracker.get("leaders") if isinstance(tracker.get("leaders"), dict) else {}

    # lines[event_key] = list of (swing, cid, prob, text). Only 6h odds moves that
    # clear the market's category threshold ever land here.
    lines = {}

    # New listings: recorded in seen_markets.json, never posted (a listing reaching
    # a volume floor is not an odds move).
    check_new_markets(universe, now)

    by_id = {m["conditionId"]: m for m in universe}

    # Favorite changes in multi-outcome (negRisk) events: tracked for the
    # supporting clause only, never a trigger on their own.
    events = {}
    for m in universe:
        # Only outcomes with real trading can be "the favorite": Polymarket lists
        # unnamed placeholders ("Player K") at the 50% default with $0 volume.
        if (m.get("negRisk") and isinstance(m.get("impliedProbability"), (int, float))
                and (m.get("volume") or 0) >= FLIP_CANDIDATE_VOLUME):
            events.setdefault(event_key(m), []).append(m)
    new_leaders = {}
    new_favorites = set()
    for ek, ms in events.items():
        if len(ms) < 2:
            continue
        top = max(ms, key=lambda x: x["impliedProbability"])
        prev_id = leaders.get(ek)
        prev = by_id.get(prev_id) if prev_id else None
        if prev is None or prev_id == top["conditionId"]:
            new_leaders[ek] = top["conditionId"]   # first sighting / unchanged
            continue
        # Hysteresis: the challenger must lead by FLIP_MARGIN, else keep the old leader.
        if top["impliedProbability"] - (prev.get("impliedProbability") or 0) < FLIP_MARGIN:
            new_leaders[ek] = prev_id
            continue
        new_leaders[ek] = top["conditionId"]
        if top["impliedProbability"] >= FLIP_MIN_PROB:
            new_favorites.add(top["conditionId"])

    # 6h odds moves (the only trigger). Crossing bands are still tracked every run.
    new_bands = {}
    for m in universe:
        cid = m["conditionId"]
        p = m.get("impliedProbability")
        if not isinstance(p, (int, float)):
            continue
        old_band = bands.get(cid)
        band = crossing_band(old_band, p)
        new_bands[cid] = band
        if not is_live_swingable(m) or not loud_enough(m):
            continue
        inst = category_threshold(m, "instant")
        # Both windows, same threshold; keep the larger qualifying move. One
        # cooldown record (last_alert) covers both, so a market posts once per move.
        candidates = []
        for hours, sw in ((INSTANT_HOURS, swing_6h(cid)), (INSTANT_DAY_HOURS, swing_24h(cid))):
            if sw and clears(sw[1] - sw[0], inst) and not artificial_swing(sw[0], m):
                candidates.append((abs(sw[1] - sw[0]), hours, sw[0], sw[1]))
        if not candidates:
            continue
        _, hours, start, end = max(candidates, key=lambda c: (c[0], -c[1]))
        if not cooldown_ok(la, cid, end, inst, now):
            continue
        clauses = [crossing_clause(old_band, band)]
        if cid in new_favorites:
            clauses.append(f"making {_short_subject(subject_outcome(m.get('question'))[0])} "
                           f"the new favorite")
        clauses.append(volume_clause(m, hours))
        lines.setdefault(event_key(m), []).append(
            (abs(end - start), cid, end, instant_sentence(m, start, end, cid + today,
                                                          hours=hours, clauses=clauses)))

    # One Slack message per event per run, biggest moves first.
    fired = 0
    messages = 0
    for ek, items in sorted(lines.items(), key=lambda kv: -max(x[0] for x in kv[1])):
        if messages >= INSTANT_MAX_MESSAGES:
            print(f"Instant: message cap {INSTANT_MAX_MESSAGES} hit, deferring the rest.")
            break
        chosen = sorted(items, key=lambda x: -x[0])[:EVENT_MAX_LINES]
        if post_slack("\n\n".join(x[3] for x in chosen)):
            messages += 1
            for _, cid, prob, _ in chosen:
                mark_alerted(la, cid, prob, now)
                sent[cid] = {"ts": _iso_utc(now), "kind": "swing"}
                fired += 1

    _save_maps(TRACKER_PATH, {}, {"leaders": new_leaders, "bands": new_bands})
    save_state(state)
    print(f"Instant alerts: {messages} message(s), {fired} market line(s) "
          f"({len(sent)} sent so far today {today}).")



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
    """Our own GitHub Pages page, keyed on the market's stored slug (the exact slug
    the poller builds each page under), so it always exists. Not used for alert
    links (which go to HoopsMatic) — kept as a reliable fallback."""
    return f"{SITE_BASE}/docs/market/{m.get('slug')}/"


def market_url_clean(m):
    """Our GitHub Pages page at the CLEAN (question-derived) slug — the URL that
    HoopsMatic and any human-facing clean link actually request. When the stored
    slug carries a -<timestamp> (or value) suffix the clean slug lacks, build_entities
    emits an alias page here. This is the meaningful "is our page reachable?" check:
    market_url()'s stored slug is built for every market so it is always 200."""
    return f"{SITE_BASE}/docs/market/{slugify(m.get('question'))}/"


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


def due_slot(now_madrid):
    """The most recent digest slot whose target time has elapsed, as
    (slot_id, label, slot_date). Each slot stays claimable from its target hour
    until the next slot's target, so a cron delayed hours past its slot still
    posts it (the old narrow-hour windows silently missed these); a fire after
    midnight maps to the PRIOR day's night slot. There is always an elapsed
    target (yesterday's at worst), so this never returns None."""
    candidates = []
    for slot_id, hour, label in DIGEST_SLOTS:
        target_today = now_madrid.replace(hour=hour, minute=0, second=0, microsecond=0)
        for target in (target_today, target_today - timedelta(days=1)):
            if target <= now_madrid:
                candidates.append((target, slot_id, label))
    target, slot_id, label = max(candidates)
    return slot_id, label, target.strftime("%Y-%m-%d")


def digest_healthcheck(state):
    """Tripwire: the digest silently skipped every run for 12 days once. If no
    slot has been processed in > DIGEST_STALE_HOURS, post ONE loud Slack notice
    per UTC day (an Actions log line stays invisible — which is how it went
    unseen). Safe to call from any job and it is called from two: the digest
    itself, and the poll (whose workflow is the reliable heartbeat), so even the
    digest workflow silently not running at all is still caught. Mutates state's
    digest3.last_warn and returns True when it warned; the caller saves state."""
    digest = state.get("digest3") or {}
    last_slot = digest.get("last_slot")
    if not last_slot:
        return False                                   # no heartbeat yet (fresh install)
    now_utc = datetime.now(timezone.utc)
    try:
        stale_h = (now_utc - datetime.fromisoformat(last_slot)).total_seconds() / 3600
    except ValueError:
        return False
    today = now_utc.strftime("%Y-%m-%d")
    if stale_h > DIGEST_STALE_HOURS and digest.get("last_warn") != today:
        post_slack(f":rotating_light: NBA movers digest has not posted in "
                   f"{stale_h:.0f}h (last slot {last_slot[:16]}Z). "
                   f"Check the movers-digest workflow/cron.")
        digest["last_warn"] = today
        state["digest3"] = digest
        return True
    return False


def run_digest(force=False):
    now_madrid = datetime.now(MADRID)
    slot_id, slot_label, slot_date = due_slot(now_madrid)

    # Debug line so a run's log states definitively whether force was active and
    # why (see main() for the --force / GITHUB_EVENT_NAME detection).
    print(f"Digest: force={force}, GITHUB_EVENT_NAME={os.environ.get('GITHUB_EVENT_NAME')!r}, "
          f"argv={sys.argv[1:]}, Madrid={now_madrid:%H:%M} (slot={slot_id}@{slot_date}).")

    # Dedup by slot-instance ("<date>-<slot>"), kept as a short rolling list so a
    # night slot's post-midnight fire (prior day) still dedups. Leave the instant
    # channel's state untouched.
    state = load_state()
    digest = state.get("digest3") or {}
    sent = digest.get("sent")
    if not isinstance(sent, list):
        sent = []                          # fresh, or migrate the old {date, slots} shape
    # carry the health fields across runs: last_slot = ISO-UTC of the last slot
    # processed; last_warn = the UTC date the tripwire last warned for (dedup)
    digest = {"sent": sent, "last_slot": digest.get("last_slot"),
              "last_warn": digest.get("last_warn")}
    state["digest3"] = digest
    state.setdefault("instant_sent", {})
    now_utc = now_madrid.astimezone(timezone.utc)

    # Same tripwire the poll job runs; harmless to also check here before posting.
    if not force and digest_healthcheck(state):
        save_state(state)

    slot_key = f"{slot_date}-{slot_id}"

    # A manual workflow_dispatch (force) bypasses the dedup so the digest can be
    # tested on demand at any hour; a forced run posts but records no slot, so it
    # never consumes a scheduled slot and can be repeated. Scheduled crons dedup.
    if force:
        slot_label = f"manual {now_madrid:%H:%M}"
    elif slot_key in sent:
        print(f"Digest: {slot_label} slot for {slot_date} already sent, skipping.")
        return

    cutoff_utc = now_madrid.astimezone(timezone.utc) - timedelta(hours=24)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    la = last_alerts(state)
    movers = []
    max_swing = 0.0                                # biggest 24h move among real candidates
    for m in index.get("markets", []):
        cid = m.get("conditionId")
        if not cid or not m.get("negRisk") or m.get("resolved") or not loud_enough(m):
            continue
        if slack_muted(m):
            continue                               # muted type (see SLACK_MUTED_MARKET_TYPES)
        sw = swing_last_24h(cid, cutoff_utc)
        if not sw:
            continue
        start_p, end_p = sw
        if artificial_swing(start_p, m):
            continue                               # skip 0.50-default initialization noise
        delta = abs(end_p - start_p)
        max_swing = max(max_swing, delta)          # track before the floor, for the quiet-day line
        floor = category_threshold(m, "digest")
        if not clears(delta, floor):
            continue
        # Shared 24h cooldown: skip a market already alerted (instant or a previous
        # digest) unless it has moved 2x its category's digest floor since then.
        if not cooldown_ok(la, cid, end_p, floor, now_utc):
            continue
        movers.append((m, start_p, end_p, delta))

    movers.sort(key=lambda x: x[3], reverse=True)
    # At most one line per event: keep each event's biggest mover.
    seen_events = set()
    per_event = []
    for mv in movers:
        ek = event_key(mv[0])
        if ek in seen_events:
            continue
        seen_events.add(ek)
        per_event.append(mv)
    movers = per_event[:DIGEST_TOP_N]

    if movers:
        header = f"*NBA Polymarket — biggest moves · last 24h ({slot_date}, {slot_label} Madrid)*"
        blocks = [header] + [
            prose_sentence(m, start_p, end_p, m.get("conditionId", "") + slot_date + slot_id,
                           "over the past 24 hours on Polymarket")
            for m, start_p, end_p, _ in movers
        ]
        if post_slack("\n\n".join(blocks)) and not force:
            for m, _, end_p, _ in movers:      # forced test runs don't consume cooldowns
                mark_alerted(la, m["conditionId"], end_p, now_utc)
    elif not force and now_madrid.date() <= QUIET_LINE_UNTIL:
        # Pre-season quiet-day signal (self-expires at QUIET_LINE_UNTIL): report the
        # flat market and by how much, so silence isn't mistaken for a broken pipeline.
        post_slack(f"*NBA Polymarket* — markets quiet, biggest 24h move {max_swing * 100:.1f}pt.")

    if not force:                                  # forced runs record no slot
        sent.append(slot_key)
        digest["sent"] = sent[-9:]                 # bound the list (~3 days x 3 slots)
        digest["last_slot"] = now_utc.isoformat()  # health heartbeat for the tripwire
        save_state(state)
    print(f"Digest: {len(movers)} movers posted for {slot_date} {slot_label} slot.")


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
    elif mode == "--digest-healthcheck":
        # Cheap staleness tripwire, run from the poll workflow (the reliable
        # heartbeat) so a digest workflow that stops firing entirely is still caught.
        state = load_state()
        if digest_healthcheck(state):
            save_state(state)
    else:
        print("usage: movers_alerts.py --instant | --digest [--force] | --digest-healthcheck",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
