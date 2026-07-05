/* ==========================================================================
   Market detail renderer. Shared by:
     - docs/market.html?id=<conditionId>   (fallback; redirects to the slug page
       when one exists)
     - docs/market/<slug>/index.html        (pre-generated SEO page; sets
       window.MARKET = { conditionId, ... })

   Depends on app.js (loaded first) and Chart.js. Path-agnostic: it uses the
   data base loadData discovers, so it works at either depth.
   ========================================================================== */
const RANGE_DAYS = { '6h': 0.25, '24h': 1, '7d': 7, '30d': 30, 'all': Infinity };
let CHART = null;
let RACE_CHART = null;
let HISTORY = [];     // [[ms, prob], ...] ascending, prob non-null
let NORM = null;      // conditionId -> normalized probability (from the index)

/* like loadData but reuses the base already discovered by the index.json load. */
async function loadMarketFile(path) {
  const r = await fetch((typeof _dataBase === 'string' ? _dataBase : '../data/') + path);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

/* try the live file, then recent archive months, for a conditionId */
async function loadMarketRecord(id, resolvedAt) {
  const cands = ['markets/' + id + '.json'];
  const months = new Set();
  if (resolvedAt) months.add(resolvedAt.slice(0, 7));
  const now = new Date();
  for (let k = 0; k < 4; k++) {
    const d = new Date(now.getFullYear(), now.getMonth() - k, 1);
    months.add(d.toISOString().slice(0, 7));
  }
  months.forEach(mm => cands.push('archive/' + mm + '/' + id + '.json'));
  for (const c of cands) { try { return await loadMarketFile(c); } catch (e) {} }
  return null;
}

function deltaFromHistory(hours) {
  if (!HISTORY.length) return null;
  const last = HISTORY[HISTORY.length - 1];
  const cutoff = last[0] - hours * 3600 * 1000;
  let base = null;
  for (const p of HISTORY) { if (p[0] <= cutoff) base = p[1]; }
  if (base === null) base = HISTORY[0][1];
  return last[1] - base;
}

/* ---------- single-market chart ---------- */
function rangeLabel(ms, days) {
  const d = new Date(ms);
  if (days <= 1) return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function drawChart(rangeKey) {
  const days = RANGE_DAYS[rangeKey];
  const wrap = document.getElementById('chartwrap');
  let pts = HISTORY;
  if (isFinite(days) && HISTORY.length) {
    const cutoff = HISTORY[HISTORY.length - 1][0] - days * 86400 * 1000;
    pts = HISTORY.filter(p => p[0] >= cutoff);
  }
  if (pts.length < 2) {
    if (CHART) { CHART.destroy(); CHART = null; }
    wrap.innerHTML = '<div class="chart-empty">Not enough data in this range yet.</div>';
    return;
  }
  wrap.innerHTML = '<canvas id="chart"></canvas>';
  const ctx = document.getElementById('chart').getContext('2d');
  const labels = pts.map(p => rangeLabel(p[0], days));
  const data = pts.map(p => Math.round(p[1] * 1000) / 10);
  if (CHART) CHART.destroy();
  CHART = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data, borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.12)',
        borderWidth: 2, fill: true, tension: .25,
        pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: '#3b82f6'
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: c => c.parsed.y + '%' },
          backgroundColor: '#1d1d1f', titleFont: { family: "'JetBrains Mono',monospace" },
          bodyFont: { family: "'JetBrains Mono',monospace" }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 8, autoSkip: true, maxRotation: 0,
            color: '#6e6e73', font: { family: "'JetBrains Mono',monospace", size: 10 },
            callback: function (value, index, ticksArr) {
              const cur = labels[value];
              if (index > 0 && labels[ticksArr[index - 1].value] === cur) return '';
              return cur;
            }
          }
        },
        y: {
          grid: { color: 'rgba(0,0,0,.06)' },
          ticks: {
            callback: v => Math.round(v) + '%',
            color: '#6e6e73', font: { family: "'JetBrains Mono',monospace", size: 10 }
          }
        }
      }
    }
  });
}

function wireRange() {
  const tog = document.getElementById('range');
  if (!tog) return;
  tog.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    tog.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
    drawChart(btn.dataset.range);
  });
}

