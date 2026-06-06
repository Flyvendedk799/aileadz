/* fm-charts.js — shared, token-bound Chart.js theme + helpers (FM Data-Viz layer).
 *
 * Load AFTER chart.umd.min.js. Every dashboard chart should go through this so the
 * whole surface looks identical, inverts correctly in dark mode, re-skins under
 * white-label, and degrades to a clean empty state instead of a blank canvas.
 *
 *   FMChart.line(canvasId, {labels, series, currency})
 *   FMChart.bar(canvasId,  {labels, series, horizontal, stacked})
 *   FMChart.doughnut(canvasId, {labels, values})
 *
 * A "series" is {label, data:[...], color?}. Colors default to the brand palette.
 * If a chart has no positive data it is replaced with an empty-state node.
 */
(function () {
  "use strict";

  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  // Brand chart palette — pulled from the same --fm-* tokens the rest of the UI uses.
  function palette() {
    return [
      cssVar('--fm-primary', '#0f766e'),
      cssVar('--fm-clay', '#c9603a'),
      cssVar('--fm-indigo', '#4f46e5'),
      cssVar('--fm-gold', '#d9a441'),
      cssVar('--fm-plum', '#8b5cf6'),
      cssVar('--fm-success', '#16a34a')
    ];
  }

  function softTint(color) {
    // Chart-fill tint; color-mix is supported on the same browsers as the rest of the UI.
    return 'color-mix(in srgb, ' + color + ' 16%, transparent)';
  }

  var _themed = false;
  function applyTheme() {
    if (typeof Chart === 'undefined') return;
    Chart.defaults.color = cssVar('--fm-ink-3', '#64748b');
    Chart.defaults.borderColor = cssVar('--fm-line', 'rgba(148,163,184,.25)');
    try { Chart.defaults.font.family = cssVar('--ff-body', "'Hanken Grotesk', system-ui, sans-serif"); } catch (e) {}
    try {
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        Chart.defaults.animation = false;
      }
    } catch (e) {}
    _themed = true;
  }

  function tooltip() {
    return { backgroundColor: 'rgba(15,23,42,.92)', padding: 12, cornerRadius: 8, displayColors: true,
             titleFont: { weight: '600' }, bodyFont: { size: 12 } };
  }

  function el(id) { return typeof id === 'string' ? document.getElementById(id) : id; }

  function flatten(series) {
    var out = [];
    (series || []).forEach(function (s) { (s.data || []).forEach(function (v) { out.push(Number(v) || 0); }); });
    return out;
  }
  function hasData(values) { return values.some(function (v) { return Number(v) > 0; }); }

  function emptyOut(canvas, msg) {
    if (!canvas) return;
    var box = document.createElement('div');
    box.className = 'fm-chart-empty';
    box.innerHTML = '<i class="fa-solid fa-chart-line"></i><span>' + (msg || 'Ingen data endnu') + '</span>';
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

  function line(canvasId, opts) {
    opts = opts || {};
    var series = opts.series || [];
    var cols = palette();
    var canvas = guard(canvasId, flatten(series), opts.empty);
    if (!canvas) return null;
    return new Chart(canvas, {
      type: 'line',
      data: {
        labels: opts.labels || [],
        datasets: series.map(function (s, i) {
          var c = s.color || cols[i % cols.length];
          return {
            label: s.label || '', data: s.data || [], borderColor: c,
            backgroundColor: softTint(c), borderWidth: 2, fill: opts.fill !== false,
            tension: 0.35, pointRadius: 2, pointHoverRadius: 4
          };
        })
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: series.length > 1, position: 'bottom' }, tooltip: tooltip() },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } }, x: { grid: { display: false } } }
      }
    });
  }

  function bar(canvasId, opts) {
    opts = opts || {};
    var series = opts.series || [];
    var cols = palette();
    var canvas = guard(canvasId, flatten(series), opts.empty);
    if (!canvas) return null;
    return new Chart(canvas, {
      type: 'bar',
      data: {
        labels: opts.labels || [],
        datasets: series.map(function (s, i) {
          var c = s.color || cols[i % cols.length];
          return { label: s.label || '', data: s.data || [], backgroundColor: c,
                   borderRadius: 6, maxBarThickness: 46 };
        })
      },
      options: {
        indexAxis: opts.horizontal ? 'y' : 'x',
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: series.length > 1, position: 'bottom' }, tooltip: tooltip() },
        scales: {
          x: { stacked: !!opts.stacked, grid: { display: !!opts.horizontal } },
          y: { stacked: !!opts.stacked, beginAtZero: true, ticks: { precision: 0 } }
        }
      }
    });
  }

  function doughnut(canvasId, opts) {
    opts = opts || {};
    var values = (opts.values || []).map(function (v) { return Number(v) || 0; });
    var cols = palette();
    var canvas = guard(canvasId, values, opts.empty);
    if (!canvas) return null;
    return new Chart(canvas, {
      type: 'doughnut',
      data: { labels: opts.labels || [],
              datasets: [{ data: values, backgroundColor: cols, borderWidth: 0 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: '64%',
        plugins: { legend: { position: 'bottom' }, tooltip: tooltip() }
      }
    });
  }

  window.FMChart = {
    palette: palette, applyTheme: applyTheme, cssVar: cssVar,
    line: line, bar: bar, doughnut: doughnut
  };

  document.addEventListener('DOMContentLoaded', applyTheme);
})();
