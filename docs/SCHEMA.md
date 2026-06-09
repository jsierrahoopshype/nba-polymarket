# Data Schema

This document describes every file the tracker produces and every field inside
it. It is written to be readable without coding knowledge. The frontend pages
read these files directly from the same site, so this is the contract between
the data pipeline and the pages.

There are three kinds of files, all under the `data/` folder:

| File | What it is | Written by |
| --- | --- | --- |
| `data/index.json` | One summary list of every market worth showing right now. The pages load this first. | `scripts/build_index.py` |
| `data/markets/<conditionId>.json` | The full history of one live market. One file per market. | `scripts/poll_polymarket.py` |
| `data/archive/<YYYY-MM>/<conditionId>.json` | A frozen, resolved market, filed under the month it resolved. | `scripts/poll_polymarket.py` |

`conditionId` is Polymarket's unique ID for a market. We use it as the filename
so every market has exactly one place to live.

---

## How the data flows

Every 10 minutes a scheduled job runs:

1. **Poll** (`poll_polymarket.py`): asks Polymarket for every active NBA event,
   and for each market inside takes a *snapshot* of the current numbers. The
   snapshot is appended to that market's history file.
2. **Compact**: old history points are thinned out so files stay small forever
   (details under "Compaction tiers" below).
3. **Resolve**: if a market has just closed (a team or player is in/out), a
   final snapshot is written, the file is stamped `resolved`, and it is moved
   into the archive.
4. **Sweep**: a market can leave the active feed *before* we ever see it close
   (its parent event goes inactive when, say, a game ends or the season
   finishes). Any live file that wasn't refreshed this cycle is re-checked by
   looking its event up directly by slug, so an off-feed resolution still gets
   archived. The sweep is bounded (at most 50 events per run, most-stale first)
   and never deletes a file just because it is temporarily missing from a feed.
5. **Build index** (`build_index.py`): all the live files plus recently
   resolved ones are summarized into `data/index.json`.

---

## The snapshot

A snapshot is one reading of a market at one moment. Every history list is a
list of snapshots, oldest first.

| Field | Type | Meaning |
| --- | --- | --- |
| `timestamp` | string (ISO 8601 UTC, ends in `Z`) | When this reading was taken. |
| `impliedProbability` | number 0–1, or null | The market's estimate of the chance this outcome happens. This is the YES price. `0.62` means a 62% implied chance. |
| `volume24hr` | number | Dollar volume traded in the last 24 hours. Polymarket reports null on resolved markets; we store `0` instead. |
| `volume` | number, or null | Total dollar volume ever traded on this market. |
| `liquidity` | number, or null | How much money is resting in the order book. |
| `spread` | number, or null | Gap between the best buy and best sell price. Smaller means a tighter, more confident market. |
| `oneDayPriceChange` | number, or null | Polymarket's own reported price change over the last day. |
| `oneWeekPriceChange` | number, or null | Polymarket's own reported price change over the last week. |

> Note on backfilled points: the very first time we ever see a market we try to
> pull some past prices from Polymarket so charts are not empty. Those
> historical points only have `timestamp` and `impliedProbability`; the other
> fields are null because that detail is not available for the past.

---

## Per-market file: `data/markets/<conditionId>.json`

This holds the identity of one market (stored once) plus its full history.

| Field | Type | Meaning |
| --- | --- | --- |
| `conditionId` | string | Polymarket's unique market ID. Matches the filename. |
| `slug` | string | Short URL-style name of the market. |
| `question` | string | The market's question, e.g. "Will the Thunder win the 2026 NBA Championship?". |
| `eventSlug` | string | URL-style name of the parent event. |
| `eventTitle` | string | Human title of the parent event, e.g. "2026 NBA Champion". |
| `eventId` | string | Polymarket's ID for the parent event. |
| `clobTokenIds` | list of strings | The token IDs for the YES and NO sides. The first is YES. |
| `outcomes` | list of strings | The outcome labels, normally `["Yes", "No"]`. |
| `endDate` | string or null | When the market is scheduled to end. |
| `tags` | list of objects | The parent event's tags (used for grouping in the UI — see the table below). |
| `resolved` | boolean | `false` while live; `true` once the market has closed. |
| `resolvedAt` | string or null | When we detected it resolved. `null` while live. |
| `history` | list of snapshots | Every reading we have kept, oldest first. |

When a market resolves, the same file (with `resolved: true`, a final snapshot,
and `resolvedAt` set) is **moved** from `data/markets/` into
`data/archive/<YYYY-MM>/`, where `<YYYY-MM>` is the month it resolved.

> File format: the identity fields are pretty-printed, but each snapshot in
> `history` is written on its own compact single line. A normal poll therefore
> only appends one line and changes nothing else, which keeps commits tiny. It
> is still ordinary JSON — anything that reads JSON reads it unchanged.

---

## The index file: `data/index.json`

This is the display-ready list the pages load. It is rebuilt from scratch every
cycle.

> File format: the top-level fields are pretty-printed and each market entry is
> written on its own compact single line. Because the whole file is rewritten
> every cycle, keeping each entry compact (and trimming `tags` to just
> `id`/`label`/`slug`) keeps the committed size down. It is still ordinary
> JSON; the pages parse it the same either way.

Top level:

| Field | Type | Meaning |
| --- | --- | --- |
| `lastUpdated` | string (ISO 8601 UTC) | When the index was built. |
| `count` | number | How many markets are in the list. |
| `markets` | list of entries | The markets, **live first** (highest 24h volume first), then recently resolved (most recent first). |

