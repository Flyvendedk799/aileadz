"""N-5.4: the vendor assistant keeps durable memory, scoped to its own vendor."""

import os
import unittest

os.environ.setdefault("SANDBOX", "1")

from flask import Flask  # noqa: E402

import vendor_conversations as vc  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402


class DurableMemoryTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.raw.executescript(
            "CREATE TABLE conversation_history (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, session_id TEXT, "
            "title TEXT, mode TEXT, messages TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);"
            "CREATE TABLE user_active_sessions (username TEXT, mode TEXT, session_id TEXT, UNIQUE(username, mode));")
        self.app = Flask(__name__)
        self.app.mysql = self.db
        self.app.secret_key = "t"
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    def test_save_and_reload_survive_a_new_process(self):
        who = vc.owner(11)
        sid = vc.new_sid(11)
        self.assertTrue(vc.save(who, sid, [{"role": "user", "content": "Hvordan går det?"},
                                           {"role": "assistant", "content": "Godt."},
                                           {"role": "system", "content": "skal ikke gemmes"}]))
        self.assertEqual([m["role"] for m in vc.load(who, sid)], ["user", "assistant"])

    def test_another_vendor_cannot_read_the_transcript(self):
        sid = vc.new_sid(11)
        vc.save(vc.owner(11), sid, [{"role": "user", "content": "hemmeligt tal"}])
        self.assertEqual(vc.load(vc.owner(12), sid), [])

    def test_resolve_sid_ignores_a_foreign_pointer_and_resumes_last_active(self):
        vc.save(vc.owner(11), "vendor_11_abc", [{"role": "user", "content": "hej"}])
        with self.app.test_request_context("/"):
            from flask import session
            session["vendor_chat_session_id"] = "vendor_12_zzz"      # someone else's id
            sid = vc.resolve_sid(session, 11)
            self.assertEqual(sid, "vendor_11_abc")                   # resumed after "deploy"

    def test_prompt_asks_for_suggestions_and_fences_the_name(self):
        import vendor_portal
        self.assertIn("<suggestions>", vendor_portal.VENDOR_SYSTEM_PROMPT)
        self.assertIn("EKSEMPLER PÅ TONEN", vendor_portal.VENDOR_SYSTEM_PROMPT)
        self.assertTrue(vendor_portal.VENDOR_FALLBACK_SUGGESTIONS)
        src = open(os.path.join(os.path.dirname(__file__), "..", "vendor_portal.py"), encoding="utf-8").read()
        self.assertIn("delimit_untrusted", src)


if __name__ == "__main__":
    unittest.main()
