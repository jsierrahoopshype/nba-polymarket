"""
HoopsMatic link 404-audit (background, decoupled from the alert pipeline).

Our Slack movers alerts link to HoopsMatic market pages, but HoopsMatic doesn't
build a page for every market (coverage gaps, e.g. free-agency next-team events),
so some alert links 404. This job measures that 404 rate for real, once a day,
instead of us finding out one broken link at a time.

It is a PERIODIC BACKGROUND AUDIT, not a per-alert live check:
  - Runs in its own workflow (.github/workflows/hoopsmatic-audit.yml), never in
    the poll/alert path, so it can't slow down or break alerts.
  - Reads data/index.json, selects the alert-eligible markets (the exact gating
    movers_alerts uses), and HTTP-checks each market's HoopsMatic URL (the exact
    URL an alert would produce — the slug logic is imported from movers_alerts).
  - For every broken HoopsMatic link it cross-checks our own GitHub Pages page is
    up, so we can tell "HoopsMatic coverage gap" from "market genuinely gone".

Output (cheapest, versioned): data/hoopsmatic_audit.json, committed daily, with a
rolling 30-day history so we get a 404-rate trend for free. A weekly summary is
posted to Slack (Mondays, Europe/Madrid) via $SLACK_MOVERS_WEBHOOK; with no
webhook set it prints (dry-run), so it is safe to run locally.

Usage:
    python scripts/hoopsmatic_audit.py
    SLACK_MOVERS_WEBHOOK=... python scripts/hoopsmatic_audit.py
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Reuse the alert channel's gating + link builders so the audit tests the EXACT
# markets that can alert and the EXACT URLs alerts produce (single source of truth).
from movers_alerts import (MADRID, hoopsmatic_url, loud_enough, market_url,
                           post_slack)

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "data" / "index.json"
AUDIT_PATH = REPO_ROOT / "data" / "hoopsmatic_audit.json"

AUDIT_MAX = 150            # bound the HTTP volume (all ~72 eligible fit under this)
REQUEST_DELAY = 0.4       # seconds between requests — polite, ~30-60s total
TIMEOUT = 15
HISTORY_DAYS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- HTTP ---------------------------------------------------------------------

def check_url(url):
    """(status, verdict) for a URL. verdict is 'ok' | 'broken' | 'error'.

    Broken = a 4xx/5xx status, OR a 200 that is really a not-found response (a
    short body containing 'not found' — HoopsMatic has been seen returning
    {"error":"not found"}). A network failure is 'error' (inconclusive), never
    counted as a HoopsMatic gap."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
            body = resp.read(2000).decode("utf-8", "replace").lower()
    except urllib.error.HTTPError as exc:
        return exc.code, "broken"
    except Exception:  # noqa: BLE001 - network fails many ways; inconclusive
        return None, "error"
    soft_404 = len(body) < 600 and "not found" in body
    return status, ("broken" if soft_404 else "ok")


# --- audit --------------------------------------------------------------------

def eligible_markets(index):
    """The alert-eligible population: negRisk, unresolved, past the alert floors.
    Sorted by volume desc and capped, so a run is bounded and deterministic."""
    markets = [m for m in index.get("markets", [])
               if m.get("negRisk") and not m.get("resolved") and loud_enough(m)]
    markets.sort(key=lambda m: m.get("volume") or 0, reverse=True)
    return markets[:AUDIT_MAX]


def run_audit():
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    markets = eligible_markets(index)

    ok = broken = errors = 0
    broken_markets = []
    for m in markets:
        status, verdict = check_url(hoopsmatic_url(m))
        if verdict == "ok":
            ok += 1
        elif verdict == "error":
            errors += 1
        else:
            broken += 1
            # cross-check our own page is up -> "HoopsMatic gap" vs "market gone"
            our_status, our_verdict = check_url(market_url(m))
            broken_markets.append({
                "question": m.get("question"),
                "slug": m.get("slug"),
                "eventSlug": m.get("eventSlug"),
                "hoopsmatic_url": hoopsmatic_url(m),
                "status": status,
                "ourPageUp": our_verdict == "ok",
            })
            time.sleep(REQUEST_DELAY)
        time.sleep(REQUEST_DELAY)

    checked = ok + broken  # conclusive checks only; errors excluded from the rate
    rate = round(broken / checked, 4) if checked else 0.0
    return {
        "checked": checked, "ok": ok, "broken": broken, "errors": errors,
        "rate": rate, "brokenMarkets": broken_markets,
    }


# --- state / history ----------------------------------------------------------

def load_audit():
    try:
        return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_audit(audit):
    AUDIT_PATH.write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def update_history(history, today, result):
    """Replace today's entry if the audit re-ran, else append; keep last N days."""
    entry = {"date": today, "checked": result["checked"],
             "broken": result["broken"], "rate": result["rate"]}
    history = [h for h in (history or []) if h.get("date") != today]
    history.append(entry)
    return history[-HISTORY_DAYS:]


# --- weekly Slack summary -----------------------------------------------------

def fmt_pct(rate):
    return f"{rate * 100:.1f}%"


def weekly_summary(result, history):
    lines = [f"*HoopsMatic link audit — weekly*  ({fmt_pct(result['rate'])} of "
             f"{result['checked']} alert-eligible markets 404 on HoopsMatic)"]
    # 7-day trend from history (broken counts)
    recent = (history or [])[-7:]
    if len(recent) >= 2:
        trend = " → ".join(f"{h['broken']}" for h in recent)
        lines.append(f"Broken/day (last {len(recent)}): {trend}")
    gaps = [b for b in result["brokenMarkets"] if b.get("ourPageUp")]
    if gaps:
        lines.append(f"\nBroken links (our page is up — HoopsMatic coverage gap):")
        for b in gaps[:12]:
            lines.append(f"• {b['question']}\n  {b['hoopsmatic_url']}")
        if len(gaps) > 12:
            lines.append(f"…and {len(gaps) - 12} more.")
    else:
        lines.append("No coverage-gap links this week. 🎉")
    return "\n".join(lines)


def maybe_post_weekly(audit, result):
    """Post a Slack summary once a week (Mondays, Madrid), tracked in the file."""
    now_madrid = datetime.now(MADRID)
    year, week, _ = now_madrid.isocalendar()
    this_week = f"{year}-W{week:02d}"
    if now_madrid.weekday() != 0:                      # 0 = Monday
        return audit.get("lastWeeklyPost")
    if audit.get("lastWeeklyPost") == this_week:       # already posted this week
        return audit.get("lastWeeklyPost")
    post_slack(weekly_summary(result, audit.get("history")))
    return this_week


# --- entry point --------------------------------------------------------------

def main():
    result = run_audit()
    today = datetime.now(MADRID).strftime("%Y-%m-%d")

    audit = load_audit()
    audit["history"] = update_history(audit.get("history"), today, result)
    audit["lastWeeklyPost"] = maybe_post_weekly(audit, result)
    audit.update({"lastRun": iso_now(), **result})
    save_audit(audit)

    print(f"HoopsMatic audit: {result['broken']}/{result['checked']} broken "
          f"({fmt_pct(result['rate'])}), {result['errors']} inconclusive.")


if __name__ == "__main__":
    main()
