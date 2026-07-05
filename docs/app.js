/* ==========================================================================
   NBA Polymarket tracker — shared client-side helpers.

   Pure browser JS, no build step, no dependencies. Loaded by index.html,
   market.html and movers.html. All three read the same JSON the poller writes
   under the repo's /data folder.
   ========================================================================== */

/* --- data loading --------------------------------------------------------- */
/* The pages live in /docs and the data lives in /data at the repo root, so the
   natural path is "../data/". We try a few bases and cache the first that works
   so the site keeps functioning whether GitHub Pages serves from the repo root
   or from /docs with a data copy alongside. */
/* Candidates cover the app at /docs/ ("../data/") and the generated entity
   pages at /docs/player|team/<slug>/ ("../../../data/"). First 200 is cached. */
const DATA_BASES = ['../data/', '../../../data/', '../../data/', './data/', 'data/'];
let _dataBase = null;

async function loadData(path) {
  if (_dataBase !== null) {
    const r = await fetch(_dataBase + path);
    if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + path);
    return r.json();
  }
  let lastErr;
  for (const base of DATA_BASES) {
    try {
      const r = await fetch(base + path);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      _dataBase = base;          // remember the winning base for later loads
      return j;
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error('could not load ' + path);
}

/* --- categories ----------------------------------------------------------- */
/* Each market is assigned to exactly ONE bucket by first-match priority. Tag
   IDs are matched against docs/SCHEMA.md's grouping tags, adapted to the tags
   that actually carry markets in the current (Finals) season. Edit this list to
   re-shape the chips; everything else keys off it. */
const CATEGORIES = [
  { key: 'champion', label: 'Champion',   tags: ['102288'] },
  { key: 'fmvp',     label: 'Finals MVP', tags: ['104582'] },
  { key: 'mvp',      label: 'MVP',        tags: ['707'] },     // season MVP — empty in Finals season, auto-activates when those markets return
  { key: 'awards',   label: 'Awards',     tags: ['18'] },      // ROY/DPOY/etc. — empty in Finals season, auto-activates when those markets return
  { key: 'games',    label: 'Games',      tags: ['100639'] },
  { key: 'draft',    label: 'Draft',      tags: ['104857', '100283'] },
  { key: 'playoffs', label: 'Playoffs',   tags: ['104587', '100240', '102037'] },
  { key: 'futures',  label: 'Futures',    tags: null },   // catch-all: every live market always renders somewhere
];

function categoryOf(m) {
  const ids = new Set((m.tags || []).map(t => String(t && t.id)));
  for (const c of CATEGORIES) {
    if (c.tags && c.tags.some(t => ids.has(t))) return c;
  }
  return CATEGORIES[CATEGORIES.length - 1];   // Futures
}

/* --- display filtering (presentation only — we still poll/store everything) - */
/* Markets below this much TOTAL traded volume (USD) are hidden by default and
   revealed by the "show low-volume markets" toggle. */
const MIN_VOLUME = 10000;

/* A placeholder market: a freshly-listed outcome nobody has traded — zero/near
   zero volume, price still pinned at the 0.50 default, and no 24h movement. */
function isPlaceholder(m) {
  const lowVol = !isNum(m.volume) || m.volume < 1;
  const atDefault = isNum(m.impliedProbability) && Math.abs(m.impliedProbability - 0.5) < 0.005;
  const noMove = !isNum(m.delta24h) || Math.abs(m.delta24h) < 0.005;
  return lowVol && atDefault && noMove;
}

/* True when a market should show by default (real volume, not a placeholder). */
function passesFloor(m) {
  return isNum(m.volume) && m.volume >= MIN_VOLUME && !isPlaceholder(m);
}

/* --- normalized probability ----------------------------------------------- */
/* For mutually-exclusive (negRisk) events, the YES prices of the outcomes form
   a race that should sum to 100%. We normalize each outcome's YES price over
   the sum of the race set — live, non-placeholder outcomes above the volume
   floor — and expose a conditionId -> normalizedProbability map. Markets in
   non-negRisk events, or outside the race set, are absent (callers fall back to
   the raw YES price). Run AFTER the placeholder/volume filter. */
function computeNormalized(markets) {
  const byEvent = new Map();
  for (const m of markets || []) {
    if (m.resolved || !m.negRisk || !m.eventId) continue;
    if (!isNum(m.impliedProbability) || !passesFloor(m)) continue;
    if (!byEvent.has(m.eventId)) byEvent.set(m.eventId, []);
    byEvent.get(m.eventId).push(m);
  }
  const norm = new Map();
  for (const list of byEvent.values()) {
    const sum = list.reduce((s, m) => s + m.impliedProbability, 0);
    if (sum > 0) for (const m of list) norm.set(m.conditionId, m.impliedProbability / sum);
  }
  return norm;
}

/* Probability to display: normalized when available, else the raw YES price.
   Returns { value, normalized:boolean, raw }. */
function probInfo(m, norm) {
  const n = norm && norm.get(m.conditionId);
  if (isNum(n)) return { value: n, normalized: true, raw: m.impliedProbability };
  return { value: m.impliedProbability, normalized: false, raw: m.impliedProbability };
}

/* A "Now %" cell/value: normalized number with the raw YES price kept in a
   tooltip when the two differ. */
function probHtml(m, norm) {
  const info = probInfo(m, norm);
  if (info.normalized) {
    return '<span title="Raw YES price ' + fmtPct(info.raw) + '">' + fmtPct(info.value) + '</span>';
  }
  return fmtPct(info.value);
}

/* --- Polymarket deep links ------------------------------------------------ */
/* Site-wide legal-caution switch: set to false to hide EVERY outbound
   polymarket.com link at once (Polymarket is ISP-blocked in some regions and
   the regulatory picture can change quickly). Both link helpers honor it, so
   every caller hides its link when this is off. */
const SHOW_POLYMARKET_LINKS = true;

function polyEventUrl(eventSlug) {
  if (!SHOW_POLYMARKET_LINKS || !eventSlug) return null;
  return 'https://polymarket.com/event/' + encodeURIComponent(eventSlug);
}
function polyMarketUrl(m) {
  if (!SHOW_POLYMARKET_LINKS) return null;
  if (m.eventSlug && m.slug) {
    return 'https://polymarket.com/event/' + encodeURIComponent(m.eventSlug) +
           '/' + encodeURIComponent(m.slug);
  }
  if (m.eventSlug) return polyEventUrl(m.eventSlug);
  if (m.slug) return 'https://polymarket.com/market/' + encodeURIComponent(m.slug);
  return null;
}

/* Path back to /docs/ from whatever depth the current page sits at, derived
   from the data base loadData discovered (data/ and docs/ are siblings at the
   site root). Lets nav links resolve correctly from deep pages like
   /docs/player/<slug>/. Falls back to the app's own dir before the first load. */
function docsBase() {
  return (typeof _dataBase === 'string' ? _dataBase : '../data/').replace(/data\/$/, 'docs/');
}

/* onerror for hotlinked images: try the raw.githubusercontent mirror once
   (the Pages host can lag/serve differently), then hide if that also fails. */
const IMG_FALLBACK =
  "if(this.dataset.f){this.style.display='none'}else{this.dataset.f=1;" +
  "this.src=this.src.replace('https://jsierrahoopshype.github.io/nba-headshots/'," +
  "'https://raw.githubusercontent.com/jsierrahoopshype/nba-headshots/main/')}";

/* --- entities (players/teams matched to a market) ------------------------- */
/* data/entities.json maps a conditionId to { primary, all }: `primary` is the
   single unambiguous player/team for the row thumbnail (or null), `all` is every
   matched entity for the clickable chips. Both come straight from the build's
   exact full-name/alias matching — no fuzzy matching in the browser. Optional:
   if the file isn't there yet, rows just render without entities. */
let ENTITY_MAP = {};
async function loadEntities() {
  try {
    const d = await loadData('entities.json');
    ENTITY_MAP = (d && d.markets) || {};
  } catch (e) { ENTITY_MAP = {}; }
  return ENTITY_MAP;
}

function entityLink(x, cls, imgSize, withName) {
  return '<a class="' + cls + '" href="' + docsBase() + esc(x.t) + '/' + esc(x.slug) + '/" ' +
    'title="' + esc(x.name) + '" onclick="event.stopPropagation()">' +
    '<img src="' + esc(x.img) + '" alt="" loading="lazy" width="' + imgSize + '" height="' + imgSize +
    '" onerror="' + IMG_FALLBACK + '">' + (withName ? esc(x.name) : '') + '</a>';
}

/* Small linked thumbnail for a market's primary entity (row name cells). */
function entityThumb(conditionId) {
  const e = ENTITY_MAP[conditionId];
  const p = e && e.primary;
  return p ? entityLink(p, 'ethumb', 22, false) : '';
}

/* Clickable chips for every entity matched to a market (one per player/team). */
function entityChips(conditionId) {
  const e = ENTITY_MAP[conditionId];
  const all = (e && e.all) || [];
  if (!all.length) return '';
  return '<span class="echips">' + all.map(x => entityLink(x, 'echip', 16, true)).join('') + '</span>';
}

/* --- formatting ----------------------------------------------------------- */
function isNum(v) { return typeof v === 'number' && isFinite(v); }

/* implied probability (0-1) -> whole-number percent, e.g. 0.78 -> "78%" */
function fmtPct(p) { return isNum(p) ? Math.round(p * 100) + '%' : '—'; }

/* probability delta (in 0-1 units) -> { text, cls }, e.g. 0.042 -> +4.2 (pos).
   Near-flat (|change| < 0.5 points) is shown neutral gray. */
function fmtDelta(d) {
  if (!isNum(d)) return { text: '—', cls: 'flat', v: null };
  const pts = Math.round(d * 1000) / 10;            // points, 1 decimal
  const cls = pts >= 0.5 ? 'pos' : (pts <= -0.5 ? 'neg' : 'flat');
  const text = (pts > 0 ? '+' : '') + pts.toFixed(1);
  return { text, cls, v: pts };
}

function fmtVol(v) {
  if (!isNum(v)) return '—';
  if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + Math.round(v / 1e3) + 'K';
  return '$' + Math.round(v);
}

/* short local date like "Jun 9" */
function fmtDayShort(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const GAME_TAG = '100639';   // Polymarket "Games" tag
function isGameMarket(m) {
  return (m.tags || []).some(t => String(t && t.id) === GAME_TAG);
}

/* Display title: game markets get the game date appended so otherwise-identical
   matchup rows ("Spurs vs. Knicks") are distinguishable. */
function displayTitle(m) {
  const q = m.question || '';
  if (isGameMarket(m)) {
    const d = fmtDayShort(m.endDate);
    if (d) return q + ' · ' + d;
  }
  return q;
}

/* "Settled" badge for resolved markets — shown wherever a market renders so a
   recently-resolved market never reads as live odds. */
function settledBadge(m) {
  return m && m.resolved ? '<span class="badge-settled">Settled</span>' : '';
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtStamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  }) + ' local';
}

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* conditionId -> slug, populated from index.json by registerSlugs. Lets
   marketUrl point at the pre-generated SEO page docs/market/<slug>/ when one
   exists, and fall back to docs/market.html?id= otherwise. */
