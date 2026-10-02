"""Templates and front-end JS stay wired to real routes.

A static scan of every template (``templates/``, ``app1/templates/``) and
``static/**/*.js`` against the real ``create_app()`` URL map:

* every ``url_for('bp.endpoint')`` names a registered endpoint (a typo renders
  as a 500, or, on the 404 page, as a bare-text fallback);
* a link (``href="{{ url_for(...) }}"``) never points at a POST-only route (405);
* a ``<form method=... action="{{ url_for(...) }}">`` uses a method the route accepts;
* every ``fetch()`` URL that can be resolved statically (string literal,
  concatenation, template literal, ``url_for`` or a ``const X = ...`` holding
  one) matches a route that accepts the request's method;
* every absolute ``href="/..."`` path matches a route;
* inline ``on*=""`` handlers only call functions defined on the page, in its
  extends/include chain, or in the shared static JS.

Plus behaviour tests for the half-done features this scan surfaced.
"""

import glob
import os
import re
import unittest
from unittest import mock

from werkzeug.security import generate_password_hash

from tests.secapp import client_as, get_app, patch_mysql

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _templates():
    out = []
    for pattern in ("templates/**/*.html", "app1/templates/**/*.html"):
        out += glob.glob(os.path.join(ROOT, pattern), recursive=True)
    return sorted(out)


def _static_js():
    return sorted(glob.glob(os.path.join(ROOT, "static", "**", "*.js"), recursive=True))


def _rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def _line(text, pos):
    return text.count("\n", 0, pos) + 1


_HOLE = "\x00"   # stands for a dynamic URL segment


class _Routes:
    def __init__(self, app):
        self.ep_methods = {}
        self.rules = []
        for r in app.url_map.iter_rules():
            methods = {m for m in r.methods if m not in ("HEAD", "OPTIONS")}
            self.ep_methods.setdefault(r.endpoint, set()).update(methods)
            rx = ""
            for part in re.split(r"(<[^>]+>)", r.rule):
                if part.startswith("<"):
                    rx += "(?:.+)" if part.startswith("<path:") else "(?:[^/]+)"
                else:
                    rx += re.escape(part)
            sample = re.sub(r"<[^>]+>", "v1", r.rule)
            self.rules.append((re.compile("^" + rx.rstrip("/") + "/?$"), sample, methods, r.rule))

    def methods_for_path(self, path):
        """Union of methods of every rule the (possibly holed) path can match, or None."""
        path = path.split("?")[0].split("#")[0]
        hits = set()
        found = False
        holed = re.compile("^" + re.escape(path).replace(_HOLE, "[^?]+") + "/?$")
        for rx, sample, methods, _rule in self.rules:
            if (_HOLE not in path and rx.match(path)) or holed.match(sample) or holed.match(sample.rstrip("/")):
                hits |= methods
                found = True
        return hits if found else None


_ROUTES = None


def _routes():
    global _ROUTES
    if _ROUTES is None:
        _ROUTES = _Routes(get_app())
    return _ROUTES