/* ---------- race chart (item 8): every outcome over time ---------- */
const RACE_COLORS = ['#3b82f6', '#ef4444', '#1d8a40', '#b26b00', '#8b5cf6',
                     '#0891b2', '#db2777', '#65a30d'];

/* short, entity-based label for an outcome (e.g. "New York Knicks" not the full
   question); falls back to a trimmed question when no entity matched. */
/* entity keys (t/slug) matched to a market */
function entityKeys(m) {
  const e = ENTITY_MAP[m.conditionId];
  return ((e && e.all) || []).map(x => x.t + '/' + x.slug);
}

/* a cleaned question fragment, used when no distinguishing entity exists */
function cleanQuestion(m) {
  const q = (m.question || '').replace(/^Will (the )?/i, '').replace(/\?\s*$/, '').trim();
  return q.length > 30 ? q.slice(0, 29) + '…' : q;
}

/* Build a per-outcome labeler for a race. Entities matched to EVERY outcome are
   "common" and don't distinguish (e.g. "LeBron James" on a next-team event);
   each outcome is labeled by its first NON-common entity (the team that differs)
   and falls back to a cleaned question fragment when nothing distinguishes it.
   Events whose outcomes are distinct entities (Champion, Finals MVP) have no
   common entity, so each is labeled by its own team/player. */
function buildLabeler(markets) {
  let common = null;
  markets.forEach(m => {
    const ks = new Set(entityKeys(m));
    common = common === null ? ks : new Set([...common].filter(k => ks.has(k)));
  });
  common = common || new Set();
  return function (m) {
    const e = ENTITY_MAP[m.conditionId];
    const distinct = ((e && e.all) || []).find(x => !common.has(x.t + '/' + x.slug));
    return distinct ? distinct.name : cleanQuestion(m);
  };
}

/* step value of a [ [ms,p], ... ] series at time T (last point with ms<=T) */
function stepAt(series, T) {
  let v = null;
  for (let i = 0; i < series.length; i++) {
    if (series[i][0] <= T) v = series[i][1]; else break;
  }
  return v;
}

async function buildRaceChart(outcomes) {
  // fetch every outcome's history in parallel; keep the ones that load
  const loaded = await Promise.all(outcomes.map(async o => {
    const rec = await loadMarketRecord(o.conditionId, o.resolvedAt);
    const hist = ((rec && rec.history) || [])
      .map(s => [Date.parse(s.timestamp), s.impliedProbability])
      .filter(p => isFinite(p[0]) && isNum(p[1]))
      .sort((a, b) => a[0] - b[0]);
    return { o: o, hist: hist };
  }));
  const series = loaded.filter(x => x.hist.length >= 2);
  if (series.length < 2) return null;

  // merged, de-duplicated timeline across all outcomes
  const tset = {};
  series.forEach(s => s.hist.forEach(p => { tset[p[0]] = 1; }));
  const merged = Object.keys(tset).map(Number).sort((a, b) => a - b);

  // top 8 by current volume; the rest collapse into one gray "Others" line
  series.sort((a, b) => (b.o.volume || 0) - (a.o.volume || 0));
  const top = series.slice(0, 8), rest = series.slice(8);
  const label = buildLabeler(series.map(s => s.o));   // distinguishing labels

  const datasets = top.map((s, i) => ({
    label: label(s.o),
    data: merged.map(t => { const v = stepAt(s.hist, t); return v == null ? null : Math.round(v * 1000) / 10; }),
    borderColor: RACE_COLORS[i % RACE_COLORS.length], backgroundColor: 'transparent',
    borderWidth: 1.8, tension: .25, pointRadius: 0, pointHoverRadius: 3, spanGaps: true
  }));
  if (rest.length) {
    datasets.push({
      label: 'Others (' + rest.length + ')',
      data: merged.map(t => {
        let sum = 0, any = false;
        rest.forEach(s => { const v = stepAt(s.hist, t); if (v != null) { sum += v; any = true; } });
        return any ? Math.round(sum * 1000) / 10 : null;
      }),
      borderColor: '#9a9aa0', backgroundColor: 'transparent', borderWidth: 1.5,
      borderDash: [4, 3], tension: .25, pointRadius: 0, pointHoverRadius: 3, spanGaps: true
    });
  }
  return { merged: merged, datasets: datasets };
}

/* Click a legend name to ISOLATE that outcome (show only it); click the same one
   again to restore all. Defensive on purpose — tolerate missing args so it can
   never silently die the way the previous handler did. */
