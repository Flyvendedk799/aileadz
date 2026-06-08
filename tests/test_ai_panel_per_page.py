"""Tests for the per-HR-page embedded AI assistant + token-bound chart tooltip
(plan #7).

The HR AI advisor (_ai_panel.html) must be reachable from EVERY HR page but must
NOT bleed onto the wrong audiences. The hard constraint (and the plan's risk
register) is explicit: the panel must NEVER be placed in the shared fm_base.html
(94 admin/vendor/public/employee templates extend it). The chosen mechanism is to
auto-include the panel from the canonical HR-only subnav partial.

Separately, the shared chart layer (fm-charts.js) hardcoded a slate tooltip
background that broke dark-mode / white-label theming; it must read a --fm-* token.

These render templates through Jinja with a stubbed url_for (no app boot, no DB)
and grep the static assets, locking in:

  1. The panel is auto-included via fm/_hr_subnav.html (so it lands on all 24 HR
     pages from one place) and is the ONLY include site.
  2. fm_base.html does NOT include the panel (hard audience-scoping constraint).
  3. The panel renders the FAB for HR roles, and is suppressed for non-HR roles
     and when there is no session at all.
  4. The panel carries the active_hr_page context onto the DOM for context-aware
     questions.
  5. fm-charts.js no longer hardcodes the tooltip colour and reads the tooltip
     tokens, which are defined in both the light and dark CSS roots.
"""
import re

import jinja2

TEMPLATES = "templates"
CHARTS_JS = "static/futurematch/assets/fm-charts.js"
FM_CSS = "static/futurematch/assets/fm.css"


def _env():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES))
    env.globals["url_for"] = lambda ep, **kw: "/" + ep
    return env


def _render_panel(session=None, active_hr_page="compliance"):
    src = open(f"{TEMPLATES}/fm/_ai_panel.html").read()
    ctx = {"active_hr_page": active_hr_page}
    if session is not None:
        ctx["session"] = session
    return _env().from_string(src).render(**ctx)


# ---- inclusion topology / audience scoping --------------------------------

def test_panel_is_autoincluded_from_canonical_subnav():
    subnav = open(f"{TEMPLATES}/fm/_hr_subnav.html").read()
    assert "fm/_ai_panel.html" in subnav, (
        "the HR AI panel must be auto-included from the canonical HR-only subnav "
        "so it reaches every HR page from a single site"
    )


def test_subnav_is_the_only_panel_include_site():
    # Exactly one template includes the panel: the HR-only subnav. Any other
    # include (e.g. a direct one left on hr.html, or one on an admin/vendor page)
    # is a duplicate-id / audience-leak risk.
    import glob

    sites = []
    for path in glob.glob(f"{TEMPLATES}/**/*.html", recursive=True):
        if path.endswith("_ai_panel.html"):
            continue
        if "fm/_ai_panel.html" in open(path).read():
            sites.append(path)
    assert sites == [f"{TEMPLATES}/fm/_hr_subnav.html"], (
        f"the AI panel must be included only via the HR subnav; found: {sites}"
    )


def test_panel_never_in_fm_base():
    # HARD CONSTRAINT: fm_base.html is extended by 94 admin/vendor/public/employee
    # templates. The HR advisor panel must never live there.
    base = open(f"{TEMPLATES}/fm_base.html").read()
    assert "_ai_panel" not in base, (
        "the HR AI panel must NOT be in fm_base.html (would leak onto admin/"
        "vendor/public/employee surfaces)"
    )
    assert "_hr_subnav" not in base, (
        "the HR subnav (which auto-includes the panel) must not be in fm_base.html"
    )


# ---- role gating ----------------------------------------------------------

def test_panel_renders_for_hr_roles():
    for role in ("company_admin", "hr_manager", "department_head"):
        html = _render_panel(session={"company_role": role})
        assert 'id="fmAip"' in html, f"panel should render for company_role={role}"
        assert "fmAipFab" in html


def test_panel_renders_for_platform_admin():
    html = _render_panel(session={"role": "admin"})
    assert 'id="fmAip"' in html


def test_panel_suppressed_for_non_hr_role():
    html = _render_panel(session={"company_role": "employee"})
    assert 'id="fmAip"' not in html, "panel must not render for a plain employee"


def test_panel_suppressed_without_session():
    # Isolated/edge renders (e.g. the subnav unit test) have no session — the
    # panel must degrade to nothing rather than crash.
    html = _render_panel(session=None)
    assert 'id="fmAip"' not in html


# ---- page-aware context ---------------------------------------------------

def test_panel_carries_active_page_context():
    html = _render_panel(session={"company_role": "hr_manager"}, active_hr_page="roi")
    assert 'data-aip-page="roi"' in html, (
        "panel must carry active_hr_page so the advisor is context-aware"
    )


def test_panel_sends_query_to_sse_endpoint():
    html = _render_panel(session={"company_role": "hr_manager"})
    assert "/hr_dashboard.hr_chatbot_ask" in html, (
        "panel must POST to the existing HR chatbot SSE endpoint"
    )


# ---- token-bound chart tooltip -------------------------------------------

def test_chart_tooltip_no_hardcoded_color():
    js = open(CHARTS_JS).read()
    assert "rgba(15,23,42" not in js, (
        "fm-charts.js must not hardcode the tooltip background (breaks dark-mode/"
        "white-label); read a --fm-* token instead"
    )


def test_chart_tooltip_reads_tokens():
    js = open(CHARTS_JS).read()
    assert "--fm-tooltip-bg" in js
    assert "--fm-tooltip-ink" in js
    # The tooltip must also set text colours so the inverted surface stays legible.
    assert "titleColor" in js and "bodyColor" in js


def test_tooltip_tokens_defined_light_and_dark():
    css = open(FM_CSS).read()
    # token defined in :root (light)
    assert re.search(r":root\b.*?--fm-tooltip-bg", css, re.S), "missing light tooltip token"
    # and overridden in the dark theme block
    dark = css[css.index('[data-theme="dark"]'):]
    assert "--fm-tooltip-bg" in dark and "--fm-tooltip-ink" in dark, (
        "tooltip tokens must be overridden for dark mode"
    )