# ── fetch() URL resolution ────────────────────────────────────────────────────
def _first_arg(txt, start):
    depth, i, quote = 0, start, None
    while i < len(txt):
        c = txt[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return txt[start:i]
            depth -= 1
        elif c == "," and depth == 0:
            return txt[start:i]
        i += 1
    return txt[start:]


def _resolve(expr, txt, depth=0):
    """('ep', endpoint) | ('path', path-with-holes) | None (not statically resolvable)."""
    e = expr.strip()
    m = re.search(r"""url_for\(\s*['"]([\w.]+)['"]""", e)
    if m:
        return ("ep", m.group(1))
    if re.match(r"^[A-Za-z_$][\w$]*$", e) and depth < 3:
        d = re.search(r"(?:const|let|var)\s+" + re.escape(e) + r"\s*=\s*([^;\n]+)", txt)
        return _resolve(d.group(1), txt, depth + 1) if d else None
    s = re.sub(r"\$\{[^}]*\}", _HOLE, e)
    out = ""
    for quote, lit, other in re.findall(r"""(['"`])((?:\\.|(?!\1).)*)\1|([^+'"`]+)""", s):
        if quote:
            out += lit
        elif other.strip():
            out += _HOLE
    out = out.split("?")[0]
    return ("path", out) if out.startswith("/") else None


class TemplateRouteWiringTests(unittest.TestCase):
    def test_every_url_for_names_a_registered_endpoint(self):
        routes = _routes()
        bad = []
        files = _templates() + [p for p in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True)
                                if not _rel(p).startswith((".claude/", "tests/"))]
        for path in files:
            txt = _read(path)
            for m in re.finditer(r"""\burl_for\(\s*['"]([\w.]+)['"]""", txt):
                ep = m.group(1)
                if ep.startswith(".") or ep in routes.ep_methods:
                    continue
                bad.append("%s:%d %s" % (_rel(path), _line(txt, m.start()), ep))
        self.assertEqual(bad, [], "url_for() of an unknown endpoint")

    def test_links_never_point_at_post_only_routes(self):
        routes = _routes()
        bad = []
        for path in _templates():
            txt = _read(path)
            for m in re.finditer(r"""href\s*=\s*["']\{\{\s*url_for\(\s*['"]([\w.]+)['"]""", txt):
                methods = routes.ep_methods.get(m.group(1))
                if methods is not None and "GET" not in methods:
                    bad.append("%s:%d %s %s" % (_rel(path), _line(txt, m.start()), m.group(1), sorted(methods)))
        self.assertEqual(bad, [], "a plain link to a route that does not accept GET (use a POST form)")

    def test_forms_use_a_method_the_route_accepts(self):
        routes = _routes()
        bad = []
        for path in _templates():
            txt = _read(path)
            for m in re.finditer(r"<form\b([^>]*)>", txt, re.S):
                attrs = m.group(1)
                am = re.search(r"""action\s*=\s*["']\{\{\s*url_for\(\s*['"]([\w.]+)['"]""", attrs)
                if not am:
                    continue
                mm = re.search(r"""method\s*=\s*['"]?(\w+)""", attrs, re.I)
                method = (mm.group(1) if mm else "GET").upper()
                methods = routes.ep_methods.get(am.group(1))
                if methods is not None and method not in methods:
                    bad.append("%s:%d %s %s" % (_rel(path), _line(txt, m.start()), am.group(1), method))
        self.assertEqual(bad, [], "form method not accepted by its action route")

    def test_fetch_urls_map_to_routes(self):
        routes = _routes()
        bad = []
        checked = 0
        for path in _templates() + _static_js():
            txt = _read(path)
            for m in re.finditer(r"\bfetch\(", txt):
                arg = _first_arg(txt, m.end())
                rest = txt[m.end() + len(arg): m.end() + len(arg) + 600]
                mm = re.match(r"\s*,\s*\{[^}]*?method\s*:\s*['\"](\w+)['\"]", rest, re.S)
                method = mm.group(1).upper() if mm else "GET"
                res = _resolve(arg, txt)
                if res is None:
                    continue
                checked += 1
                kind, val = res
                where = "%s:%d" % (_rel(path), _line(txt, m.start()))
                if kind == "ep":
                    methods = routes.ep_methods.get(val)
                    if methods is None:
                        bad.append("%s unknown endpoint %s" % (where, val))
                    elif method not in methods:
                        bad.append("%s %s %s not in %s" % (where, val, method, sorted(methods)))
                else:
                    methods = routes.methods_for_path(val)
                    if methods is None:
                        bad.append("%s no route for %r" % (where, val.replace(_HOLE, "<x>")))
                    elif method not in methods:
                        bad.append("%s %r %s not in %s" % (where, val.replace(_HOLE, "<x>"), method, sorted(methods)))
        self.assertGreater(checked, 50, "the scanner stopped finding fetch() calls")
        self.assertEqual(bad, [], "fetch() to a URL/method no route serves")

    def test_absolute_hrefs_map_to_routes(self):
        routes = _routes()
        bad = []
        for path in _templates():
            txt = _read(path)
            for m in re.finditer(r"""href\s*=\s*(['"])(/[^'"#{?]*)(?:[?#][^'"]*)?\1""", txt):
                url = m.group(2)
                if url.startswith(("//", "/static/")) or url == "/":
                    continue
                if routes.methods_for_path(url) is None:
                    bad.append("%s:%d %s" % (_rel(path), _line(txt, m.start()), url))
        self.assertEqual(bad, [], "href to a path no route serves")

    def test_inline_handlers_call_defined_functions(self):
        tpl_dirs = [os.path.join(ROOT, "templates"), os.path.join(ROOT, "app1", "templates")]
        tpl_dirs += glob.glob(os.path.join(ROOT, "*", "templates"))
        files = _templates()
        texts = {p: _read(p) for p in files}

        def find(name):
            for d in tpl_dirs:
                p = os.path.join(d, name)
                if os.path.exists(p):
                    return p
            return None

        def chain(path, seen):
            if not path or path in seen:
                return ""
            seen.add(path)
            txt = texts.get(path) or _read(path)
            out = txt
            for m in re.finditer(r"""{%-?\s*(?:extends|include|import|from)\s+['"]([^'"]+)['"]""", txt):
                out += chain(find(m.group(1)), seen)
            return out

        def defs(txt):
            s = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", txt))
            s |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", txt))
            s |= set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", txt))
            return s

        global_defs = defs("".join(_read(p) for p in _static_js()))
        builtins = {"if", "return", "confirm", "alert", "prompt", "setTimeout", "fetch", "parseInt",
                    "encodeURIComponent", "Number", "String", "JSON", "Object", "Array", "Promise",
                    "Math", "Date", "void", "typeof", "new", "translateY", "translateX", "var", "rgba"}
        bad = []
        for path in files:
            txt = re.sub(r"\{\{.*?\}\}", "X", texts[path], flags=re.S)   # Jinja output may hold quotes
            handlers = re.findall(r"""\bon(?:click|change|input|submit|keyup|keydown|blur|focus)\s*=\s*"([^"]*)\"""", txt)
            if not handlers:
                continue
            ctx = chain(path, set())
            base = os.path.basename(path)
            inc = re.compile(r"""{%-?\s*include\s+['"][^'"]*""" + re.escape(base) + "['\"]")
            for other in files:
                if inc.search(texts[other]):
                    ctx += chain(other, set())
            known = defs(ctx) | global_defs | builtins
            for code in handlers:
                for name in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", code):
                    if name not in known:
                        bad.append("%s %s()" % (_rel(path), name))
        self.assertEqual(sorted(set(bad)), [], "inline handler calls a function the page never defines")