function raceLegendClick(e, legendItem, legend) {
  const ci = (legend && legend.chart) || (this && this.chart) || RACE_CHART;
  if (!ci || !legendItem || legendItem.datasetIndex == null) return;
  const idx = legendItem.datasetIndex;
  raceSetIsolation(ci, ci._raceIsolated === idx ? null : idx);
}

/* show only dataset `idx`, or all lines when idx is null (the reset) */
function raceSetIsolation(ci, idx) {
  if (!ci || !ci.data) return;
  const n = ci.data.datasets.length;
  for (let i = 0; i < n; i++) ci.setDatasetVisibility(i, idx == null || i === idx);
  ci._raceIsolated = idx;
  ci.update();
  const btn = document.getElementById('race-showall');
  if (btn) btn.hidden = (idx == null);   // the reset is only relevant while isolated
}

function drawRaceChart(built) {
  const wrap = document.getElementById('racewrap');
  if (!built) { wrap.innerHTML = '<div class="chart-empty">Not enough outcome history yet.</div>'; return; }
  wrap.innerHTML = '<canvas id="racechart"></canvas>';
  const labels = built.merged.map(ms => rangeLabel(ms, 7));
  RACE_CHART = new Chart(document.getElementById('racechart').getContext('2d'), {
    type: 'line',
    data: { labels: labels, datasets: built.datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      plugins: {
        legend: {
          display: true, position: 'bottom',
          labels: { boxWidth: 18, font: { family: "'JetBrains Mono',monospace", size: 10 }, color: '#1d1d1f' },
          onClick: raceLegendClick
        },
        tooltip: {
          callbacks: { label: c => c.dataset.label + ': ' + c.parsed.y + '%' },
          backgroundColor: '#1d1d1f', titleFont: { family: "'JetBrains Mono',monospace" },
          bodyFont: { family: "'JetBrains Mono',monospace" }
        }
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            maxTicksLimit: 8, autoSkip: true, maxRotation: 0,
            color: '#6e6e73', font: { family: "'JetBrains Mono',monospace", size: 10 },
            callback: function (value, index, ticksArr) {
              const cur = labels[value];
              if (index > 0 && labels[ticksArr[index - 1].value] === cur) return '';
              return cur;
            }
          }
        },
        y: {
          grid: { color: 'rgba(0,0,0,.06)' },
          ticks: { callback: v => Math.round(v) + '%', color: '#6e6e73', font: { family: "'JetBrains Mono',monospace", size: 10 } }
        }
      }
    }
  });
  const sa = document.getElementById('race-showall');
  if (sa) sa.addEventListener('click', () => raceSetIsolation(RACE_CHART, null));
}

/* lazy-load the race chart only when its section scrolls into view */
function wireRaceChart(outcomes) {
  const sec = document.getElementById('racesec');
  if (!sec) return;
  let done = false;
  const go = () => {
    if (done) return; done = true;
    buildRaceChart(outcomes).then(drawRaceChart).catch(() => {
      document.getElementById('racewrap').innerHTML = '<div class="chart-empty">Couldn’t load outcome history.</div>';
    });
  };
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(es => { es.forEach(e => { if (e.isIntersecting) { go(); io.disconnect(); } }); }, { rootMargin: '200px' });
    io.observe(sec);
  } else { go(); }
}

