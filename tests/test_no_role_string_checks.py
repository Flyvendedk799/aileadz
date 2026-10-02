"""Permissions are asked of the role matrix, never decided by comparing role names.

``auth_decorators`` owns the matrix (``CAPABILITY_MIN_ROLE`` + aliases, per-member
permission overrides, department scoping); views and templates ask
``can('...')``. A role-string comparison elsewhere silently drifts from the
matrix (the link shows, the route bounces, or an override is ignored), so this
test fails on any new one.

Two scans, both pure source greps (no app, no MySQL):

* templates: any comparison against the session role / company role
  (``session.get('role') == ...``, ``session['company_role'] in [...]``,
  ``company.user_role in [...]``). Displaying a role is fine; comparing is not.
  Comparing a *data row's* role (``employee.role == 'company_admin'`` to badge
  it) is not a permission check and is not matched.
* Python: comparisons of a company role (``company_role`` / ``user_role``)
  against a role literal, outside the short allow-list below.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SESSION_ROLE = (r"(?:session\.get\(\s*['\"](?:role|company_role)['\"]\s*\)"
                 r"|session\[\s*['\"](?:role|company_role)['\"]\s*\]"
                 r"|session\.(?:role|company_role)\b"
                 r"|\b(?:company_role|user_role)\b)")
_TEMPLATE_CMP = re.compile(
    _SESSION_ROLE + r"\s*(?:==|!=|\bnot\s+in\b|\bin\b)"
    r"|(?:==|!=)\s*" + _SESSION_ROLE)

_ROLE_LITERAL = r"['\"](?:company_admin|hr_manager|department_head|team_lead|employee)['\"]"
_PY_CMP = re.compile(
    r"\b(?:company_role|user_role)['\"]?\)?\]?\s*(?:==|!=|\bnot\s+in\b|\bin\b)\s*[\(\[]?\s*" + _ROLE_LITERAL)

# Python files allowed to name company roles in a comparison, each with the reason.
PY_ALLOWED = {
    # The matrix itself, and require_company_role (kept for callers outside the app).
    "auth_decorators.py": "owns the role matrix",
    # Thin wrapper that normalises the acting role for the matrix.
    "capabilities.py": "normalises the acting role for the matrix",
    # Service-layer data scoping on an OrderContext built from the session: a
    # department head's order queries are filtered to their department. This is
    # row scoping inside the order service, not an access decision.
    "order_service.py": "department row-scoping inside the order service",
    # 2FA enforcement policy is keyed on holding the company_admin role (who must
    # use a second factor), not on what that role may do.
    "two_factor.py": "2FA enforcement policy for the company_admin role",
}


def _tracked(*patterns):
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if p]


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as fh:
        return fh.read()


class NoRoleStringChecksTests(unittest.TestCase):
    def test_templates_ask_capabilities_not_roles(self):
        hits = []
        templates = _tracked("*.html")
        self.assertTrue(templates)
        for path in templates:
            for no, line in enumerate(_read(path).splitlines(), 1):
                if _TEMPLATE_CMP.search(line):
                    hits.append(f"{path}:{no}: {line.strip()[:160]}")
        self.assertEqual(hits, [], "Role-string comparison in a template; use can('...') instead:\n"
                         + "\n".join(hits))

    def test_python_views_ask_capabilities_not_roles(self):
        hits = []
        for path in _tracked("*.py"):
            if path.startswith("tests/") or path in PY_ALLOWED:
                continue
            for no, line in enumerate(_read(path).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if _PY_CMP.search(line):
                    hits.append(f"{path}:{no}: {line.strip()[:160]}")
        self.assertEqual(hits, [], "Company-role comparison outside auth_decorators; use can('...'):\n"
                         + "\n".join(hits))

    def test_patterns_catch_the_shapes_they_exist_for(self):
        for bad in ("{% if session.get('company_role') == 'company_admin' %}",
                    "{% if session.get('role') == 'admin' %}",
                    "{% set x = session['company_role'] in ['hr_manager'] %}",
                    "{% if company.user_role in ['company_admin', 'hr_manager'] %}",
                    "{% if 'admin' == session.get('role') %}"):
            self.assertTrue(_TEMPLATE_CMP.search(bad), bad)
        for ok in ("{% if can('company.admins') or employee.role == 'company_admin' %}",
                   "{{ session.get('company_role') }}",
                   "{% if session.get('company_role') %}"):
            self.assertFalse(_TEMPLATE_CMP.search(ok), ok)
        for bad in ("if session.get('company_role') not in ('company_admin', 'hr_manager'):",
                    "if company['user_role'] not in ['company_admin', 'hr_manager']:",
                    "if role_x and company_role == 'department_head':"):
            self.assertTrue(_PY_CMP.search(bad), bad)
        self.assertFalse(_PY_CMP.search("if session.get('role') == 'admin':"))


if __name__ == "__main__":
    unittest.main()
