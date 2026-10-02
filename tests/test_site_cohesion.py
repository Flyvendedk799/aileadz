"""Site cohesion: the sidebar, the HR / admin / vendor sub-navs and the pages agree.

Pins four things:

  1. Every ``url_for`` in the shared navigation resolves, and the nav-state maps in
     ``futurematch_ui`` only name real endpoints and real tab keys.
  2. Every HR navigation link is gated by the capability its route's guard checks,
     both in the template source and in the route's actual behaviour (a role that
     holds the capability gets in, one that lacks it is turned away), so nobody is
     shown a link that bounces.
  3. The current location is highlighted exactly once in the sidebar and in the
     sub-nav, including on pages that set a generic ``page_id`` or none at all.
  4. No full page is an orphan: every argument-less GET route that renders an
     ``fm/`` template is linked from some template, unless it is on the short,
     reasoned allow-list below.

Runs on the real app (tests/secapp.py), no MySQL, no network.
"""

import inspect
import os
import re
import unittest
from unittest import mock

from tests.secapp import ROLES, get_app, login, patch_mysql

import futurematch_ui as fm

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
NAV_FILES = ("fm_base.html", "fm/_hr_subnav.html", "fm/_admin_subnav.html", "fm/_vendor_nav.html")


def _src(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def _no_branding():
    return mock.patch("white_label_global_integration.get_template_context", return_value={})


# The capability each HR navigation destination's ROUTE GUARD checks
# (hr_dashboard.require_hr_access -> company.workspace; require_hr_manager_access
# (cap='hr.analytics') -> company.analytics; companies.require_company_admin ->
# company.employees; settings hub "virksomhed" tab -> company.settings ...).
ROUTE_CAPABILITY = {
    "hr_dashboard.dashboard": "company.workspace",
    "hr_dashboard.team_cockpit": "company.team",
    "companies.employees": "company.employees",
    "hr_dashboard.employee_progress": "company.analytics",
    "hr_dashboard.departments": "company.workspace",
    "hr_dashboard.pending_approvals": "company.approvals",
    "hr_dashboard.approval_policies": "company.policies",
    "hr_dashboard.department_budgets": "company.workspace",
    "hr_dashboard.billing_overview": "company.billing",
    "hr_dashboard.learning_analytics": "company.analytics",
    "hr_dashboard.roi_dashboard": "company.analytics",
    "hr_dashboard.funnel_dashboard": "company.analytics",
    "hr_dashboard.retention_dashboard": "company.analytics",
    "hr_dashboard.benchmarking_view": "company.analytics",
    "hr_dashboard.skill_gaps_view": "company.analytics",
    "hr_ext.engagement": "company.analytics",
    "hr_ext.ai_quality": "company.analytics",
    "hr_ext.training_plan": "company.workspace",
    "hr_dashboard.learning_paths": "company.workspace",
    "hr_dashboard.internal_courses": "company.workspace",
    "hr_dashboard.compliance_matrix": "company.analytics",
    "hr_ext.procurement": "company.workspace",
    "hr_dashboard.supplier_management": "company.workspace",
    "hr_dashboard.reports": "company.analytics",
    "hr_dashboard.hr_chatbot": "company.assistant",
    "settings_hub.index": "company.settings",
}
_ALIASES = {"_ws": "company.workspace", "_an": "company.analytics"}
_DENIAL = re.compile(r"ikke adgang|ikke tilladelse|ikke de nødvendige rettigheder|Kun HR|Log ind", re.I)


def _gated_links(src):
    """{endpoint: guard expression} for every `{% if GUARD %}<a ... url_for('EP')` line."""
    out = {}
    for line in src.splitlines():
        m = re.search(r"\{% if (.+?) %\}\s*<(?:li><)?a [^>]*href=\"\{\{ url_for\('([^']+)'", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def _cap_in(guard, cap):
    caps = set(re.findall(r"can\('([^']+)'\)", guard))
    caps |= {_ALIASES[a] for a in _ALIASES if re.search(r"\b%s\b" % a, guard)}
    return cap in caps


class NavResolvesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def test_every_nav_url_for_resolves(self):
        for name in NAV_FILES:
            for ep in set(re.findall(r"url_for\('([^']+)'", _src(name))):
                self.assertIn(ep, self.app.view_functions, f"{name} links to unknown endpoint {ep}")

    def test_no_hardcoded_internal_hrefs_in_nav(self):
        for name in NAV_FILES:
            self.assertEqual(re.findall(r'href="/[a-z]', _src(name)), [], name)

    def test_nav_state_maps_name_real_endpoints(self):
        for table in (fm.HR_TAB_BY_ENDPOINT, fm.ADMIN_TAB_BY_ENDPOINT, fm.SIDEBAR_BY_ENDPOINT):
            for ep in table:
                self.assertIn(ep, self.app.view_functions, ep)

    def test_nav_state_maps_name_real_tabs_and_sections(self):
        hr_src, admin_src, base = _src("fm/_hr_subnav.html"), _src("fm/_admin_subnav.html"), _src("fm_base.html")
        for tab in set(fm.HR_TAB_BY_ENDPOINT.values()) | set(fm.HR_TAB_SECTION):
            self.assertIn(f"_hp == '{tab}'", hr_src, tab)
            self.assertIn(tab, fm.HR_TAB_SECTION, tab)
        for section in set(fm.HR_TAB_SECTION.values()):
            self.assertIn(f"_nav == 'hr.{section}'", base, section)
        for tab in set(fm.ADMIN_TAB_BY_ENDPOINT.values()):
            self.assertIn(f"_ap == '{tab}'", admin_src, tab)
            self.assertIn(f"_nav == 'admin.{tab}'", base, tab)
        for key in set(fm.SIDEBAR_BY_ENDPOINT.values()) | set(fm.PAGE_ID_ALIASES.values()):
            self.assertIn(f"_nav == '{key}'", base, key)

    def test_sidebar_and_admin_subnav_share_labels(self):
        base, admin_src = _src("fm_base.html"), _src("fm/_admin_subnav.html")
        admin_block = base[base.index("can('platform.admin')"):]
        side = dict(re.findall(r"url_for\('([^']+)'\) }}\"><i class=\"fa-solid [^\"]+\"></i><span>([^<]+)</span>", admin_block))
        tabs = dict(re.findall(r"url_for\('([^']+)'\) }}\"><i class=\"fa-solid [^\"]+\"></i> ([^<]+)</a>", admin_src))
        self.assertEqual(side, tabs)

    def test_sidebar_hr_labels_match_their_subnav_tab(self):
        base, hr_src = _src("fm_base.html"), _src("fm/_hr_subnav.html")
        block = base[base.index('fm-nav-label">Virksomhed'):base.index('fm-nav-label">Konto')]
        side = {ep: (ic, lb) for ep, ic, lb in re.findall(
            r"url_for\('([^']+)'\) }}\"><i class=\"(fa-solid [^\"]+)\"></i><span>([^<]+)</span>", block)}
        self.assertGreaterEqual(len(side), 9)
        tabs = {ep: (ic, lb) for ep, ic, lb in re.findall(
            r"url_for\('([^']+)'\) }}\"><i class=\"(fa-solid [^\"]+)\"></i> ([^<]+)</a>", hr_src)}
        for ep, (icon, label) in side.items():
            if ep in tabs:   # HR-assistent and Virksomhedsindstillinger live outside the sub-nav
                self.assertEqual((icon, label), tabs[ep], ep)


class CapabilityGatingTests(unittest.TestCase):
    """Template side: each link is wrapped in the route's capability."""

    def test_hr_subnav_links_carry_the_route_capability(self):
        links = _gated_links(_src("fm/_hr_subnav.html"))
        for ep, cap in ROUTE_CAPABILITY.items():
            if ep in ("hr_dashboard.hr_chatbot", "settings_hub.index"):
                continue
            self.assertIn(ep, links, f"{ep} is not in the HR sub-nav or not gated")
            self.assertTrue(_cap_in(links[ep], cap), f"{ep}: guard {links[ep]!r} != {cap}")

    def test_sidebar_company_links_carry_the_route_capability(self):
        base = _src("fm_base.html")
        block = base[base.index('fm-nav-label">Virksomhed'):base.index('fm-nav-label">Konto')]
        links = _gated_links(block)
        self.assertTrue(links)
        for ep, guard in links.items():
            self.assertIn(ep, ROUTE_CAPABILITY, ep)
            self.assertTrue(_cap_in(guard, ROUTE_CAPABILITY[ep]), f"{ep}: {guard!r}")

    def test_section_capability_matches_the_sidebar_guard(self):
        base = _src("fm_base.html")
        block = base[base.index('fm-nav-label">Virksomhed'):base.index('fm-nav-label">Konto')]
        matched = 0
        for line in block.splitlines():
            m = re.search(r"\{% if can\('([^']+)'\) %\}<li><a class=\"fm-nav-link \{\{ 'active' if _nav == 'hr\.(\w+)'", line)
            if m and m.group(2) in fm.HR_SECTION_CAPABILITY:
                matched += 1
                self.assertEqual(fm.HR_SECTION_CAPABILITY[m.group(2)], m.group(1), m.group(2))
        self.assertGreaterEqual(matched, 7)

    def test_sidebar_never_compares_role_strings(self):
        for name in NAV_FILES:
            src = _src(name)
            self.assertNotIn("session.get('role') ==", src, name)
            self.assertNotIn("company_role') ==", src, name)


class RouteGuardMatchesNavTests(unittest.TestCase):
    """Behaviour side: the route lets in exactly the roles the link is shown to."""

    PROBE_ROLES = ("employee", "dept_head", "hr_manager")

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def _denied(self, endpoint, role):
        with self.app.test_request_context():
            from flask import url_for
            url = url_for(endpoint)
        client = self.app.test_client()
        login(client, **ROLES[role])
        fake, patcher = patch_mysql(self.app)
        with patcher, _no_branding():
            resp = client.get(url)
        with client.session_transaction() as s:
            flashes = " ".join(m for _c, m in s.get("_flashes", []))
        return resp.status_code in (401, 403) or (resp.status_code in (301, 302) and bool(_DENIAL.search(flashes)))

    def test_link_visibility_matches_route_guard(self):
        import capabilities
        for ep, cap in ROUTE_CAPABILITY.items():
            for role in self.PROBE_ROLES:
                holds = capabilities.can(cap, dict(ROLES[role]))
                self.assertEqual(self._denied(ep, role), not holds,
                                 f"{ep} as {role}: link shown={holds} but route denied={self._denied(ep, role)}")


class ActiveStateTests(unittest.TestCase):
    """Exactly one sidebar entry and one sub-nav tab light up, and they agree."""

    @classmethod
    def setUpClass(cls):
        cls.app = get_app()

    def _render(self, path, page_id="", body="", role="hr_manager"):
        from flask import render_template_string, session
        tpl = ("{% extends 'fm_base.html' %}{% block page_id %}" + page_id + "{% endblock %}"
               "{% block content %}" + body + "{% endblock %}")
        with self.app.test_request_context(path), _no_branding():
            session.update(ROLES[role])
            self.app.preprocess_request()
            return render_template_string(tpl)

    @staticmethod
    def _active(html, cls):
        return re.findall(r'class="%s active"[^>]*href="([^"]+)"' % cls, html)

    def _check(self, path, side_href, tab_href=None, page_id="hr", subnav="hr", role="hr_manager"):
        body = {"hr": "{% include 'fm/_hr_subnav.html' %}",
                "admin": "{% include 'fm/_admin_subnav.html' %}", "": ""}[subnav]
        html = self._render(path, page_id, body, role)
        self.assertEqual(self._active(html, "fm-nav-link"), [side_href], path)
        if tab_href is not None:
            self.assertEqual(self._active(html, "pg-tab"), [tab_href], path)

    def test_hr_pages_light_their_section_and_tab(self):
        cases = [
            ("/hr/", "/hr/", "/hr/"),
            ("/hr/roi", "/hr/learning-analytics", "/hr/roi"),
            ("/hr/benchmarking", "/hr/learning-analytics", "/hr/benchmarking"),
            ("/hr/engagement", "/hr/learning-analytics", "/hr/engagement"),
            ("/hr/approvals", "/hr/approvals", "/hr/approvals"),
            ("/hr/approval-policies", "/hr/approvals", "/hr/approval-policies"),
            ("/hr/billing", "/hr/approvals", "/hr/billing"),
            ("/hr/order/abc/details", "/hr/approvals", "/hr/approvals"),
            ("/companies/employees", "/companies/employees", "/companies/employees"),
            ("/hr/employee/5/details", "/companies/employees", "/companies/employees"),
            ("/hr/departments", "/companies/employees", "/hr/departments"),
            ("/hr/courses/add", "/hr/training-plan", "/hr/courses"),
            ("/hr/suppliers", "/hr/procurement", "/hr/suppliers"),
            ("/hr/compliance", "/hr/compliance", "/hr/compliance"),
            ("/hr/reports", "/hr/reports", "/hr/reports"),
        ]
        for path, side, tab in cases:
            self._check(path, side, tab)

    def test_settings_and_assistant_pages_light_the_sidebar_only(self):
        self._check("/virksomhed/indstillinger/branding", "/virksomhed/indstillinger/", page_id="company", subnav="")
        self._check("/hr/chatbot", "/hr/chatbot", page_id="hr", subnav="")

    def test_learner_pages(self):
        self._check("/min-ordre/abc123", "/min-tidslinje", page_id="timeline", subnav="", role="employee")
        self._check("/analytics", "/analytics", page_id="analytics", subnav="", role="employee")
        self._check("/mine-maal", "/mine-maal", page_id="goals", subnav="", role="employee")

    def test_admin_pages_light_sidebar_and_admin_subnav(self):
        self._check("/admin/billing", "/admin/billing", "/admin/billing", page_id="hr", subnav="admin", role="admin")
        self._check("/admin/users", "/admin/users", "/admin/users", page_id="admin", subnav="admin", role="admin")
        self._check("/companies/admin/3", "/companies/admin", "/companies/admin", page_id="", subnav="admin",
                    role="admin")

    def test_page_id_fallback_outside_a_known_route(self):
        # The /ui design gallery renders pages outside their route; the page id decides.
        with self.app.test_request_context("/nowhere"):
            self.assertEqual(fm.nav_state("benchmark")["side"], "hr.analytics")
            self.assertEqual(fm.nav_state("goals")["side"], "goals")
            self.assertEqual(fm.nav_state("hr", hr_tab="roi")["hr"], "roi")

    def test_hidden_section_falls_back_to_oversigt(self):
        # A department head may open Afdelinger, but the "Medarbejdere" sidebar entry
        # is HR-manager only; the sidebar must still show where they are.
        self._check("/hr/departments", "/hr/", "/hr/departments", role="dept_head")

    def test_department_head_sees_only_what_they_can_open(self):
        html = self._render("/hr/approvals", "hr", "{% include 'fm/_hr_subnav.html' %}", role="dept_head")
        for allowed in ("/hr/approvals", "/hr/team", "/hr/departments", "/hr/budgets", "/hr/procurement"):
            self.assertIn(f'href="{allowed}"', html, allowed)
        for hidden in ("/hr/roi", "/hr/learning-analytics", "/hr/reports", "/hr/billing",
                       "/hr/approval-policies", "/companies/employees", "/virksomhed/indstillinger/"):
            self.assertNotIn(f'href="{hidden}"', html, hidden)


# Full pages intentionally reachable without a template link.
ORPHAN_ALLOWLIST = {
    "pages.contact": "legacy URL; renders the support page, which the footer links",
    "auth.login_2fa": "step two of the login flow (redirect target)",
    "enterprise_settings.webhooks_page": "rendered inside the settings hub's Webhooks tab",
    "hr_dashboard.chatbot_settings": "rendered inside the settings hub's Chatbot tab",
    "hr_dashboard.widget_creator": "rendered inside the settings hub's Chatbot > Widget tab",
}


class OrphanTests(unittest.TestCase):
    def test_every_full_page_is_linked(self):
        app = get_app()
        corpus = []
        for root, _dirs, files in os.walk(TEMPLATES):
            for f in files:
                if f.endswith(".html"):
                    with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                        corpus.append(fh.read())
        corpus = "\n".join(corpus)
        orphans = []
        for rule in app.url_map.iter_rules():
            if "GET" not in rule.methods or rule.arguments or rule.endpoint in ORPHAN_ALLOWLIST:
                continue
            view = app.view_functions.get(rule.endpoint)
            while hasattr(view, "__wrapped__"):
                view = view.__wrapped__
            try:
                src = inspect.getsource(view)
            except (OSError, TypeError):
                continue
            if not re.search(r"render_template\(\s*['\"]fm/", src):
                continue
            linked = (f"url_for('{rule.endpoint}'" in corpus or f'url_for("{rule.endpoint}"' in corpus
                      or f'"{rule.rule}"' in corpus or f"'{rule.rule}'" in corpus)
            if not linked:
                orphans.append(f"{rule.endpoint} ({rule.rule})")
        self.assertEqual(sorted(set(orphans)), [], "full pages no template links to")


if __name__ == "__main__":
    unittest.main()
