"""Guards for the shared chart layer (templates/fm/_charts.html + fm-charts.js).

Every chart on the site goes through ``window.FMChart`` so it shares the token-bound
theme, Danish number formatting, live dark-mode re-theming, the accessible summary and
the honest empty state. These tests keep it that way:

  1. no template builds a Chart.js chart directly (``new Chart(``);
  2. Chart.js is loaded from exactly one place, pinned to one version;
  3. every page that calls ``FMChart.*`` includes ``fm/_charts.html`` before the first call;
  4. the FMChart public API stays backward compatible (other pages call it);
  5. the chart palette tokens exist in light and dark mode and match the JS fallback.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CHARTS_JS = os.path.join(ROOT, "static", "futurematch", "assets", "fm-charts.js")
CHARTS_CSS = os.path.join(ROOT, "static", "futurematch", "assets", "fm-charts.css")
LOADER = os.path.join(TEMPLATES, "fm", "_charts.html")

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _templates():
    for dirpath, _dirs, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith(".html"):
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as fh:
                    yield os.path.relpath(path, TEMPLATES).replace(os.sep, "/"), fh.read()


def _code(src):
    """Template source without Jinja comments (docs may mention the patterns)."""
    return _JINJA_COMMENT.sub("", src)


def test_no_template_constructs_chartjs_directly():
    offenders = [rel for rel, src in _templates() if re.search(r"new\s+Chart\s*\(", _code(src))]
    assert not offenders, (
        "Use FMChart.* (templates/fm/_charts.html) instead of raw `new Chart(` in: "
        + ", ".join(sorted(offenders))
    )


def test_chartjs_loaded_only_by_the_shared_loader_and_pinned():
    offenders = []
    for rel, src in _templates():
        if rel == "fm/_charts.html":
            continue
        if re.search(r"npm/chart\.js|chart\.umd(\.min)?\.js", _code(src)):
            offenders.append(rel)
    assert not offenders, "Chart.js must only be loaded via fm/_charts.html: " + ", ".join(offenders)
    with open(LOADER, encoding="utf-8") as fh:
        loader = _code(fh.read())
    versions = re.findall(r"chart\.js@([0-9.]+)/", loader)
    assert versions == ["4.4.0"], versions
    # Order matters: tokens + Chart.js must exist before fm-charts.js reads them.
    i_css = loader.index("fm-charts.css")
    i_chartjs = loader.index("chart.js@")
    i_fm = loader.index("fm-charts.js")
    assert i_css < i_chartjs < i_fm


def test_pages_calling_fmchart_include_the_loader_first():
    missing, late = [], []
    for rel, src in _templates():
        if rel == "fm/_charts.html":
            continue
        code = _code(src)
        m = re.search(r"FMChart\.\w+\s*\(", code)
        if not m:
            continue
        inc = code.find("fm/_charts.html")
        if inc == -1:
            missing.append(rel)
        elif inc > m.start():
            late.append(rel)
    assert not missing, "Pages call FMChart without including fm/_charts.html: " + ", ".join(missing)
    assert not late, "fm/_charts.html must be included before the first FMChart call: " + ", ".join(late)


def test_fmchart_public_api_is_backward_compatible():
    with open(CHARTS_JS, encoding="utf-8") as fh:
        js = fh.read()
    export = js[js.index("window.FMChart = {"):]
    export = export[: export.index("};")]
    for name in ("palette", "applyTheme", "cssVar", "line", "bar", "doughnut", "radar",
                 "sparkline", "stackedBar"):
        assert re.search(r"\b%s\s*:" % name, export), "FMChart.%s must stay exported" % name
    for name in ("hbarRanked", "format", "fmtDate", "fillDays", "ramp", "muted", "get",
                 "destroy", "refreshTheme"):
        assert re.search(r"\b%s\s*:" % name, export), "FMChart.%s is documented API" % name
    # Every helper documented in the header comment exists.
    header = js[: js.index("*/")]
    for name in re.findall(r"FMChart\.(\w+)\(", header):
        assert re.search(r"\b%s\s*:" % name, export), "header documents FMChart.%s" % name


def test_fmchart_keeps_empty_state_and_accessibility():
    with open(CHARTS_JS, encoding="utf-8") as fh:
        js = fh.read()
    assert "fm-chart-empty" in js and "Ingen data endnu" in js
    assert "'role', 'img'" in js and "aria-label" in js
    assert "da-DK" in js
    # Live re-theme on the dark-mode toggle and OS colour-scheme change.
    assert "data-theme" in js and "prefers-color-scheme" in js


def _token_block(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return dict(re.findall(r"(--fm-chart-[\w-]+)\s*:\s*([^;]+);", m.group(1)))


def test_chart_palette_tokens_light_and_dark():
    with open(CHARTS_CSS, encoding="utf-8") as fh:
        css = fh.read()
    light = _token_block(css, ":root")
    dark = _token_block(css, '[data-theme="dark"]')
    slots = ["--fm-chart-%d" % i for i in range(1, 7)]
    for tok in slots + ["--fm-chart-muted", "--fm-chart-grid"]:
        assert tok in light and tok in dark, tok
    with open(CHARTS_JS, encoding="utf-8") as fh:
        js = fh.read()
    fallback = re.search(r"var FALLBACK = \[([^\]]+)\]", js).group(1)
    assert [c.strip().strip("'\"") for c in fallback.split(",")] == [light[s].strip() for s in slots]
