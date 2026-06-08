"""Structural tests for the unified HR sub-navigation (plan #1).

These render the canonical _hr_subnav.html and the converted HR pages through
Jinja with a stubbed url_for, so they need no Flask app boot and no DB. They
lock in three invariants the nav-unification depends on:

  1. The canonical subnav exposes every HR destination (incl. the previously
     orphaned ones folded in: departments, employees, internal_courses,
     suppliers, learning_analytics).
  2. Each converted page includes the canonical subnav and no longer carries an
     inline `.pg-subnav` block (no split-brain).
  3. `active_hr_page` highlights exactly one tab, the right one.
"""
import re
import jinja2
import pytest

TEMPLATES = "templates"


def _env():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES))
    # Stub url_for so endpoints don't need an app/url map; return a marker that
    # encodes the endpoint name for assertions.
    env.globals["url_for"] = lambda ep, **kw: "/" + ep
    return env


# (template, active_hr_page key the view sets, endpoint that must be marked active)
CONVERTED_PAGES = [
    ("fm/approvals.html", "approvals", "hr_dashboard.pending_approvals"),
    ("fm/budgets.html", "budgets", "hr_dashboard.department_budgets"),
    ("fm/compliance.html", "compliance", "hr_dashboard.compliance_matrix"),
    ("fm/learning_paths.html", "learning_paths", "hr_dashboard.learning_paths"),
    ("fm/roi.html", "roi", "hr_dashboard.roi_dashboard"),
    ("fm/skill_gaps.html", "skill_gaps", "hr_dashboard.skill_gaps_view"),
    ("fm/team_cockpit.html", "team", "hr_dashboard.team_cockpit"),
    ("fm/departments.html", "departments", "hr_dashboard.departments"),
    ("fm/internal_courses.html", "internal_courses", "hr_dashboard.internal_courses"),
    ("fm/suppliers.html", "suppliers", "hr_dashboard.supplier_management"),
    ("fm/supplier_agreements.html", "suppliers", "hr_dashboard.supplier_management"),
    ("fm/employee_progress.html", "employees", "hr_dashboard.employee_progress"),
    ("fm/learning_analytics.html", "learning_analytics", "hr_dashboard.learning_analytics"),
]

# Destinations that MUST be reachable from the canonical nav. Includes the four
# the plan explicitly folds in plus the people/org page the inline navs carried.
REQUIRED_ENDPOINTS = {
    "hr_dashboard.dashboard",
    "hr_dashboard.team_cockpit",
    "hr_dashboard.employee_progress",
    "hr_dashboard.departments",
    "hr_dashboard.pending_approvals",
    "hr_dashboard.approval_policies",
    "hr_dashboard.department_budgets",
    "hr_dashboard.roi_dashboard",
    "hr_dashboard.learning_analytics",
    "hr_dashboard.benchmarking_view",
    "hr_dashboard.skill_gaps_view",
    "hr_ext.training_plan",
    "hr_dashboard.learning_paths",
    "hr_dashboard.internal_courses",
    "hr_dashboard.compliance_matrix",
    "hr_dashboard.supplier_management",
    "hr_ext.procurement",
    "hr_ext.engagement",
    "hr_ext.ai_quality",
    "hr_dashboard.reports",
}


def _render_subnav(active):
    src = open(f"{TEMPLATES}/fm/_hr_subnav.html").read()
    return _env().from_string(src).render(active_hr_page=active)


def test_canonical_subnav_covers_every_hr_destination():
    src = open(f"{TEMPLATES}/fm/_hr_subnav.html").read()
    endpoints = set(re.findall(r"url_for\('([^']+)'", src))
    missing = REQUIRED_ENDPOINTS - endpoints
    assert not missing, f"canonical subnav dropped destinations: {sorted(missing)}"


def test_subnav_highlights_exactly_one_tab_per_active_key():
    # Every key a converted page can set must light exactly one tab.
    for _, active, _ in CONVERTED_PAGES:
        html = _render_subnav(active)
        assert html.count("pg-tab active") == 1, (
            f"active_hr_page={active!r} highlighted "
            f"{html.count('pg-tab active')} tabs (expected 1)"
        )


def test_subnav_with_no_active_key_highlights_nothing():
    # Sub-pages like billing/my_department/chatbot_sessions pass '' and must not
    # false-highlight a tab.
    assert _render_subnav("").count("pg-tab active") == 0


@pytest.mark.parametrize("tmpl,active,endpoint", CONVERTED_PAGES)
def test_converted_page_includes_canonical_subnav(tmpl, active, endpoint):
    src = open(f"{TEMPLATES}/{tmpl}").read()
    assert "fm/_hr_subnav.html" in src, f"{tmpl} no longer includes canonical subnav"
    # The split-brain inline nav must be gone: no raw `pg-subnav` class left in
    # the page body (the include lives in the partial, not inline here).
    assert 'class="pg-subnav"' not in src, f"{tmpl} still has an inline pg-subnav block"


@pytest.mark.parametrize("tmpl,active,endpoint", CONVERTED_PAGES)
def test_converted_page_highlights_its_own_tab(tmpl, active, endpoint):
    # Render just the subnav with the page's active key and confirm the active
    # tab points at the page's own endpoint.
    html = _render_subnav(active)
    active_hrefs = re.findall(r'class="pg-tab active"[^>]*href="([^"]+)"', html)
    assert active_hrefs == ["/" + endpoint], (
        f"{tmpl} (active={active!r}) highlights {active_hrefs}, expected ['/{endpoint}']"
    )
