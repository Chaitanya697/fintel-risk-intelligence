(function () {
  var R = JSON.parse(document.getElementById('reportData').textContent);
  var NS = 'http://www.w3.org/2000/svg';

  function inr(n) {
    var neg = n < 0; n = Math.round(Math.abs(n));
    var s = String(n), l = s.slice(-3), r = s.slice(0, -3);
    return (neg ? '-' : '') + '₹' + (r ? r.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + l : l);
  }
  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  // ---------- tabs ----------
  var tabs = document.querySelectorAll('.tab');
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      tabs.forEach(function (x) { x.classList.remove('active'); });
      document.querySelectorAll('.panel').forEach(function (p) { p.classList.remove('active'); });
      t.classList.add('active');
      document.getElementById(t.dataset.tab).classList.add('active');
      if (t.dataset.tab === 'evidence') fillBars();
      history.replaceState(null, '', '#' + t.dataset.tab);
    });
  });
  if (location.hash) { var pre = document.querySelector('.tab[data-tab="' + location.hash.slice(1) + '"]'); if (pre) pre.click(); }

  function fillBars() {
    document.querySelectorAll('.bar-fill').forEach(function (b) { b.style.width = b.dataset.w + '%'; });
  }

  // ---------- Exhibit A: growth chart ----------
  (function drawGrowth() {
    var svg = document.getElementById('growthChart'); if (!svg) return;
    var W = svg.clientWidth || 900, H = 280, pad = { l: 70, r: 14, t: 12, b: 28 };
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    var g = R.growth, all = g.portfolio.concat(g.market);
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all), n = g.portfolio.length;
    var x = function (i) { return pad.l + (W - pad.l - pad.r) * i / (n - 1); };
    var y = function (v) { return pad.t + (H - pad.t - pad.b) * (1 - (v - lo) / (hi - lo || 1)); };
    for (var k = 0; k <= 4; k++) {
      var v = lo + (hi - lo) * k / 4, yy = y(v);
      el('line', { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, stroke: '#1D2840' }, svg);
      el('text', { x: pad.l - 8, y: yy + 4, 'text-anchor': 'end' }, svg).textContent = inr(v);
    }
    var lastYear = '';
    g.dates.forEach(function (d, i) {
      var yr = d.slice(0, 4);
      if (yr !== lastYear && i > 0) { el('text', { x: x(i), y: H - 8, 'text-anchor': 'middle' }, svg).textContent = yr; }
      lastYear = yr;
    });
    function path(series, color, width) {
      var d = series.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1); }).join('');
      return el('path', { d: d, fill: 'none', stroke: color, 'stroke-width': width, 'stroke-linejoin': 'round' }, svg);
    }
    path(g.market, '#7D7564', 1.5);
    var area = g.portfolio.map(function (v, i) { return (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1); }).join('') +
      'L' + x(n - 1) + ',' + (H - pad.b) + 'L' + x(0) + ',' + (H - pad.b) + 'Z';
    el('path', { d: area, fill: 'rgba(201,164,92,.07)' }, svg);
    path(g.portfolio, '#C9A45C', 2.2);
  })();

  // ---------- Exhibit D: correlation heatmap ----------
  (function heat() {
    var t = document.getElementById('heat'); if (!t) return;
    var c = R.correlation, h = '<tr><th></th>' + c.symbols.map(function (s) { return '<th>' + esc(s) + '</th>'; }).join('') + '</tr>';
    c.symbols.forEach(function (s, i) {
      h += '<tr><th style="text-align:right">' + esc(s) + '</th>';
      c.matrix[i].forEach(function (v, j) {
        var bg = i === j ? '#1B2740' : (v >= 0 ? 'rgba(179,64,76,' + Math.min(1, v * 1.1).toFixed(2) + ')' : 'rgba(95,168,138,' + Math.min(1, -v * 1.5).toFixed(2) + ')');
        h += '<td style="background:' + bg + '">' + (i === j ? '—' : v.toFixed(2)) + '</td>';
      });
      h += '</tr>';
    });
    t.innerHTML = h;
  })();

  // ---------- IV: hearings ----------
  function detailTable(res) {
    if (!res.by_stock) return '';
    return '<table class="ledger" style="margin-top:14px"><tr><th>Stock</th><th>Sector</th><th class="r">Assumed move</th><th class="r">Loss</th></tr>' +
      res.by_stock.map(function (b) {
        return '<tr><td><b>' + esc(b.symbol) + '</b></td><td>' + esc(b.sector) + '</td><td class="r">' + b.shock_pct.toFixed(1) + '%</td><td class="r">' + inr(b.loss) + '</td></tr>';
      }).join('') + '</table>';
  }
  function showPreset(i) {
    var s = R.stress[i];
    document.getElementById('presetDetail').innerHTML =
      '<h3>' + esc(s.name) + '</h3><p style="margin-top:6px">' + esc(s.detail) + '</p>' +
      '<div class="result-line"><div>Current value<b>' + inr(R.value) + '</b></div><div>Estimated value after<b>' + inr(s.new_value) +
      '</b></div><div>Potential loss<b style="color:#E5848E">' + inr(s.loss) + '</b></div><div>Portfolio impact<b>' + s.impact_pct.toFixed(2) + '%</b></div></div>' +
      detailTable(s);
  }
  document.querySelectorAll('#presetGrid .hearing').forEach(function (b) {
    b.addEventListener('click', function () {
      document.querySelectorAll('#presetGrid .hearing').forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active'); showPreset(+b.dataset.i);
    });
  });
  if (R.stress.length) showPreset(0);

  var mkt = document.getElementById('mkt'), secp = document.getElementById('secp');
  function lab() { document.getElementById('mktV').textContent = mkt.value + '%'; document.getElementById('secV').textContent = secp.value + '%'; }
  mkt.addEventListener('input', lab); secp.addEventListener('input', lab); lab();
  document.getElementById('customForm').addEventListener('submit', function (e) {
    e.preventDefault();
    var out = document.getElementById('customOut');
    out.innerHTML = '<p style="margin-top:14px"><span class="spinner"></span> Running stress test…</p>';
    var sector = document.getElementById('sec').value;
    fetch('/api/portfolios/' + FINTEL.caseId + '/stress', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': FINTEL.csrf },
      body: JSON.stringify({ market_pct: +mkt.value, sector: sector, sector_pct: sector ? +secp.value : 0 })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (r) {
        if (!r.ok) { out.innerHTML = '<div class="flash error" style="margin-top:14px">' + esc(r.j.error || 'The stress test failed.') + '</div>'; return; }
        var s = r.j;
        out.innerHTML = '<div class="card" style="margin-top:14px"><h3>Market ' + mkt.value + '%' + (sector ? ', ' + esc(sector) + ' ' + secp.value + '%' : '') + '</h3>' +
          '<div class="result-line"><div>Estimated value after<b>' + inr(s.new_value) + '</b></div><div>' + (s.loss >= 0 ? 'Potential loss' : 'Potential gain') +
          '<b style="color:' + (s.loss >= 0 ? '#E5848E' : 'var(--verdigris)') + '">' + inr(Math.abs(s.loss)) + '</b></div><div>Portfolio impact<b>' + s.impact_pct.toFixed(2) + '%</b></div></div>' + detailTable(s) + '</div>';
      }).catch(function () { out.innerHTML = '<div class="flash error" style="margin-top:14px">Could not reach the server. Try again.</div>'; });
  });

  // ---------- VI: model evidence ----------
  (function evidence() {
    var t = document.getElementById('evidenceTable'); if (!t) return;
    var pctF = { volatility: 1, avg_return: 1, max_drawdown: 1, sector_concentration: 1, market_volatility: 1, var_95: 1 };
    R.evidence.forEach(function (e) {
      var val;
      if (pctF[e.feature]) val = (e.value * 100).toFixed(e.feature === 'var_95' ? 2 : 1) + '%';
      else if (e.feature === 'n_stocks') val = String(Math.round(e.value));
      else if (e.feature === 'trading_volume') val = Math.pow(10, e.value).toFixed(2) + '× median stock';
      else val = e.value.toFixed(2);
      var col = 'var(--brass)';
      var tr = document.createElement('tr');
      tr.innerHTML = '<td><b>' + esc(e.label) + '</b></td><td class="r">' + val + '</td>' +
        '<td><div class="pbar" style="grid-template-columns:1fr 44px;margin:0"><span class="t"><i style="width:' + e.percentile + '%;background:' + col + '"></i></span><span class="num">' + e.percentile + '</span></div></td>' +
        '<td class="r">' + (e.importance * 100).toFixed(1) + '%</td>';
      t.appendChild(tr);
    });
  })();
})();