Each entry in `markets`:

| Field | Type | Meaning |
| --- | --- | --- |
| `conditionId` | string | Unique market ID. Used to link to the detail page (`market.html?id=<conditionId>`) and to load `data/markets/<conditionId>.json`. |
| `slug` | string | Short name. |
| `question` | string | The market's question. |
| `eventSlug` | string | Parent event short name. |
| `eventTitle` | string | Parent event title. |
| `eventId` | string | Parent event ID. |
| `endDate` | string or null | Scheduled end. |
| `tags` | list of objects | Parent event tags, for grouping. |
| `impliedProbability` | number or null | Latest implied probability (the headline number). |
| `volume24hr` | number | Latest 24h volume. |
| `volume` | number or null | Latest total volume. |
| `liquidity` | number or null | Latest liquidity. |
| `delta24h` | number or null | Change in implied probability over the last 24 hours (e.g. `+0.05` = up 5 points). |
| `delta7d` | number or null | Change in implied probability over the last 7 days. |
| `sparkline` | list of `{t, p}` | A small set of points for a mini-chart: at most 30 points spread across the last 7 days. `t` is a timestamp, `p` is the implied probability. |
| `resolved` | boolean | Whether this market has resolved. |
| `resolvedAt` | string or null | When it resolved, if it has. |

### Which markets appear in the index

- **Live markets:** always in the index.
- **Recently resolved markets** (resolved within the last **7 days**): in the
  index with `resolved: true` and `resolvedAt` set. These power the
  "Recently resolved" strip on the homepage.
- **Older resolved markets:** *not* in the index. They stay only in the
  archive, so the index never grows without bound.

---

## UI grouping tags

Markets are grouped in the UI by their parent event's tags. Polling always uses
the NBA tag (`745`); these finer tags are only for sorting markets into
sections on the pages.

| Tag ID | Group |
| --- | --- |
| 745 | NBA (the umbrella tag — used for polling) |
| 100240 | NBA Finals |
| 707 | MVP |
| 18 | Awards |
| 102288 | NBA Champion |
| 104587 | 2026 NBA Playoffs |

A market may carry several of these tags. Each tag object inside `tags` looks
like `{"id": "745", "label": "NBA", "slug": "nba"}`.

---

## Compaction tiers

To keep history files small forever, older points are thinned. Each cycle, for
every history list, we keep at most one point per time bucket, where the bucket
size depends on how old the point is:

| Age of the point | Kept resolution |
| --- | --- |
| Last 24 hours | every 10 minutes (everything, since we poll every 10 min) |
| 1 to 7 days | hourly |
| 7 to 30 days | every 6 hours |
| Older than 30 days | daily |

The newest point is always kept.

---

## Example entries

### A live market (as it appears in `data/index.json`)

```json
{
  "conditionId": "0x8f2c1d7e3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d",
  "slug": "will-the-thunder-win-the-2026-nba-championship",
  "question": "Will the Oklahoma City Thunder win the 2026 NBA Championship?",
  "eventSlug": "2026-nba-champion",
  "eventTitle": "2026 NBA Champion",
  "eventId": "21456",
  "endDate": "2026-06-21T00:00:00Z",
  "tags": [
    {"id": "745", "label": "NBA", "slug": "nba"},
    {"id": "102288", "label": "NBA Champion", "slug": "nba-champion"}
  ],
  "impliedProbability": 0.34,
  "volume24hr": 184213.55,
  "volume": 9425110.82,
  "liquidity": 142880.19,
  "delta24h": 0.03,
  "delta7d": -0.05,
  "sparkline": [
    {"t": "2026-06-01T14:00:00Z", "p": 0.39},
    {"t": "2026-06-03T02:00:00Z", "p": 0.37},
    {"t": "2026-06-05T14:00:00Z", "p": 0.31},
    {"t": "2026-06-08T14:00:00Z", "p": 0.34}
  ],
  "resolved": false,
  "resolvedAt": null
}
```

### A recently resolved market (also in `data/index.json` for up to 7 days)

```json
{
  "conditionId": "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
  "slug": "will-nikola-jokic-win-2026-nba-mvp",
  "question": "Will Nikola Jokic win the 2026 NBA MVP?",
  "eventSlug": "nba-mvp",
  "eventTitle": "2026 NBA MVP",
  "eventId": "20991",
  "endDate": "2026-05-12T00:00:00Z",
  "tags": [
    {"id": "745", "label": "NBA", "slug": "nba"},
    {"id": "707", "label": "MVP", "slug": "mvp"},
    {"id": "18", "label": "Awards", "slug": "awards"}
  ],
  "impliedProbability": 1.0,
  "volume24hr": 0,
  "volume": 5310447.12,
  "liquidity": 0,
  "delta24h": 0.12,
  "delta7d": 0.28,
  "sparkline": [
    {"t": "2026-06-01T14:00:00Z", "p": 0.72},
    {"t": "2026-06-03T14:00:00Z", "p": 0.88},
    {"t": "2026-06-04T10:00:00Z", "p": 1.0}
  ],
  "resolved": true,
  "resolvedAt": "2026-06-04T10:32:00Z"
}
```

A resolved market settles at `1.0` (the outcome happened) or `0.0` (it did
not). The per-market archive file has the same identity fields plus the full
`history` list ending in that final snapshot.
