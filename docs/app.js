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
const DATA_BASES = ['../data/', './data/', 'data/'];
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

function marketUrl(id) { return 'market.html?id=' + encodeURIComponent(id); }

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

/* --- misc ----------------------------------------------------------------- */
/* Delegate row/card taps: any element with data-id navigates to its detail. */
function wireRowNav(container) {
  container.addEventListener('click', e => {
    const el = e.target.closest('[data-id]');
    if (el && el.dataset.id) location.href = marketUrl(el.dataset.id);
  });
}
