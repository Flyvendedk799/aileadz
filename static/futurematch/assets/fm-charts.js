/* fm-charts.js — shared, token-bound Chart.js theme + helpers (FM Data-Viz layer).
 *
 * Load via templates/fm/_charts.html (Chart.js 4.4.0 + fm-charts.css + this file).
 * Every chart on the site goes through FMChart so the whole surface looks identical,
 * re-themes live in dark mode, re-skins under white-label, formats numbers the
 * Danish way and degrades to an honest empty state instead of a blank canvas.
 * Raw `new Chart(` in templates is not allowed (tests/test_charts_layer.py).
 *
 * ── Chart helpers (all return the Chart instance, or null when an empty state was shown)
 *   FMChart.line(canvasId, {labels, series, ...common, fill, max})
 *   FMChart.bar(canvasId,  {labels, series, ...common, horizontal, stacked, max, highlight, valueLabels})
 *   FMChart.stackedBar(canvasId, {labels, series, ...common, horizontal})
 *   FMChart.doughnut(canvasId, {labels, values, ...common, colors, maxSlices})
 *   FMChart.radar(canvasId, {labels, series, ...common, max, step})
 *   FMChart.sparkline(canvasId, {data, color, ...common})
 *   FMChart.hbarRanked(canvasId, {labels, values | data:{name: value}, label, limit, other, ...common, highlight})
 *       Ranked horizontal bars (largest first, one hue, value at the bar tip). Rows past
 *       `limit` (default 10) fold into one muted "Andre" bar; pass other:false to drop them.
 *
 * A "series" is {label, data:[...], color?, dashed?, fill?}. `color` is a CSS color or a
 * token name ('--fm-clay' / 'var(--fm-clay)'); token names are re-read when the theme
 * changes, resolved hex values are not. Without `color`, series take the validated chart
 * palette slots in fixed order (--fm-chart-1..6, see fm-charts.css). `dashed:true` draws
 * the series as a dashed outline (targets/plans); `fill:false` drops a radar/line fill.
 * Bars also accept an array of colours (one per bar) or color:'ramp' — an ordinal ramp of
 * slot 1 (strongest first) for ordered categories; a single entry 'ramp:i/n' is step i of
 * an n-step ramp, and '--fm-chart-muted' is the de-emphasis gray. Doughnut takes
 * colors:[...] or 'ramp'.
 * Keyword/token colours re-theme live; precomputed values (e.g. FMChart.ramp()) do not.
 *
 * ── Common options (all optional; every existing caller keeps working without them)
 *   empty        empty-state text (default 'Ingen data endnu')
 *   format       'number' (default) | 'currency' (→ "1.234 kr") | 'percent' (→ "42%",
 *                values are 0–100) | function(v) → string. Used by ticks, tooltips,
 *                value labels and the accessible summary.
 *   currency     true → format 'currency' (legacy alias).   percent: true → 'percent'.
 *   decimals     fixed number of decimals (default: up to 1; currency 0)
 *   valuePrefix / valueSuffix   strings around every formatted value, e.g. ' ms', ' t'
 *   xFormat      'day' (ISO YYYY-MM-DD → "5. jan") | 'month' (YYYY-MM → "jan 2026") |
 *                function(label) → string. Raw labels stay in the data.
 *   fillDays     line/bar with ISO day labels: true → insert missing days as 0 between
 *                the first and last label; N → exactly the last N calendar days up to today.
 *   reference    {value, label?} or an array of them: a thin dashed target/reference line
 *                on the value axis (axis always stretches to include it); its key ("- - Mål 80")
 *                sits in a band above the plot so it never covers data.
 *   legend       true/false to force; default: shown only when there is more than one series.
 *   footer       function(index) → string|string[]: extra tooltip line(s) per data point.
 *   title        short name used in the accessible summary when the canvas has no aria-label.
 *   table        false → skip the visually-hidden data table rendered after the canvas.
 *   aspectRatio  number → keep that width/height ratio instead of filling the parent box
 *                (default: fill the parent; give the parent a height, e.g. .fm-chart-box).
 *   maxBarThickness  bar thickness cap in px (default 24).
 *   labelMax     truncate long category tick labels to N chars (default 26; tooltip shows all).
 *
 * ── Utilities
 *   FMChart.format(v, opts)        Danish number formatting with the options above.
 *   FMChart.fmtDate(label, style)  'day' | 'dayLong' | 'month' Danish date label.
 *   FMChart.fillDays(labels, seriesOrValues, n?)   gap-fill ISO day series → {labels, data}.
 *   FMChart.ramp(n, color?)        n ordinal steps of one hue (full → light) for ordered
 *                                  categories (funnel stages, bands); default hue = slot 1.
 *   FMChart.muted()                the de-emphasis gray (rest-of-data in emphasis charts).
 *   FMChart.palette() / cssVar(name, fallback) / applyTheme()   (unchanged)
 *   FMChart.get(canvasId) / destroy(canvasId) / refreshTheme()
 *
 * Behaviour: canvases get role="img" and an aria-label summarising the data (an authored
 * aria-label is kept and the summary is linked via aria-describedby); a visually-hidden
 * table carries the values. Charts are tracked by canvas id: rendering the same canvas
 * again destroys the previous instance first. A theme switch (data-theme attribute,
 * OS colour-scheme change, white-label vars) re-reads the tokens and updates every live
 * chart in place.
 */
