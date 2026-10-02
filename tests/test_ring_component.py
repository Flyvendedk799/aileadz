"""R-4: one completeness-ring component (templates/fm/_ring.html + .fm-ring in fm.css)."""

import os
import re
import unittest

os.environ.setdefault("SANDBOX", "1")

from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class RingMacroTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(PlatformDB())

    def _render(self, src, **ctx):
        with self.app.test_request_context("/"):
            return self.app.jinja_env.from_string(src).render(**ctx)

    def test_large_ring_with_label_keeps_the_ids_page_js_updates(self):
        html = self._render("{% from 'fm/_ring.html' import ring %}"
                            "{{ ring(42, size='lg', id='ringBig', pct_id='ringPct', label='Profilstyrke') }}")
        self.assertIn('class="fm-ring fm-ring--lg" id="ringBig" style="--p:42"', html)
        self.assertIn('<span class="fm-ring-pct" id="ringPct">42%</span>', html)
        self.assertIn('<div class="fm-ring-lab">Profilstyrke</div>', html)

    def test_small_ring_is_the_default_and_has_no_label(self):
        html = self._render("{% from 'fm/_ring.html' import ring %}{{ ring(id='profRing', pct_id='profPct') }}")
        self.assertIn('class="fm-ring fm-ring--sm" id="profRing" style="--p:0"', html)
        self.assertIn('id="profPct">0%<', html)
        self.assertNotIn("fm-ring-lab", html)


class RingIsTheOnlyRingTests(unittest.TestCase):
    def test_pages_use_the_macro_not_their_own_ring_css(self):
        for rel in ("templates/fm/my_profile.html",):
            src = _read(rel)
            self.assertIn("{% from 'fm/_ring.html' import ring %}", src, rel)
            self.assertNotRegex(src, r"\.(ring-big|prof-ring)\b", rel)

    def test_dead_chat_ring_widget_css_is_gone(self):
        self.assertNotRegex(_read("static/futurematch/assets/chat.css"), r"\.ring-(widget|svg|fg|bg|pct|lab|info)\b")

    def test_ring_css_uses_fm_tokens_and_animates(self):
        css = _read("static/futurematch/assets/fm.css")
        rule = re.search(r"\.fm-ring \{[^}]*\}", css).group(0)
        self.assertIn("var(--fm-primary)", rule)
        self.assertIn("var(--fm-surface-3)", rule)
        self.assertIn(".fm-ring, .learn-ring { transition: --p", css)
        self.assertIn('".fm-ring, .learn-ring"', _read("static/futurematch/assets/shell.js"))


class PagesRenderTheRingTests(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        p = render_patches()
        p.start()
        self.addCleanup(p.stop)
        self.db.execute("INSERT INTO users (id, username, email, role) VALUES (5, 'emp', 'e@x.dk', 'user')")
        self.c = client_as(self.app, user="emp", user_id=5, role="user")

    def test_profile_page(self):
        html = self.c.get("/profil").get_data(as_text=True)
        self.assertIn('class="fm-ring fm-ring--lg" id="ringBig"', html)
        self.assertIn('id="ringPct"', html)

    def test_legacy_profiler_url_lands_in_the_assistant(self):
        resp = self.c.get("/ai-profiler?from=profile&c=7")
        self.assertEqual(resp.status_code, 301)
        loc = resp.headers["Location"]
        self.assertTrue(loc.startswith("/chat?"), loc)
        self.assertIn("from=profile", loc)
        self.assertIn("c=7", loc)


if __name__ == "__main__":
    unittest.main()