let MARKET_SLUG = {};
function registerSlugs(markets) {
  (markets || []).forEach(function (m) {
    if (m && m.conditionId && m.slug) MARKET_SLUG[m.conditionId] = m.slug;
  });
}
function marketUrl(id) {
  var slug = MARKET_SLUG[id];
  return slug
    ? docsBase() + 'market/' + encodeURIComponent(slug) + '/'
    : docsBase() + 'market.html?id=' + encodeURIComponent(id);
}

/* --- sparkline ------------------------------------------------------------ */
/* Tiny inline SVG line from a market's sparkline points. Color encodes net
   direction over the window (up green / down red / flat gray). */
function sparkline(points) {
  const ys = (points || []).map(p => p && p.p).filter(isNum);
  if (ys.length < 2) return '<span class="dash">—</span>';
  const w = 88, h = 24, pad = 3;
  const min = Math.min(...ys), max = Math.max(...ys);
  const span = (max - min) || 1;
  const n = ys.length;
  const X = i => pad + i * (w - 2 * pad) / (n - 1);
  const Y = v => pad + (1 - (v - min) / span) * (h - 2 * pad);
  const d = 'M' + ys.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' L');
  const last = ys[n - 1], first = ys[0];
  const color = last > first + 1e-9 ? '#34c759'
              : last < first - 1e-9 ? '#ef4444' : '#6e6e73';
  const cx = X(n - 1).toFixed(1), cy = Y(last).toFixed(1);
  return '<span class="spark"><svg viewBox="0 0 ' + w + ' ' + h + '">' +
    '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.5" ' +
    'stroke-linejoin="round" stroke-linecap="round"/>' +
    '<circle cx="' + cx + '" cy="' + cy + '" r="1.7" fill="' + color + '"/></svg></span>';
}

