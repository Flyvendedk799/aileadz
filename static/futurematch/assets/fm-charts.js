/* fm-charts.js — shared, token-bound Chart.js theme + helpers (FM Data-Viz layer).
 *
 * Load AFTER chart.umd.min.js. Every dashboard chart should go through this so the
 * whole surface looks identical, inverts correctly in dark mode, re-skins under
 * white-label, and degrades to a clean empty state instead of a blank canvas.
 *
 *   FMChart.line(canvasId, {labels, series, currency})
 *   FMChart.bar(canvasId,  {labels, series, horizontal, stacked})
 *   FMChart.doughnut(canvasId, {labels, values})
 *   FMChart.radar(canvasId, {labels, series, max})
 *   FMChart.sparkline(canvasId, {data, color})
 *   FMChart.stackedBar(canvasId, {labels, series, horizontal})
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
    // Token-bound so the tooltip surface inverts in dark mode and re-skins under
    // white-label, instead of a hardcoded slate that disappears on dark backgrounds.
    var bg = cssVar('--fm-tooltip-bg', '#1a211d');
    var ink = cssVar('--fm-tooltip-ink', '#ffffff');
    return { backgroundColor: bg, titleColor: ink, bodyColor: ink,
             padding: 12, cornerRadius: 8, displayColors: true,
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
      options: (function () {
        var yScale = { beginAtZero: true, ticks: { precision: 0 } };
        // Optional fixed upper bound on the value axis (e.g. 0..5 for a skill
        // level trend) so a small movement isn't exaggerated by auto-scaling.
        if (opts.max != null) yScale.max = Number(opts.max);
        return {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: series.length > 1, position: 'bottom' }, tooltip: tooltip() },
          scales: { y: yScale, x: { grid: { display: false } } }
        };
      })()
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
      options: (function () {
        var valueAxis = opts.horizontal ? 'x' : 'y';
        var scales = {
          x: { stacked: !!opts.stacked, grid: { display: !!opts.horizontal } },
          y: { stacked: !!opts.stacked, beginAtZero: true, ticks: { precision: 0 } }
        };
        // Optional fixed upper bound on the value axis (e.g. 0..100 for a
        // percentile chart). Direction-agnostic: applies to whichever axis
        // carries the data values given the orientation.
        if (opts.max != null) {
          scales[valueAxis].max = Number(opts.max);
          scales[valueAxis].beginAtZero = true;
        }
        return {
          indexAxis: opts.horizontal ? 'y' : 'x',
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: series.length > 1, position: 'bottom' }, tooltip: tooltip() },
          scales: scales
        };
      })()
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

  // Danish-locale number format for tooltips/ticks (matches the rest of the UI).
  function nf(v) {
    try { return Number(v).toLocaleString('da-DK'); } catch (e) { return String(v); }
  }

  function radar(canvasId, opts) {
    opts = opts || {};
    var series = opts.series || [];
    var cols = palette();
    var canvas = guard(canvasId, flatten(series), opts.empty);
    if (!canvas) return null;
    return new Chart(canvas, {
      type: 'radar',
      data: {
        labels: opts.labels || [],
        datasets: series.map(function (s, i) {
          var c = s.color || cols[i % cols.length];
          return {
            label: s.label || '', data: s.data || [], borderColor: c,
            backgroundColor: softTint(c), borderWidth: 2,
            pointRadius: 3, pointHoverRadius: 5,
            pointBackgroundColor: c, pointBorderColor: c
          };
        })
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: series.length > 1, position: 'bottom' },
          tooltip: Object.assign(tooltip(), {
            callbacks: { label: function (ctx) { return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + nf(ctx.parsed.r); } }
          })
        },
        scales: {
          r: {
            beginAtZero: true,
            suggestedMax: opts.max || undefined,
            angleLines: { color: cssVar('--fm-line', 'rgba(148,163,184,.25)') },
            grid: { color: cssVar('--fm-line', 'rgba(148,163,184,.25)') },
            pointLabels: { color: cssVar('--fm-ink-2', '#475569'), font: { size: 11 } },
            ticks: { display: true, precision: 0, backdropColor: 'transparent', stepSize: opts.step || undefined }
          }
        }
      }
    });
  }

  function sparkline(canvasId, opts) {
    opts = opts || {};
    var data = (opts.data || []).map(function (v) { return Number(v) || 0; });
    var c = opts.color || palette()[0];
    var canvas = guard(canvasId, data, opts.empty);
    if (!canvas) return null;
    return new Chart(canvas, {
      type: 'line',
      data: {
        labels: data.map(function () { return ''; }),
        datasets: [{
          data: data, borderColor: c, backgroundColor: softTint(c),
          borderWidth: 2, fill: true, tension: 0.4,
          pointRadius: 0, pointHoverRadius: 3, pointHoverBackgroundColor: c
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: Object.assign(tooltip(), {
            callbacks: { title: function () { return ''; }, label: function (ctx) { return nf(ctx.parsed.y); } }
          })
        },
        scales: { x: { display: false }, y: { display: false } },
        elements: { line: { borderCapStyle: 'round' } }
      }
    });
  }

  function stackedBar(canvasId, opts) {
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
                   borderRadius: 5, borderWidth: 0, maxBarThickness: 46 };
        })
      },
      options: {
        indexAxis: opts.horizontal ? 'y' : 'x',
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { display: series.length > 1, position: 'bottom' },
          tooltip: Object.assign(tooltip(), {
            callbacks: { label: function (ctx) { return (ctx.dataset.label ? ctx.dataset.label + ': ' : '') + nf(ctx.parsed[opts.horizontal ? 'x' : 'y']); } }
          })
        },
        scales: {
          x: { stacked: true, grid: { display: !!opts.horizontal }, beginAtZero: !!opts.horizontal, ticks: { precision: 0 } },
          y: { stacked: true, beginAtZero: !opts.horizontal, grid: { display: !opts.horizontal }, ticks: { precision: 0 } }
        }
      }
    });
  }

  window.FMChart = {
    palette: palette, applyTheme: applyTheme, cssVar: cssVar,
    line: line, bar: bar, doughnut: doughnut,
    radar: radar, sparkline: sparkline, stackedBar: stackedBar
  };

  document.addEventListener('DOMContentLoaded', applyTheme);
})();
