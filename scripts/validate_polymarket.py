"""
Polymarket Gamma + CLOB API validation script.

Run once locally. Outputs validation_report.json next to itself.
Paste me the report and I'll write the production polling script against the
confirmed shape.

Usage:
    pip install requests
    python validate_polymarket.py
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

report = {"ran_at": datetime.utcnow().isoformat() + "Z", "checks": {}}


def check(name, fn):
    print(f"\n=== {name} ===")
    try:
        result = fn()
        report["checks"][name] = {"ok": True, "result": result}
        print("OK")
    except Exception as e:
        report["checks"][name] = {"ok": False, "error": str(e)}
        print(f"FAIL: {e}")


def http_get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


# 1. /sports — find the NBA tag and series IDs
def check_sports():
    data = http_get(f"{GAMMA}/sports")
    nba_entries = [
        s for s in data
        if (s.get("sport") or "").lower() == "nba"
        or "nba" in json.dumps(s).lower()[:200]
    ]
    return {
        "total_sports": len(data),
        "first_sport_sample": data[0] if data else None,
        "nba_matches": nba_entries[:3],
    }


# 2. Fetch the known NBA championship event by slug
def check_event_by_slug():
    data = http_get(f"{GAMMA}/events", params={"slug": "2026-nba-champion"})
    if not data:
        return {"found": False, "note": "empty array"}
    ev = data[0]
    sample_market = (ev.get("markets") or [{}])[0]
    return {
        "found": True,
        "event_id": ev.get("id"),
        "event_title": ev.get("title"),
        "event_slug": ev.get("slug"),
        "active": ev.get("active"),
        "closed": ev.get("closed"),
        "volume": ev.get("volume"),
        "volume24hr": ev.get("volume24hr"),
        "tags": ev.get("tags"),
        "series": ev.get("series"),
        "n_markets": len(ev.get("markets") or []),
        "sample_market": {
            "id": sample_market.get("id"),
            "question": sample_market.get("question"),
            "slug": sample_market.get("slug"),
            "outcomes": sample_market.get("outcomes"),
            "outcomePrices": sample_market.get("outcomePrices"),
            "clobTokenIds": sample_market.get("clobTokenIds"),
            "volume": sample_market.get("volume"),
            "volume24hr": sample_market.get("volume24hr"),
            "liquidity": sample_market.get("liquidity"),
            "oneDayPriceChange": sample_market.get("oneDayPriceChange"),
            "oneWeekPriceChange": sample_market.get("oneWeekPriceChange"),
            "active": sample_market.get("active"),
            "closed": sample_market.get("closed"),
            "endDate": sample_market.get("endDate"),
        },
    }


# 3. Fetch the NBA MVP event
def check_mvp_event():
    data = http_get(f"{GAMMA}/events", params={"slug": "nba-mvp-694"})
    if not data:
        return {"found": False}
    ev = data[0]
    return {
        "found": True,
        "event_id": ev.get("id"),
        "event_title": ev.get("title"),
        "series": ev.get("series"),
        "tags": ev.get("tags"),
        "n_markets": len(ev.get("markets") or []),
        "first_3_markets": [
            {"q": m.get("question"), "prices": m.get("outcomePrices")}
            for m in (ev.get("markets") or [])[:3]
        ],
    }


# 4. Try filtering events by series — does series_id work?
def check_series_filter():
    # try a couple of series_id candidates
    candidates = [10345, 2]
    out = {}
    for sid in candidates:
        data = http_get(
            f"{GAMMA}/events",
            params={
                "series_id": sid,
                "active": "true",
                "closed": "false",
                "limit": 5,
            },
        )
        non_closed = [e for e in data if e.get("closed") is False]
        out[f"series_id={sid}"] = {
            "n_returned": len(data),
            "n_currently_open": len(non_closed),
            "first_title": data[0].get("title") if data else None,
            "first_seriesSlug": data[0].get("seriesSlug") if data else None,
            "all_nba": (
                all("nba" in (e.get("seriesSlug") or "").lower() for e in data)
                if data
                else None
            ),
        }
    return out


# 5. Try filtering events by tag slug
def check_tag_filter():
    # Tag slug "nba" might work via tag_slug parameter
    out = {}
    for params in [
        {"tag_slug": "nba", "active": "true", "closed": "false", "limit": 5},
        {"category": "Sports", "active": "true", "closed": "false", "limit": 5},
    ]:
        try:
            data = http_get(f"{GAMMA}/events", params=params)
            out[json.dumps(params)] = {
                "n_returned": len(data),
                "first_title": data[0].get("title") if data else None,
                "first_seriesSlug": data[0].get("seriesSlug") if data else None,
            }
        except Exception as e:
            out[json.dumps(params)] = {"error": str(e)}
    return out


# 6. Price history for one NBA market (the YES token of the Thunder championship market)
def check_price_history():
    # First grab the Thunder championship market from the event
    data = http_get(f"{GAMMA}/events", params={"slug": "2026-nba-champion"})
    if not data:
        return {"error": "no event"}
    markets = (data[0].get("markets") or [])
    # find a market with non-trivial volume
    pick = None
    for m in markets:
        try:
            v = float(m.get("volume") or 0)
            if v > 10000:
                pick = m
                break
        except Exception:
            continue
    if not pick:
        pick = markets[0] if markets else None
    if not pick:
        return {"error": "no markets"}

    clob_token_ids = json.loads(pick.get("clobTokenIds") or "[]")
    if not clob_token_ids:
        return {"error": "no clobTokenIds", "market": pick.get("question")}
    yes_token = clob_token_ids[0]

    history = http_get(
        f"{CLOB}/prices-history",
        params={"market": yes_token, "interval": "1d", "fidelity": 60},
    )
    h = history.get("history") or []
    return {
        "market_question": pick.get("question"),
        "market_slug": pick.get("slug"),
        "yes_token_id": yes_token,
        "history_points_1d": len(h),
        "first_point": h[0] if h else None,
        "last_point": h[-1] if h else None,
    }


# 7. How many NBA events exist if we just paginate sports + filter by series slug
def check_full_nba_pagination():
    page = http_get(
        f"{GAMMA}/events",
        params={
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": 0,
            "order": "volume24hr",
            "ascending": "false",
        },
    )
    nba = [
        e for e in page
        if (e.get("seriesSlug") or "").lower() == "nba"
        or any(
            (s.get("slug") or "").lower() == "nba"
            for s in (e.get("series") or [])
        )
    ]
    return {
        "page_size": len(page),
        "nba_in_first_page": len(nba),
        "first_5_nba_titles": [e.get("title") for e in nba[:5]],
        "first_5_nba_volumes_24h": [e.get("volume24hr") for e in nba[:5]],
        "first_5_nba_end_dates": [e.get("endDate") for e in nba[:5]],
    }


for name, fn in [
    ("sports_metadata", check_sports),
    ("event_by_slug_champion", check_event_by_slug),
    ("event_by_slug_mvp", check_mvp_event),
    ("series_id_filter", check_series_filter),
    ("tag_or_category_filter", check_tag_filter),
    ("price_history_for_one_market", check_price_history),
    ("nba_via_full_pagination", check_full_nba_pagination),
]:
    check(name, fn)

out_path = Path(__file__).resolve().parent / "validation_report.json"
out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
print(f"\nReport written to {out_path}")