class HalfDoneFeatureTests(unittest.TestCase):
    """Behaviour of the controls the wiring scan found unfinished."""

    def test_404_for_a_signed_in_user_renders_the_designed_page(self):
        app = get_app()
        c = client_as(app, "employee")
        with mock.patch("white_label_global_integration.get_template_context", return_value={}):
            r = c.get("/den-her-side-findes-ikke")
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn("Vi kunne ikke finde den side", html)   # not the bare-text fallback
        self.assertIn("Til min forside", html)

    def test_contact_redirects_to_support_with_contact_details(self):
        c = get_app().test_client()
        r = c.get("/contact")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/support"))

    def _responder(self, hashed):
        def r(sql, params):
            s = " ".join(sql.split()).lower()
            if "from users where username" in s or "from users where id" in s:
                return {"id": 21, "username": "emma", "password": hashed, "credits": 0,
                        "role": "user", "email": "emma@x.dk"}
            return None
        return r

    def _login(self, data, twofa=False):
        import login_guard
        login_guard.reset()
        app = get_app()
        _fake, p = patch_mysql(app, self._responder(generate_password_hash("Correct-Horse-9")))
        with p, mock.patch("two_factor.is_enabled", return_value=twofa), \
                mock.patch("two_factor.verify_login", return_value=True), \
                mock.patch("two_factor.enrollment_required", return_value=False):
            c = app.test_client()
            c.post("/login", data=dict(username="emma", password="Correct-Horse-9", **data))
            if twofa:
                c.post("/login/2fa", data={"code": "123456"})
            with c.session_transaction() as s:
                return s.get("user"), s.permanent

    def test_remember_me_makes_the_session_outlive_the_browser(self):
        self.assertEqual(self._login({"remember": "on"}), ("emma", True))

    def test_without_remember_me_the_session_ends_with_the_browser(self):
        self.assertEqual(self._login({}), ("emma", False))

    def test_remember_me_survives_the_2fa_step(self):
        self.assertEqual(self._login({"remember": "on"}, twofa=True), ("emma", True))
        self.assertEqual(self._login({}, twofa=True), ("emma", False))

    def test_remember_me_lifetime_is_configured(self):
        self.assertEqual(get_app().permanent_session_lifetime.days, 30)

    def test_ask_ai_url_opens_chat_about_the_course(self):
        from urllib.parse import parse_qs, urlsplit
        import catalog_service
        url = catalog_service.build_ask_ai_url({"handle": "prince2", "title": "PRINCE2 & Agile"})
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/chat")
        self.assertEqual(parse_qs(parts.query)["intent"], ['Fortæl mig mere om kurset "PRINCE2 & Agile"'])

    def test_product_page_ask_ai_link_carries_the_course(self):
        txt = _read(os.path.join(ROOT, "templates", "fm", "product_detail.html"))
        self.assertNotIn("url_for('app1.index'", txt)   # legacy URL redirected and lost the course
        self.assertIn("url_for('futurematch.chat', intent=", txt)

    def test_register_success_copy_button_is_wired_and_never_copies_a_password(self):
        txt = _read(os.path.join(ROOT, "templates", "fm", "register_success.html"))
        self.assertIn('onclick="copyCredentials()"', txt)
        self.assertNotIn("cred-password", txt)
        self.assertIn('id="cred-login"', txt)

    def test_branding_hub_enable_button_toggles_and_returns_to_the_hub(self):
        app = get_app()
        c = client_as(app, "admin")
        with mock.patch("companies.set_custom_branding_feature", return_value=True) as setter:
            r = c.post("/companies/branding/toggle-feature",
                       data={"company_id": "7", "enabled": "1", "back": "branding"})
        setter.assert_called_once_with(7, True)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/companies/branding/7"))
        txt = _read(os.path.join(ROOT, "templates", "fm", "branding.html"))
        self.assertIn("url_for('companies.toggle_branding_feature')", txt)

    def test_branding_toggle_is_platform_admin_only(self):
        app = get_app()
        c = client_as(app, "company_admin")
        with mock.patch("companies.set_custom_branding_feature", return_value=True) as setter:
            c.post("/companies/branding/toggle-feature", data={"company_id": "7", "enabled": "1"})
        setter.assert_not_called()

    def test_company_settings_fallback_has_no_dead_deactivate_button(self):
        txt = _read(os.path.join(ROOT, "templates", "fm", "company_settings.html"))
        self.assertNotIn("disabled>Deaktiver", txt)
        self.assertIn("url_for('settings_hub.index')", txt)


if __name__ == "__main__":
    unittest.main()