(function () {
  "use strict";
  if (window.FMChart && window.FMChart.version) return; // included twice → keep the first

  var DA_MONTHS = ['jan', 'feb', 'mar', 'apr', 'maj', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
  // Validated light-mode palette (dataviz validator: band, chroma, CVD, contrast) used
  // when fm-charts.css is missing. The CSS holds the light + dark steps.
  var FALLBACK = ['#069488', '#c65d37', '#4d5bcd', '#b28324', '#c04679', '#297dc2'];

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  // ---------------------------------------------------------------- colour maths
  // Tokens may hold hex, rgb() or (under white-label dark mode) color-mix(); Chart.js
  // and canvas need a plain colour, so everything is normalised to rgba() here.
  var _rgbaCache = {};
  var _probe = null;
  function parseRGBA(c) {
    if (!c || typeof c !== 'string') return null;
    var s = c.trim();
    if (_rgbaCache[s]) return _rgbaCache[s];
    var out = null, m;
    if ((m = /^#([0-9a-f]{3,8})$/i.exec(s))) {
      var h = m[1];
      if (h.length === 3 || h.length === 4) h = h.split('').map(function (x) { return x + x; }).join('');
      if (h.length === 6 || h.length === 8) {
        out = [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16),
               h.length === 8 ? parseInt(h.slice(6, 8), 16) / 255 : 1];
      }
    } else if ((m = /^rgba?\(([^)]+)\)$/i.exec(s))) {
      var p = m[1].split(/[\s,/]+/).filter(Boolean).map(function (x) {
        return /%$/.test(x) ? parseFloat(x) / 100 : parseFloat(x);
      });
      if (p.length >= 3) out = [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    } else if ((m = /^color\(srgb\s+([^)]+)\)$/i.exec(s))) {
      var q = m[1].split(/[\s/]+/).filter(Boolean).map(parseFloat);
      if (q.length >= 3) out = [q[0] * 255, q[1] * 255, q[2] * 255, q.length > 3 ? q[3] : 1];
    } else if (document.body) {
      // Anything else (named colours, color-mix, oklch): let the browser resolve it.
      try {
        if (!_probe) {
          _probe = document.createElement('span');
          _probe.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden';
          document.body.appendChild(_probe);
        }
        _probe.style.color = '';
        _probe.style.color = s;
        var resolved = getComputedStyle(_probe).color;
        if (resolved && resolved !== s) out = parseRGBA(resolved);
      } catch (e) { out = null; }
    }
    if (out && out.every(function (x) { return isFinite(x); })) { _rgbaCache[s] = out; return out; }
    return null;
  }
  function rgbaStr(p, a) {
    return 'rgba(' + Math.round(p[0]) + ',' + Math.round(p[1]) + ',' + Math.round(p[2]) + ',' +
      (+((a == null ? 1 : a) * p[3]).toFixed(3)) + ')';
  }
  function alpha(c, a) { var p = parseRGBA(c); return p ? rgbaStr(p, a) : c; }
  function solid(c) { var p = parseRGBA(c); return p ? rgbaStr(p, 1) : c; }
  // t = weight of c1 (0..1)
  function mix(c1, c2, t) {
    var a = parseRGBA(c1), b = parseRGBA(c2);
    if (!a || !b) return c1;
    return rgbaStr([a[0] * t + b[0] * (1 - t), a[1] * t + b[1] * (1 - t), a[2] * t + b[2] * (1 - t),
                    a[3] * t + b[3] * (1 - t)], 1);
  }
  // Resolve a series colour: token names are looked up now (and again on re-theme).
  function resolveColor(c, fallback) {
    if (Array.isArray(c)) return c.map(function (x) { return resolveColor(x, fallback); });
    if (c == null || c === '') return fallback;
    if (typeof c !== 'string') return c;
    var r = /^ramp:(\d+)\/(\d+)$/.exec(c.trim());   // 'ramp:i/n' → step i of an n-step ramp
    if (r) return ramp(+r[2])[Math.min(+r[1], +r[2] - 1)];
    var m = /^var\((--[\w-]+)(?:\s*,\s*([^)]+))?\)$/.exec(c.trim());
    if (m) return solid(cssVar(m[1], m[2] || fallback));
    if (/^--[\w-]+$/.test(c.trim())) return solid(cssVar(c.trim(), fallback));
    return solid(c);
  }

  function brandOverridden() {
    return !!(document.getElementById('fm-wl-vars') || document.getElementById('fm-brand-vars'));
  }

  // Categorical chart palette, fixed order, never cycled past 6 by the helpers below.
  // A white-label tenant's own primary/accent take slots 1 and 2.
  function palette() {
    var p = FALLBACK.map(function (f, i) { return solid(cssVar('--fm-chart-' + (i + 1), f)); });
    if (brandOverridden()) {
      p[0] = solid(cssVar('--fm-primary', p[0]));
      p[1] = solid(cssVar('--fm-clay', p[1]));
    }
    return p;
  }
  function muted() { return solid(cssVar('--fm-chart-muted', cssVar('--fm-line-2', '#d6d4c8'))); }
  function surface() { return solid(cssVar('--fm-surface', '#ffffff')); }
  function ink(n) { return solid(cssVar(n === 1 ? '--fm-ink' : n === 2 ? '--fm-ink-2' : '--fm-ink-3', n === 1 ? '#1a211d' : n === 2 ? '#565f58' : '#8b938c')); }
  function gridColor() { return solid(cssVar('--fm-chart-grid', cssVar('--fm-line', '#e6e4da'))); }

  function softTint(color) { return alpha(color, 0.10); }

  // Ordinal ramp: n steps of one hue from light (still visible on the surface) to full.
  function ramp(n, color) {
    var base = resolveColor(color, palette()[0]);
    var bg = surface();
    n = Math.max(1, n | 0);
    var out = [];
    for (var i = 0; i < n; i++) {
      var t = n === 1 ? 1 : 0.38 + 0.62 * (i / (n - 1));
      out.push(mix(base, bg, t));
    }
    return out.reverse(); // first category = strongest (funnel top, best band)
  }

  // ---------------------------------------------------------------- number / date format
  function nf(v, minD, maxD) {
    try {
      return Number(v).toLocaleString('da-DK', { minimumFractionDigits: minD, maximumFractionDigits: maxD });
    } catch (e) { return String(v); }
  }
  function compactNf(v) {
    try { return new Intl.NumberFormat('da-DK', { notation: 'compact', maximumFractionDigits: 1 }).format(v); }
    catch (e) { return nf(v, 0, 0); }
  }
  function fmtKind(opts) {
    opts = opts || {};
    if (opts.format) return opts.format;
    if (opts.currency) return 'currency';
    if (opts.percent) return 'percent';
    return 'number';
  }
  // format(v, opts[, compact]) → Danish string. compact shortens big tick values ("12,5 t.").
  function format(v, opts, compact) {
    opts = opts || {};
    if (v == null || v === '') return '–';
    var n = Number(v);
    if (!isFinite(n)) return String(v);
    var kind = fmtKind(opts);
    if (typeof kind === 'function') return kind(n);
    var d = opts.decimals;
    var s;
    if (compact && Math.abs(n) >= 10000) s = compactNf(n);
    else if (kind === 'currency') s = nf(n, d == null ? 0 : d, d == null ? 0 : d);
    else s = nf(n, d == null ? 0 : d, d == null ? 1 : d);
    if (kind === 'currency') s += ' kr';
    else if (kind === 'percent') s += '%';
    return (opts.valuePrefix || '') + s + (opts.valueSuffix || '');
  }

  function parseISO(label) {
    var m = /^(\d{4})-(\d{2})(?:-(\d{2}))?/.exec(String(label || ''));
    if (!m) return null;
    return { y: +m[1], m: +m[2], d: m[3] ? +m[3] : null };
  }
  function fmtDate(label, style) {
    var p = parseISO(label);
    if (!p) return String(label == null ? '' : label);
    var mon = DA_MONTHS[p.m - 1] || '';
    if (style === 'month' || p.d == null) return mon + ' ' + p.y;
    if (style === 'dayLong') return p.d + '. ' + mon + ' ' + p.y;
    return p.d + '. ' + mon;
  }
  function isoDay(dt) {
    return dt.getFullYear() + '-' + ('0' + (dt.getMonth() + 1)).slice(-2) + '-' + ('0' + dt.getDate()).slice(-2);
  }
  // Gap-fill an ISO-day series. `data` is one array or an array of series ({data}).
  // n (optional) → exactly the last n calendar days ending today.
  function fillDays(labels, data, n) {
    labels = labels || [];
    var multi = Array.isArray(data) && data.length && data[0] && typeof data[0] === 'object' && !Array.isArray(data[0]);
    var seriesData = multi ? data.map(function (s) { return s.data || []; }) : [data || []];
    var maps = seriesData.map(function (arr) {
      var m = {};
      labels.forEach(function (l, i) { var k = String(l).slice(0, 10); m[k] = (m[k] || 0) + (Number(arr[i]) || 0); });
      return m;
    });
    var start, end;
    if (n && n > 0) {
      end = new Date(); end.setHours(0, 0, 0, 0);
      start = new Date(end); start.setDate(end.getDate() - (n - 1));
    } else {
      var parsed = labels.map(parseISO).filter(function (p) { return p && p.d != null; });
      if (!parsed.length) return { labels: labels, data: multi ? data : (data || []) };
      var f = parsed[0], l = parsed[parsed.length - 1];
      start = new Date(f.y, f.m - 1, f.d); end = new Date(l.y, l.m - 1, l.d);
      if (end < start) { var t = start; start = end; end = t; }
    }
    var outLabels = [];
    var guard = 0;
    for (var dt = new Date(start); dt <= end && guard < 3700; dt.setDate(dt.getDate() + 1), guard++) outLabels.push(isoDay(dt));
    var outData = maps.map(function (m) { return outLabels.map(function (k) { return m[k] || 0; }); });
    if (multi) {
      return { labels: outLabels, data: data.map(function (s, i) { return Object.assign({}, s, { data: outData[i] }); }) };
    }
    return { labels: outLabels, data: outData[0] };
  }
  function labelFormatter(opts, long) {
    var xf = opts.xFormat;
    if (typeof xf === 'function') return xf;
    if (xf === 'day') return function (l) { return fmtDate(l, long ? 'dayLong' : 'day'); };
    if (xf === 'month') return function (l) { return fmtDate(l, 'month'); };
    return function (l) { return l == null ? '' : String(l); };
  }
  function truncate(s, n) {
    s = String(s == null ? '' : s);
    return n && s.length > n ? s.slice(0, Math.max(1, n - 1)) + '…' : s;
  }

  // ---------------------------------------------------------------- theme
  var _themed = false;
  function applyTheme() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.color = ink(3);
    Chart.defaults.borderColor = gridColor();
    try { Chart.defaults.font.family = cssVar('--ff-body', "'Hanken Grotesk', system-ui, sans-serif"); } catch (e) {}
    try {
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        Chart.defaults.animation = false;
      } else {
        Chart.defaults.animation.duration = 450;
      }
    } catch (e) {}
    registerPlugins();
    _themed = true;
  }

  function tooltip(opts, nSeries) {
    // Token-bound so the tooltip surface inverts in dark mode and re-skins under white-label.
    var bg = solid(cssVar('--fm-tooltip-bg', '#1a211d'));
    var fg = solid(cssVar('--fm-tooltip-ink', '#ffffff'));
    var t = {
      backgroundColor: bg, titleColor: fg, bodyColor: fg, footerColor: alpha(fg, 0.8),
      padding: 10, cornerRadius: 8, displayColors: nSeries == null ? true : nSeries > 1,
      boxWidth: 8, boxHeight: 8, boxPadding: 4, usePointStyle: true,
      titleFont: { weight: '600' }, bodyFont: { size: 12 }, footerFont: { size: 11.5, weight: '400' },
      caretSize: 5
    };
    if (opts && typeof opts.footer === 'function') {
      t.callbacks = { footer: function (items) { return items.length ? opts.footer(items[0].dataIndex, items) : ''; } };
    }
    return t;
  }
  function withCallbacks(t, cb) {
    t.callbacks = Object.assign({}, t.callbacks || {}, cb);
    return t;
  }
  function legend(opts, nSeries) {
    var show = opts.legend != null ? !!opts.legend : nSeries > 1;
    return {
      display: show, position: 'bottom',
      labels: { color: ink(2), usePointStyle: true, pointStyle: 'circle', boxWidth: 8, boxHeight: 8, padding: 14,
                font: { size: 12 } }
    };
  }
  // Value axis: hairline grid, no axis rule, Danish tick format.
  function valueAxis(opts, extra) {
    var a = {
      beginAtZero: true,
      grid: { color: gridColor(), lineWidth: 1, drawTicks: false },
      border: { display: false },
      ticks: { color: ink(3), padding: 6, maxTicksLimit: 6,
               callback: function (v) { return format(v, opts, true); } }
    };
    if (!opts.decimals && fmtKind(opts) !== 'percent') a.ticks.precision = 0;
    return Object.assign(a, extra || {});
  }
  // Category axis: no grid, readable (truncated) labels.
  // On a vertical (y) category axis every row keeps its label, shortened to fit roughly a
  // third of the chart width; on x the axis skips labels instead of rotating them.
  function categoryAxis(opts, extra, vertical) {
    var fl = labelFormatter(opts, false);
    var max = opts.labelMax == null ? 26 : opts.labelMax;
    return Object.assign({
      grid: { display: false },
      border: { display: false },
      ticks: { color: ink(3), padding: 4, autoSkipPadding: 12, maxRotation: 0, autoSkip: !vertical,
               callback: function (v) {
                 var lim = max;
                 if (vertical && this.chart && this.chart.width) {
                   lim = Math.min(max, Math.max(8, Math.floor(this.chart.width * 0.34 / 6.4)));
                 }
                 return truncate(fl(this.getLabelForValue(v)), lim);
               } }
    }, extra || {});
  }

  // ---------------------------------------------------------------- plugins
  var _pluginsRegistered = false;
  function registerPlugins() {
    if (_pluginsRegistered || typeof Chart === 'undefined' || !Chart.register) return;
    _pluginsRegistered = true;
    Chart.register({
      id: 'fmCrosshair',
      beforeDatasetsDraw: function (chart) {
        var fm = chart.$fm;
        if (!fm || !fm.crosshair || !chart.tooltip) return;
        var act = chart.tooltip.getActiveElements ? chart.tooltip.getActiveElements() : [];
        if (!act.length) return;
        var x = act[0].element.x, a = chart.chartArea, ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = fm.crosshair; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, a.top); ctx.lineTo(Math.round(x) + 0.5, a.bottom); ctx.stroke();
        ctx.restore();
      }
    });
    Chart.register({
      id: 'fmReference',
      afterDatasetsDraw: function (chart) {
        var fm = chart.$fm;
        if (!fm || !fm.reference || !fm.reference.length) return;
        var horizontal = chart.options.indexAxis === 'y';
        var scale = chart.scales[horizontal ? 'x' : 'y'];
        if (!scale) return;
        var a = chart.chartArea, ctx = chart.ctx;
        fm.reference.forEach(function (r) {
          var v = Number(r.value);
          if (!isFinite(v)) return;
          var px = scale.getPixelForValue(v);
          ctx.save();
          ctx.strokeStyle = fm.refColor; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
          ctx.beginPath();
          if (horizontal) { ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); }
          else { ctx.moveTo(a.left, px); ctx.lineTo(a.right, px); }
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.restore();
        });
        // Keys sit in the reserved band above the plot (never on top of the data):
        // a short dashed swatch + "Label 3,4", left to right.
        var x = a.left, y = a.top - 10;
        ctx.save();
        ctx.font = '600 11px ' + (Chart.defaults.font.family || 'sans-serif');
        ctx.textBaseline = 'middle';
        fm.reference.forEach(function (r) {
          var v = Number(r.value);
          if (!isFinite(v)) return;
          var text = (r.label ? r.label + ' ' : '') + format(v, fm.opts);
          ctx.strokeStyle = fm.refColor; ctx.lineWidth = 1.5; ctx.setLineDash([4, 3]);
          ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 16, y); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = fm.refInk;
          ctx.fillText(text, x + 21, y);
          x += 21 + ctx.measureText(text).width + 14;
        });
        ctx.restore();
      }
    });
    Chart.register({
      id: 'fmValueLabels',
      afterDatasetsDraw: function (chart) {
        var fm = chart.$fm;
        if (!fm || !fm.valueLabels) return;
        var meta = chart.getDatasetMeta(0);
        if (!meta || meta.hidden) return;
        var horizontal = chart.options.indexAxis === 'y';
        var data = chart.data.datasets[0].data;
        var ctx = chart.ctx;
        ctx.save();
        ctx.font = '600 11px ' + (Chart.defaults.font.family || 'sans-serif');
        ctx.fillStyle = fm.valueInk;
        meta.data.forEach(function (bar, i) {
          var v = data[i];
          if (v == null || !isFinite(Number(v))) return;
          var text = format(v, fm.opts);
          if (horizontal) { ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(text, bar.x + 6, bar.y); }
          else { ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.fillText(text, bar.x, bar.y - 4); }
        });
        ctx.restore();
      }
    });
  }
  function fmState(opts, extra) {
    return Object.assign({
      opts: opts,
      surface: surface(),
      refColor: ink(3),
      refInk: ink(2),
      valueInk: ink(2),
      reference: refs(opts)
    }, extra || {});
  }
  function refs(opts) {
    var r = opts.reference;
    if (r == null) return [];
    if (!Array.isArray(r)) r = [r];
    return r.map(function (x) { return typeof x === 'object' ? x : { value: x }; })
            .filter(function (x) { return x && isFinite(Number(x.value)); });
  }
  function refPad(opts) { return refs(opts).length ? 20 : 0; }
  function refMax(opts) {
    var r = refs(opts);
    return r.length ? Math.max.apply(null, r.map(function (x) { return Number(x.value); })) : null;
  }

  // ---------------------------------------------------------------- canvas plumbing
  function el(id) { return typeof id === 'string' ? document.getElementById(id) : id; }
  var _registry = {};
  var _uid = 0;
  function keyFor(canvas) {
    if (!canvas.id) canvas.id = 'fmchart-' + (++_uid);
    return canvas.id;
  }

  function flatten(series) {
    var out = [];
    (series || []).forEach(function (s) { (s.data || []).forEach(function (v) { out.push(Number(v) || 0); }); });
    return out;
  }
  function hasData(values) { return values.some(function (v) { return Number(v) > 0; }); }

  function emptyOut(canvas, msg) {
    if (!canvas) return;
    var key = canvas.id;
    if (key) forget(key);
    var box = document.createElement('div');
    box.className = 'fm-chart-empty';
    box.setAttribute('role', 'note');
    var i = document.createElement('i');
    i.className = 'fa-solid fa-chart-line';
    i.setAttribute('aria-hidden', 'true');
    var span = document.createElement('span');
    span.textContent = msg || 'Ingen data endnu';
    box.appendChild(i); box.appendChild(span);
    if (canvas.parentNode) canvas.parentNode.replaceChild(box, canvas);
  }

  function guard(canvasId, values, emptyMsg) {
    if (!_themed) applyTheme();
    var canvas = el(canvasId);
    if (!canvas) return null;
    if (typeof Chart === 'undefined') { emptyOut(canvas, 'Diagrammer kunne ikke indlæses'); return null; }
    if (!hasData(values)) { emptyOut(canvas, emptyMsg); return null; }
    return canvas;
  }

  function forget(key) {
    var r = _registry[key];
    if (r && r.chart) { try { r.chart.destroy(); } catch (e) {} }
    delete _registry[key];
  }

  function baseOptions(opts) {
    var o = { responsive: true, maintainAspectRatio: false };
    if (opts.aspectRatio) { o.maintainAspectRatio = true; o.aspectRatio = Number(opts.aspectRatio); }
    return o;
  }

  // Build → create (destroying any previous chart on this canvas) → track → describe.
  function render(kind, canvas, opts, model) {
    var key = keyFor(canvas);
    forget(key);
    try { var prev = Chart.getChart && Chart.getChart(canvas); if (prev) prev.destroy(); } catch (e) {}
    var cfg = BUILD[kind](opts, model);
    var chart = new Chart(canvas, { type: cfg.type, data: cfg.data, options: cfg.options });
    chart.$fm = cfg.fm;
    _registry[key] = { chart: chart, kind: kind, opts: opts, model: model };
    describe(canvas, kind, opts, model);
    return chart;
  }

  // ---------------------------------------------------------------- accessibility
  var KIND_NAME = {
    line: 'Linjediagram', bar: 'Søjlediagram', hbar: 'Vandret søjlediagram', stackedBar: 'Stablet søjlediagram',
    doughnut: 'Ringdiagram', radar: 'Radardiagram', sparkline: 'Minidiagram', hbarRanked: 'Rangeret søjlediagram'
  };
  function summary(kind, opts, model) {
    var labels = model.labels || [];
    var lf = labelFormatter(opts, true);
    var parts = [];
    if (kind === 'doughnut') {
      var total = model.values.reduce(function (a, b) { return a + b; }, 0) || 1;
      parts.push(labels.map(function (l, i) {
        return lf(l) + ' ' + format(model.values[i], opts) + ' (' + nf(model.values[i] / total * 100, 0, 0) + '%)';
      }).join(', '));
    } else {
      model.series.forEach(function (s) {
        // null = no point (e.g. a month no cohort has reached), not a zero.
        var d = (s.data || []).map(function (v) { return v == null ? null : (Number(v) || 0); });
        var idx = [];
        d.forEach(function (v, i) { if (v != null) idx.push(i); });
        if (!idx.length) return;
        var hi = idx[0], lo = idx[0], last = idx[idx.length - 1];
        idx.forEach(function (i) { if (d[i] > d[hi]) hi = i; if (d[i] < d[lo]) lo = i; });
        var name = s.label ? s.label + ': ' : '';
        if (kind === 'line' || kind === 'sparkline') {
          parts.push(name + idx.length + ' punkter' +
            (labels.length ? ' fra ' + lf(labels[0]) + ' til ' + lf(labels[labels.length - 1]) : '') +
            ', højeste ' + format(d[hi], opts) + (labels[hi] != null ? ' (' + lf(labels[hi]) + ')' : '') +
            ', seneste ' + format(d[last], opts));
        } else {
          parts.push(name + 'højeste ' + (labels[hi] != null ? lf(labels[hi]) + ' ' : '') + format(d[hi], opts) +
            ', laveste ' + (labels[lo] != null ? lf(labels[lo]) + ' ' : '') + format(d[lo], opts));
        }
      });
    }
    var refsTxt = refs(opts).map(function (r) { return (r.label || 'Reference') + ' ' + format(r.value, opts); });
    if (refsTxt.length) parts.push(refsTxt.join(', '));
    var head = KIND_NAME[kind] || 'Diagram';
    if (opts.title) head += ': ' + opts.title;
    return head + '. ' + parts.join('. ') + '.';
  }
  function describe(canvas, kind, opts, model) {
    try {
      var text = summary(kind, opts, model);
      canvas.setAttribute('role', 'img');
      var authored = canvas.getAttribute('aria-label') && canvas.getAttribute('data-fm-aria') !== 'auto';
      var tableId = canvas.id + '-fmdata';
      var old = document.getElementById(tableId);
      if (old && old.parentNode) old.parentNode.removeChild(old);
      if (!authored) {
        canvas.setAttribute('aria-label', text);
        canvas.setAttribute('data-fm-aria', 'auto');
      }
      if (opts.table === false || kind === 'sparkline') {
        if (authored && canvas.getAttribute('aria-describedby') === tableId) canvas.removeAttribute('aria-describedby');
        return;
      }
      var tbl = document.createElement('table');
      tbl.id = tableId;
      tbl.className = 'fm-sr-only';
      var cap = document.createElement('caption');
      cap.textContent = text;
      tbl.appendChild(cap);
      var lf = labelFormatter(opts, true);
      var labels = model.labels || [];
      var cols = kind === 'doughnut' ? [{ label: 'Værdi', data: model.values }] : model.series;
      var thead = document.createElement('thead');
      var hr = document.createElement('tr');
      [''].concat(cols.map(function (s) { return s.label || 'Værdi'; })).forEach(function (h) {
        var th = document.createElement('th'); th.scope = 'col'; th.textContent = h; hr.appendChild(th);
      });
      thead.appendChild(hr); tbl.appendChild(thead);
      var tb = document.createElement('tbody');
      labels.slice(0, 400).forEach(function (l, i) {
        var tr = document.createElement('tr');
        var th = document.createElement('th'); th.scope = 'row'; th.textContent = lf(l); tr.appendChild(th);
        cols.forEach(function (s) {
          var td = document.createElement('td'); td.textContent = format((s.data || [])[i], opts); tr.appendChild(td);
        });
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      if (canvas.parentNode) canvas.parentNode.insertBefore(tbl, canvas.nextSibling);
      if (authored) canvas.setAttribute('aria-describedby', tableId);
    } catch (e) { /* a11y extras must never break the chart */ }
  }

  // ---------------------------------------------------------------- builders
  // Each builder turns (opts, model) into a Chart.js config. They are pure in the sense
  // that calling them again after a theme change yields the re-themed config.
  function seriesColor(s, i, cols, n) {
    if (s.color === 'ramp') return ramp(n || (s.data || []).length);
    return resolveColor(s.color, cols[Math.min(i, cols.length - 1)]);
  }

  var BUILD = {};

  BUILD.line = function (opts, model) {
    var cols = palette();
    var series = model.series;
    var n = model.labels.length;
    var single = series.length === 1;
    var fillDefault = opts.fill != null ? opts.fill !== false : single;
    var points = n <= 14;
    var surf = surface();
    var y = valueAxis(opts);
    if (opts.max != null) y.max = Number(opts.max);
    var rm = refMax(opts);
    if (rm != null) y.suggestedMax = rm;
    if (opts.beginAtZero === false) y.beginAtZero = false;
    var lfLong = labelFormatter(opts, true);
    return {
      type: 'line',
      data: {
        labels: model.labels,
        datasets: series.map(function (s, i) {
          var c = seriesColor(s, i, cols);
          return {
            label: s.label || '', data: s.data || [], borderColor: c, backgroundColor: softTint(c),
            borderWidth: 2, borderDash: s.dashed ? [6, 4] : undefined,
            fill: s.fill != null ? !!s.fill : (s.dashed ? false : fillDefault),
            tension: 0.3, cubicInterpolationMode: 'monotone',
            borderJoinStyle: 'round', borderCapStyle: 'round',
            pointRadius: points ? 3 : 0, pointHoverRadius: 5, pointHitRadius: 12,
            pointBackgroundColor: c, pointBorderColor: surf, pointBorderWidth: points ? 2 : 0,
            pointHoverBackgroundColor: c, pointHoverBorderColor: surf, pointHoverBorderWidth: 2
          };
        })
      },
      options: Object.assign(baseOptions(opts), {
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { top: refPad(opts) } },
        plugins: {
          legend: legend(opts, series.length),
          tooltip: withCallbacks(tooltip(opts, series.length), {
            title: function (items) { return items.length ? lfLong(items[0].label) : ''; },
            label: function (ctx) { return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + format(ctx.parsed.y, opts); }
          })
        },
        scales: { y: y, x: categoryAxis(opts) }
      }),
      fm: fmState(opts, { crosshair: alpha(ink(3), 0.45) })
    };
  };

  BUILD.bar = function (opts, model) {
    var cols = palette();
    var series = model.series;
    var horizontal = !!opts.horizontal;
    var stacked = !!opts.stacked;
    var mut = muted();
    var surf = surface();
    var hl = model.highlight;
    var lfLong = labelFormatter(opts, true);
    var valueLabels = opts.valueLabels != null ? !!opts.valueLabels : false;
    var vAxis = valueAxis(opts, { stacked: stacked });
    var cAxis = categoryAxis(opts, { stacked: stacked }, horizontal);
    if (opts.max != null) { vAxis.max = Number(opts.max); vAxis.beginAtZero = true; }
    var rm = refMax(opts);
    if (rm != null) vAxis.suggestedMax = rm;
    if (valueLabels && series.length === 1 && !stacked) {
      // Values sit at the bar tips, so the value axis only needs its grid.
      vAxis.ticks.display = false;
      vAxis.grid.display = false;
    }
    var scales = horizontal ? { x: vAxis, y: cAxis } : { x: cAxis, y: vAxis };
    var maxLen = 0;
    if (valueLabels) model.series[0].data.forEach(function (v) { maxLen = Math.max(maxLen, format(v, opts).length); });
    return {
      type: 'bar',
      data: {
        labels: model.labels,
        datasets: series.map(function (s, i) {
          var c = seriesColor(s, i, cols, model.labels.length);
          if (hl && series.length === 1) {
            var base = Array.isArray(c) ? c[0] : c;
            c = model.labels.map(function (_, j) { return hl[j] ? base : mut; });
          }
          if (model.mutedIndex != null && series.length === 1) {
            var b2 = Array.isArray(c) ? c.slice() : model.labels.map(function () { return c; });
            b2[model.mutedIndex] = mut;
            c = b2;
          }
          return {
            label: s.label || '', data: s.data || [], backgroundColor: c,
            borderRadius: stacked ? 0 : 4, borderSkipped: 'start',
            borderWidth: stacked ? 1 : 0, borderColor: surf,
            maxBarThickness: opts.maxBarThickness || 24,
            categoryPercentage: 0.8, barPercentage: 0.9
          };
        })
      },
      options: Object.assign(baseOptions(opts), {
        indexAxis: horizontal ? 'y' : 'x',
        interaction: { mode: 'index', intersect: false, axis: horizontal ? 'y' : 'x' },
        layout: { padding: {
          right: valueLabels && horizontal ? 10 + maxLen * 6.5 : 0,
          top: (valueLabels && !horizontal ? 18 : 0) + refPad(opts)
        } },
        plugins: {
          legend: legend(opts, series.length),
          tooltip: withCallbacks(tooltip(opts, series.length), {
            title: function (items) { return items.length ? lfLong(items[0].label) : ''; },
            label: function (ctx) {
              var v = ctx.parsed[horizontal ? 'x' : 'y'];
              return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + format(v, opts);
            }
          })
        },
        scales: scales
      }),
      fm: fmState(opts, { valueLabels: valueLabels && series.length === 1 && !stacked })
    };
  };

  BUILD.stackedBar = function (opts, model) {
    return BUILD.bar(Object.assign({}, opts, { stacked: true }), model);
  };

  BUILD.hbarRanked = function (opts, model) {
    var o = Object.assign({ valueLabels: true, labelMax: 30 }, opts, { horizontal: true, stacked: false });
    return BUILD.bar(o, model);
  };

  BUILD.doughnut = function (opts, model) {
    var cols = palette();
    var colors = opts.colors === 'ramp' ? ramp(model.values.length)
      : (opts.colors ? resolveColor(opts.colors, cols[0]) : null);
    var mut = muted();
    var bg = model.values.map(function (_, i) {
      if (model.otherIndex === i) return mut;
      if (colors && colors[i]) return colors[i];
      return cols[Math.min(i, cols.length - 1)];
    });
    var lfLong = labelFormatter(opts, true);
    return {
      type: 'doughnut',
      data: { labels: model.labels,
              datasets: [{ data: model.values, backgroundColor: bg, borderWidth: 2, borderColor: surface(),
                           hoverOffset: 4, borderRadius: 2 }] },
      options: Object.assign(baseOptions(opts), {
        cutout: '64%',
        plugins: {
          legend: Object.assign(legend(opts, 2), { display: opts.legend != null ? !!opts.legend : true }),
          tooltip: withCallbacks(tooltip(opts, 2), {
            title: function (items) { return items.length ? lfLong(items[0].label) : ''; },
            label: function (ctx) {
              var total = ctx.dataset.data.reduce(function (a, b) { return a + (Number(b) || 0); }, 0) || 1;
              return format(ctx.parsed, opts) + ' (' + nf(ctx.parsed / total * 100, 0, 0) + '%)';
            }
          })
        }
      }),
      fm: fmState(opts)
    };
  };

  BUILD.radar = function (opts, model) {
    var cols = palette();
    var series = model.series;
    var grid = gridColor();
    return {
      type: 'radar',
      data: {
        labels: model.labels,
        datasets: series.map(function (s, i) {
          var c = seriesColor(s, i, cols);
          return {
            label: s.label || '', data: s.data || [], borderColor: c,
            backgroundColor: softTint(c), borderWidth: 2, borderDash: s.dashed ? [6, 4] : undefined,
            fill: s.fill != null ? !!s.fill : !s.dashed,
            pointRadius: 3, pointHoverRadius: 5, pointHitRadius: 10,
            pointBackgroundColor: c, pointBorderColor: surface(), pointBorderWidth: 1
          };
        })
      },
      options: Object.assign(baseOptions(opts), {
        plugins: {
          legend: legend(opts, series.length),
          tooltip: withCallbacks(tooltip(opts, series.length), {
            label: function (ctx) { return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + format(ctx.parsed.r, opts); }
          })
        },
        scales: {
          r: {
            beginAtZero: true,
            suggestedMax: opts.max || undefined,
            angleLines: { color: grid },
            grid: { color: grid },
            pointLabels: { color: ink(2), font: { size: 11 },
                           callback: function (l) { return truncate(l, opts.labelMax || 22); } },
            ticks: { display: true, precision: 0, backdropColor: 'transparent', color: ink(3),
                     stepSize: opts.step || undefined }
          }
        }
      }),
      fm: fmState(opts)
    };
  };

  BUILD.sparkline = function (opts, model) {
    var c = resolveColor(opts.color, palette()[0]);
    var data = model.series[0].data;
    return {
      type: 'line',
      data: {
        labels: model.labels,
        datasets: [{
          data: data, borderColor: c, backgroundColor: softTint(c),
          borderWidth: 2, fill: true, tension: 0.3, cubicInterpolationMode: 'monotone',
          pointRadius: 0, pointHoverRadius: 3, pointHitRadius: 8, pointHoverBackgroundColor: c
        }]
      },
      options: Object.assign(baseOptions(opts), {
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: withCallbacks(tooltip(opts, 1), {
            title: function () { return ''; }, label: function (ctx) { return format(ctx.parsed.y, opts); }
          })
        },
        scales: { x: { display: false }, y: { display: false } },
        elements: { line: { borderCapStyle: 'round' } }
      }),
      fm: fmState(opts)
    };
  };

  // ---------------------------------------------------------------- public helpers
  function normSeries(series) {
    return (series || []).map(function (s) {
      return Object.assign({}, s, { data: (s.data || []).map(function (v) { return v == null ? null : (Number(v) || 0); }) });
    });
  }
  function prepXY(opts) {
    var labels = (opts.labels || []).slice();
    var series = normSeries(opts.series);
    if (opts.fillDays && series.length) {
      var f = fillDays(labels, series, typeof opts.fillDays === 'number' ? opts.fillDays : null);
      labels = f.labels; series = f.data;
    }
    return { labels: labels, series: series };
  }
  function highlightMap(h, labels) {
    if (h == null || h === false) return null;
    var list = Array.isArray(h) ? h : [h];
    var map = {};
    list.forEach(function (x) {
      if (typeof x === 'number') map[x] = true;
      else labels.forEach(function (l, i) { if (String(l) === String(x)) map[i] = true; });
    });
    return Object.keys(map).length ? map : null;
  }

  function line(canvasId, opts) {
    opts = opts || {};
    var model = prepXY(opts);
    var canvas = guard(canvasId, flatten(model.series), opts.empty);
    if (!canvas) return null;
    return render('line', canvas, opts, model);
  }

  function bar(canvasId, opts) {
    opts = opts || {};
    var model = prepXY(opts);
    model.highlight = highlightMap(opts.highlight, model.labels);
    var canvas = guard(canvasId, flatten(model.series), opts.empty);
    if (!canvas) return null;
    return render('bar', canvas, opts, model);
  }

  function stackedBar(canvasId, opts) {
    opts = opts || {};
    var model = prepXY(opts);
    var canvas = guard(canvasId, flatten(model.series), opts.empty);
    if (!canvas) return null;
    return render('stackedBar', canvas, opts, model);
  }

  function hbarRanked(canvasId, opts) {
    opts = opts || {};
    var rows = [];
    var src = opts.data && !Array.isArray(opts.data) ? opts.data : null;
    if (src) Object.keys(src).forEach(function (k) { rows.push({ l: k, v: Number(src[k]) || 0 }); });
    else (opts.labels || []).forEach(function (l, i) { rows.push({ l: l, v: Number((opts.values || opts.data || [])[i]) || 0 }); });
    rows.sort(function (a, b) { return b.v - a.v; });
    var limit = opts.limit == null ? 10 : opts.limit;
    var otherIdx = null;
    if (rows.length > limit) {
      var tail = rows.slice(limit - (opts.other === false ? 0 : 1));
      rows = rows.slice(0, limit - (opts.other === false ? 0 : 1));
      if (opts.other !== false) {
        rows.push({ l: typeof opts.other === 'string' ? opts.other : 'Andre (' + tail.length + ')',
                    v: tail.reduce(function (a, r) { return a + r.v; }, 0) });
        otherIdx = rows.length - 1;
      }
    }
    var model = {
      labels: rows.map(function (r) { return r.l; }),
      series: [{ label: opts.label || '', data: rows.map(function (r) { return r.v; }), color: opts.color }]
    };
    model.highlight = highlightMap(opts.highlight, model.labels);
    model.mutedIndex = otherIdx;
    var canvas = guard(canvasId, model.series[0].data, opts.empty);
    if (!canvas) return null;
    return render('hbarRanked', canvas, opts, model);
  }

  function doughnut(canvasId, opts) {
    opts = opts || {};
    var labels = (opts.labels || []).slice();
    var values = (opts.values || []).map(function (v) { return Number(v) || 0; });
    var maxSlices = opts.maxSlices == null ? 6 : opts.maxSlices;
    var otherIdx = null;
    if (values.length > maxSlices) {
      // Too many slices to tell apart: keep the first maxSlices-1, fold the rest into "Andre".
      var keep = maxSlices - 1;
      var rest = values.slice(keep).reduce(function (a, b) { return a + b; }, 0);
      labels = labels.slice(0, keep).concat(['Andre']);
      values = values.slice(0, keep).concat([rest]);
      otherIdx = keep;
    }
    var canvas = guard(canvasId, values, opts.empty);
    if (!canvas) return null;
    return render('doughnut', canvas, opts, { labels: labels, values: values, series: [], otherIndex: otherIdx });
  }

  function radar(canvasId, opts) {
    opts = opts || {};
    var model = { labels: (opts.labels || []).slice(), series: normSeries(opts.series) };
    var canvas = guard(canvasId, flatten(model.series), opts.empty);
    if (!canvas) return null;
    return render('radar', canvas, opts, model);
  }

  function sparkline(canvasId, opts) {
    opts = opts || {};
    var data = (opts.data || []).map(function (v) { return Number(v) || 0; });
    var labels = opts.labels ? opts.labels.slice() : data.map(function () { return ''; });
    var canvas = guard(canvasId, data, opts.empty);
    if (!canvas) return null;
    return render('sparkline', canvas, opts, { labels: labels, series: [{ label: opts.label || '', data: data }] });
  }

  // ---------------------------------------------------------------- live re-theme
  function themeKey() {
    return [cssVar('--fm-surface', ''), cssVar('--fm-ink', ''), cssVar('--fm-ink-3', ''), cssVar('--fm-line', ''),
            cssVar('--fm-tooltip-bg', ''), brandOverridden() ? cssVar('--fm-primary', '') + cssVar('--fm-clay', '') : '']
      .concat(FALLBACK.map(function (_, i) { return cssVar('--fm-chart-' + (i + 1), ''); })).join('|');
  }
  var _lastKey = null;
  function refreshTheme(force) {
    if (typeof Chart === 'undefined') return;
    var k = themeKey();
    if (!force && k === _lastKey) return;
    _lastKey = k;
    _rgbaCache = {};
    applyTheme();
    Object.keys(_registry).forEach(function (key) {
      var r = _registry[key];
      var canvas = r.chart && r.chart.canvas;
      if (!canvas || !document.documentElement.contains(canvas)) { forget(key); return; }
      try {
        var cfg = BUILD[r.kind](r.opts, r.model);
        r.chart.data = cfg.data;
        r.chart.options = cfg.options;
        r.chart.$fm = cfg.fm;
        r.chart.update('none');
      } catch (e) { /* keep the old rendering rather than breaking the page */ }
    });
  }
  var _pending = null;
  function scheduleRefresh() {
    if (_pending) return;
    // Let the attribute change cascade into computed styles before re-reading tokens.
    _pending = setTimeout(function () { _pending = null; refreshTheme(false); }, 30);
  }
  function watchTheme() {
    try {
      if (window.MutationObserver) {
        new MutationObserver(scheduleRefresh).observe(document.documentElement,
          { attributes: true, attributeFilter: ['data-theme', 'class', 'style'] });
        new MutationObserver(function (muts) {
          for (var i = 0; i < muts.length; i++) {
            var t = muts[i].target;
            if (t && (t.id === 'fm-brand-vars' || t.id === 'fm-wl-vars' || muts[i].addedNodes.length || muts[i].removedNodes.length)) {
              scheduleRefresh(); return;
            }
          }
        }).observe(document.head, { childList: true, subtree: true, characterData: true });
      }
      if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: dark)');
        if (mq.addEventListener) mq.addEventListener('change', scheduleRefresh);
        else if (mq.addListener) mq.addListener(scheduleRefresh);
      }
    } catch (e) {}
  }

  function get(canvasId) {
    var c = el(canvasId);
    var key = c ? c.id : canvasId;
    return _registry[key] ? _registry[key].chart : null;
  }
  function destroy(canvasId) {
    var c = el(canvasId);
    forget(c ? c.id : canvasId);
  }

  window.FMChart = {
    version: 2,
    palette: palette, applyTheme: applyTheme, cssVar: cssVar,
    line: line, bar: bar, doughnut: doughnut,
    radar: radar, sparkline: sparkline, stackedBar: stackedBar, hbarRanked: hbarRanked,
    format: format, fmtDate: fmtDate, fillDays: fillDays, ramp: ramp, muted: muted, alpha: alpha,
    get: get, destroy: destroy, refreshTheme: function () { refreshTheme(true); }
  };

  function init() { applyTheme(); _lastKey = themeKey(); watchTheme(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