/* ---------- render ---------- */
function render(record, entry, index) {
  const app = document.getElementById('app');
  const m = record || entry || {};
  const cat = categoryOf(m);
  const resolved = !!(record && record.resolved) || !!(entry && entry.resolved);

  const rawCur = HISTORY.length ? HISTORY[HISTORY.length - 1][1]
               : (entry ? entry.impliedProbability : null);
  const curInfo = (!resolved && entry) ? probInfo(entry, NORM)
                : { value: rawCur, normalized: false, raw: rawCur };

  const d24 = fmtDelta(entry && isNum(entry.delta24h) ? entry.delta24h : deltaFromHistory(24));
  const d7  = fmtDelta(entry && isNum(entry.delta7d)  ? entry.delta7d  : deltaFromHistory(24 * 7));

  const vol = entry ? entry.volume : null;
  const meta = [];
  if (resolved) {
    const when = (record && record.resolvedAt) || (entry && entry.resolvedAt);
    const settled = isNum(rawCur) ? (rawCur >= 0.5 ? 'YES' : 'NO') : null;
    if (settled) meta.push('<span class="resolved">✓ Settled ' + settled + '</span>');
    if (when) meta.push('Resolved ' + fmtDate(when));
  } else {
    const end = (record && record.endDate) || (entry && entry.endDate);
    if (end) meta.push('Ends ' + fmtDate(end));
  }
  if (isNum(vol)) meta.push('Volume ' + fmtVol(vol));
  const metaHtml = meta.map(x => '<span>' + x + '</span>').join('<span class="sep">·</span>');

  const poly = resolved ? null : polyMarketUrl(m);
  const polyLink = poly
    ? '<a class="poly-link" href="' + esc(poly) + '" target="_blank" rel="noopener">View on Polymarket ↗</a>'
    : '';

  const probCls = resolved ? (isNum(rawCur) && rawCur >= 0.5 ? 'pos' : 'neg') : 'flat';
  const probSub = curInfo.normalized
    ? '<div class="sub">raw YES ' + fmtPct(curInfo.raw) + '</div>'
    : (resolved ? '<div class="sub">settled</div>' : '');
  const cards =
    '<div class="stat-cards">' +
      '<div class="stat-card"><div class="lbl">Implied probability' +
        (curInfo.normalized ? ' <span class="tag">normalized</span>' : '') + '</div>' +
        '<div class="num ' + probCls + '">' + fmtPct(curInfo.value) + '</div>' + probSub + '</div>' +
      '<div class="stat-card"><div class="lbl">24h change</div>' +
        '<div class="num ' + d24.cls + '">' + d24.text + '</div></div>' +
      '<div class="stat-card"><div class="lbl">7d change</div>' +
        '<div class="num ' + d7.cls + '">' + d7.text + '</div></div>' +
    '</div>';

  const chart =
    '<div class="section">' +
      '<div class="section-head"><h2>Implied probability over time</h2>' +
        '<div class="toggle" id="range">' +
          ['6h', '24h', '7d', '30d', 'all'].map(r =>
            '<button class="' + (r === '7d' ? 'active' : '') + '" data-range="' + r + '">' +
            (r === 'all' ? 'All' : r) + '</button>').join('') +
        '</div></div>' +
      '<div class="chart-wrap" id="chartwrap"></div>' +
    '</div>';

  // outcomes in the same event (self + siblings)
  let outcomes = [];
  if (index && entry && entry.eventId) {
    outcomes = index.markets.filter(s => s.eventId === entry.eventId && isNum(s.impliedProbability));
  }

  // race chart — only for mutually-exclusive (negRisk) events with >1 outcome
  const isRace = !!(entry && entry.negRisk) && outcomes.length > 1;
  const raceSection = isRace
    ? '<div class="section" id="racesec"><div class="section-head"><h2>The race over time</h2>' +
        '<div class="race-controls"><span class="hint" style="margin:0">every outcome · click a name to isolate it</span>' +
        '<button class="showmore" id="race-showall" hidden>Show all</button></div></div>' +
        '<div class="chart-wrap" id="racewrap"><div class="loading">Loading the field…</div></div></div>'
    : '';

  // siblings — top N by volume as movers-style cards, the rest as a compact list
  let siblings = '';
  if (index && entry && entry.eventId) {
    const all = outcomes.filter(s => s.conditionId !== entry.conditionId)
      .sort((a, b) => (b.volume || 0) - (a.volume || 0));
    if (all.length) {
      const CARD_N = 8;                         // top outcomes get the full movers card
      const cardsHtml = all.slice(0, CARD_N).map(s => card(s, {})).join('');

      // everything past the top N stays a compact row; sub-floor ones stay collapsed
      const sibRow = (s, extra) => {
        const dd = fmtDelta(s.delta24h);
        return '<div class="sib" data-id="' + esc(s.conditionId) + '"' + (extra ? ' hidden data-extra' : '') + '>' +
          '<span class="nm"><a href="' + marketUrl(s.conditionId) + '">' + settledBadge(s) + esc(displayTitle(s)) + '</a></span>' +
          '<span class="v">' + fmtVol(s.volume) + '</span>' +
          '<span class="p">' + probHtml(s, NORM) + '</span>' +
          '<span class="d delta ' + dd.cls + '">' + dd.text + '</span>' +
        '</div>';
      };
      const rest = all.slice(CARD_N);
      const race = rest.filter(passesFloor);
      const low = rest.filter(s => !passesFloor(s));
      const listHtml = rest.length
        ? '<div class="siblings">' +
            race.map(s => sibRow(s, false)).join('') + low.map(s => sibRow(s, true)).join('') +
          '</div>' +
          (low.length ? '<button class="showmore" id="sib-more">Show ' + low.length + ' low-volume →</button>' : '')
        : '';

      const eventUrl = polyEventUrl(entry.eventSlug);
      const floorCount = all.filter(passesFloor).length;
      siblings =
        '<div class="section"><div class="section-head"><h2>Other outcomes in this race</h2>' +
          (eventUrl ? '<a class="poly-link" href="' + esc(eventUrl) + '" target="_blank" rel="noopener">Event on Polymarket ↗</a>' : '') +
          '</div>' +
          '<div class="hint">' + esc(entry.eventTitle || '') + ' · ' + (floorCount || all.length) + ' outcomes' +
            (NORM && NORM.has(entry.conditionId) ? ' · normalized to 100%' : '') + '</div>' +
          '<div class="outcome-cards">' + cardsHtml + '</div>' + listHtml + '</div>';
    }
  }

  const question = (record && record.question) || (entry && entry.question) || 'Market';
  const titleM = { question: question, tags: m.tags, endDate: (record && record.endDate) || (entry && entry.endDate) };
  app.innerHTML =
    '<div class="phead">' +
      '<span class="cat">' + esc(cat.label) + '</span>' +
      '<h1>' + settledBadge(m) + esc(displayTitle(titleM)) + '</h1>' +
      (entry ? entityChips(entry.conditionId) : '') +
      (meta.length ? '<div class="meta">' + metaHtml + '</div>' : '') +
      polyLink +
    '</div>' +
    cards + chart + raceSection + siblings;

  drawChart('7d');
  wireRange();
  wireRowNav(app);
  if (isRace) wireRaceChart(outcomes);
  const sibMore = document.getElementById('sib-more');
  if (sibMore) sibMore.addEventListener('click', () => {
    const open = sibMore.dataset.open === '1';
    document.querySelectorAll('.siblings [data-extra]').forEach(el => { el.hidden = open; });
    sibMore.dataset.open = open ? '0' : '1';
    sibMore.textContent = open
      ? 'Show ' + document.querySelectorAll('.siblings [data-extra]').length + ' low-volume →'
      : 'Hide low-volume ↑';
  });
}