/* --- movers card ---------------------------------------------------------- */
/* The card component shared by the homepage "Biggest movers"/standings grids and
   the per-market page's "Other outcomes" list. Team badge + question title +
   entity chips + a Vol/24h(+7d)/sparkline foot. Reads the page-global NORM for
   normalized odds. Kept here (not inline in index.html) so both pages reuse it. */
function card(m, opts) {
  opts = opts || {};
  const d24 = fmtDelta(m.delta24h), d7 = fmtDelta(m.delta7d);
  const sub = (opts.showCat ? esc(categoryOf(m).label) + ' · ' : '') + esc(m.eventTitle || '');
  let foot = '<span class="lbl">Vol</span><span>' + fmtVol(m.volume) + '</span>' +
    '<span class="lbl">24h</span><span class="delta ' + d24.cls + '">' + d24.text + '</span>';
  if (opts.show7d) foot += '<span class="lbl">7d</span><span class="delta ' + d7.cls + '">' + d7.text + '</span>';
  foot += m.sparkline && m.sparkline.length >= 2 ? sparkline(m.sparkline) : '';
  return '<div class="card" data-id="' + esc(m.conditionId) + '"' + (opts.extra ? ' hidden data-extra' : '') + '>' +
    '<div class="now"><span class="p">' + probHtml(m, NORM) + '</span></div>' +
    '<div class="q">' + entityThumb(m.conditionId) + settledBadge(m) + esc(displayTitle(m)) + '</div>' +
    '<div class="ev">' + sub + '</div>' + entityChips(m.conditionId) +
    '<div class="card-foot">' + foot + '</div>' +
  '</div>';
}

/* --- misc ----------------------------------------------------------------- */
/* Delegate row/card taps: any element with data-id navigates to its detail. */
function wireRowNav(container) {
  container.addEventListener('click', e => {
    const el = e.target.closest('[data-id]');
    if (el && el.dataset.id) location.href = marketUrl(el.dataset.id);
  });
}
