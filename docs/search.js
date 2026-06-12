/* ==========================================================================
   Global search (item 10). One component wired into every page via the
   .gsearch inputs that search_box() / the page templates emit (top + bottom).

   Pure client-side: it reads the same index.json + entities.json the rest of
   the site already uses (no external service, no extra network beyond those
   two JSON files), builds a small in-memory corpus of market questions, player
   names and team names, and shows a debounced dropdown of matches that link to
   the market / entity pages. Depends on helpers in app.js (loaded first).
   ========================================================================== */
(function () {
  var inputs = Array.prototype.slice.call(document.querySelectorAll('.gsearch'));
  if (!inputs.length) return;

  var CORPUS = null;     // [{label, sub, url, kind, img}]
  var loading = null;

  function buildCorpus() {
    if (loading) return loading;
    loading = Promise.all([
      loadData('index.json').catch(function () { return { markets: [] }; }),
      loadData('entities.json').catch(function () { return { markets: {} }; })
    ]).then(function (res) {
      var index = res[0], ents = res[1] || {};
      registerSlugs(index.markets);
      var items = [];
      // every player + team (the full roster directory), so search never misses
      // a known name even when the entity has no current markets
      (ents.directory || []).forEach(function (e) {
        items.push({
          label: e.name, sub: e.t === 'player' ? 'Player' : 'Team',
          url: docsBase() + e.t + '/' + e.slug + '/', kind: e.t, img: e.img,
          key: e.name.toLowerCase()
        });
      });
      // markets (question -> market page)
      (index.markets || []).forEach(function (m) {
        items.push({
          label: displayTitle(m), sub: m.eventTitle || '',
          url: marketUrl(m.conditionId), kind: 'market', img: null,
          key: (m.question || '').toLowerCase()
        });
      });
      CORPUS = items;
      return items;
    });
    return loading;
  }

  function search(q) {
    q = q.trim().toLowerCase();
    if (!q || !CORPUS) return [];
    var starts = [], contains = [];
    for (var i = 0; i < CORPUS.length; i++) {
      var idx = CORPUS[i].key.indexOf(q);
      if (idx === 0) starts.push(CORPUS[i]);
      else if (idx > 0) contains.push(CORPUS[i]);
      if (starts.length >= 8) break;
    }
    // entities before markets when equally ranked; cap at 8
    return starts.concat(contains).slice(0, 8);
  }

  function renderResults(box, results) {
    if (!results.length) { box.classList.remove('open'); box.innerHTML = ''; return; }
    box.innerHTML = results.map(function (r) {
      var thumb = r.img
        ? '<img src="' + esc(r.img) + '" alt="" loading="lazy" width="20" height="20" onerror="' + IMG_FALLBACK + '">'
        : '<span class="gs-dot ' + esc(r.kind) + '"></span>';
      return '<a class="gs-item" href="' + esc(r.url) + '">' + thumb +
        '<span class="gs-main">' + esc(r.label) + '</span>' +
        '<span class="gs-sub">' + esc(r.sub) + '</span></a>';
    }).join('');
    box.classList.add('open');
  }

  function wire(input) {
    var box = input.parentNode.querySelector('.gsearch-results');
    var t = null;
    input.addEventListener('input', function () {
      clearTimeout(t);
      var val = input.value;
      t = setTimeout(function () {
        buildCorpus().then(function () { renderResults(box, search(val)); });
      }, 120);
    });
    input.addEventListener('focus', buildCorpus);
    // keep the dropdown while interacting; close on outside click / Esc
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { box.classList.remove('open'); input.blur(); }
    });
    document.addEventListener('click', function (e) {
      if (!input.parentNode.contains(e.target)) box.classList.remove('open');
    });
  }

  inputs.forEach(wire);
})();