/* ---------- boot ---------- */
(async function () {
  const app = document.getElementById('app');
  const M = (typeof window !== 'undefined' && window.MARKET) || null;
  const id = M ? M.conditionId : new URLSearchParams(location.search).get('id');
  if (!id) { app.innerHTML = '<div class="empty">No market id in the URL.</div>'; return; }
  try {
    const index = await loadData('index.json');            // also sets _dataBase
    registerSlugs(index.markets);
    const entry = (index.markets || []).find(x => x.conditionId === id) || null;

    // fallback page (market.html?id=) -> hop to the SEO slug page when one exists
    if (!M && entry && entry.slug) {
      location.replace(docsBase() + 'market/' + encodeURIComponent(entry.slug) + '/');
      return;
    }

    await loadEntities();
    NORM = computeNormalized(index.markets);
    const record = await loadMarketRecord(id, entry && entry.resolvedAt);
    if (!record && !entry) { app.innerHTML = '<div class="empty">Market not found.</div>'; return; }

    HISTORY = ((record && record.history) || [])
      .map(s => [Date.parse(s.timestamp), s.impliedProbability])
      .filter(p => isFinite(p[0]) && isNum(p[1]))
      .sort((a, b) => a[0] - b[0]);

    const tm = entry || record || {};
    document.title = displayTitle({
      question: (record && record.question) || (entry && entry.question) || 'Market',
      tags: tm.tags, endDate: tm.endDate
    }) + ' · NBA Polymarket';
    render(record, entry, index);
    document.getElementById('foot').innerHTML =
      'Data from Polymarket · updated ' + fmtStamp(index.lastUpdated);
  } catch (err) {
    app.innerHTML = '<div class="empty">Couldn’t load this market.<br>' +
      '<span style="font-size:.8rem">' + esc(String(err && err.message || err)) + '</span></div>';
  }
})();
