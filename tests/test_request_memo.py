"""The current company row is read once per request, not once per layer."""
import unittest
from unittest import mock

from flask import Flask

import request_memo
from tests import sqlite_mysql
from tests.sqlite_platform import PlatformDB, make_app, client_as


class MemoUnitTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_get_requests_load_once_and_writes_always_reload(self):
        calls = []

        def loader():
            calls.append(1)
            return {"n": len(calls)}

        with self.app.test_request_context("/", method="GET"):
            self.assertEqual(request_memo.memo("k", loader), request_memo.memo("k", loader))
        self.assertEqual(len(calls), 1)
        with self.app.test_request_context("/", method="POST"):
            request_memo.memo("k", loader)
            request_memo.memo("k", loader)
        self.assertEqual(len(calls), 3)

    def test_no_request_context_passes_through(self):
        self.assertEqual(request_memo.memo("k", lambda: 5), 5)


class CompanyQueryCountTests(unittest.TestCase):
    def test_hr_approvals_reads_the_company_row_once(self):
        db = PlatformDB()
        self.addCleanup(db.raw.close)
        app = make_app(db)
        db.execute("INSERT INTO companies (id,company_name) VALUES (7,'Firma')")
        db.execute("INSERT INTO users (id,username,email) VALUES (2,'hr','hr@example.invalid')")
        db.execute(
            "INSERT INTO company_users (company_id,user_id,username,role,status,department) "
            "VALUES (7,2,'hr','hr_manager','active','HR')"
        )
        seen = []
        original = sqlite_mysql._Cursor.execute

        def spy(self, sql, params=()):
            seen.append(" ".join(sql.split()).lower())
            return original(self, sql, params)

        client = client_as(app, user="hr", user_id=2, company_id=7, company_role="hr_manager")
        with mock.patch.object(sqlite_mysql._Cursor, "execute", spy):
            response = client.get("/hr/approvals")
        self.assertEqual(response.status_code, 200)
        company_reads = [q for q in seen if "from companies" in q]
        self.assertEqual(len(company_reads), 1, company_reads)


if __name__ == "__main__":
    unittest.main()


class ScriptLoadingTests(unittest.TestCase):
    def test_chat_js_is_only_loaded_by_the_chat_page(self):
        import glob
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        users = [
            os.path.relpath(path, root).replace(os.sep, "/")
            for path in glob.glob(os.path.join(root, "templates", "**", "*.html"), recursive=True)
            if "assets/chat.js" in open(path, encoding="utf-8").read()
        ]
        self.assertEqual(users, ["templates/fm/chat.html"])

    def test_no_template_loads_the_icon_font_from_a_cdn(self):
        import glob
        import os

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = [
            path for path in glob.glob(os.path.join(root, "templates", "**", "*.html"), recursive=True)
            if "font-awesome" in open(path, encoding="utf-8").read()
        ]
        self.assertEqual(offenders, [])
