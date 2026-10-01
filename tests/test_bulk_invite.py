"""N-2.1: bulk CSV invite screen."""

import io
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")

import bulk_invite as bi  # noqa: E402
import run  # noqa: E402
from tests.sqlite_mysql import SqliteMysql  # noqa: E402

CSV = "navn;email;afdeling;stilling;rolle\nMette Hansen;mette@firma.dk;Salg;KAM;employee\nBo;ikke-en-mail;;;\nMette Igen;mette@firma.dk;;;\nChef;chef@firma.dk;Drift;Leder;company_admin\n"


class ParseTests(unittest.TestCase):
    def test_parse_marks_problems_per_row(self):
        rows, errors = bi.parse_csv(CSV)
        self.assertEqual(errors, [])
        problems = [r["problem"] for r in rows]
        self.assertIsNone(problems[0])
        self.assertEqual(problems[1], "Ugyldig e-mail")
        self.assertEqual(problems[2], "Samme e-mail står flere gange")
        self.assertIn("Ukendt rolle", problems[3])        # company_admin can never be granted in bulk

    def test_bom_comma_and_aliases(self):
        rows, _ = bi.parse_csv("﻿Name,E-mail\nAda,ada@x.dk\n")
        self.assertEqual((rows[0]["name"], rows[0]["email"], rows[0]["role"]), ("Ada", "ada@x.dk", "employee"))

    def test_missing_columns_and_empty(self):
        self.assertIn("navn", bi.parse_csv("foo,bar\n1,2\n")[1][0])
        self.assertEqual(bi.parse_csv("")[1], ["Filen er tom."])


class ScreenTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.db.execute("INSERT INTO companies (id, company_name) VALUES (7, 'Firma')")
        self.db.execute("INSERT INTO users (id, username, email) VALUES (2, 'hr', 'hr@firma.dk'), (3, 'gammel', 'gammel@firma.dk'), (4, 'emp', 'emp@firma.dk')")
        self.db.execute("INSERT INTO company_users (company_id, user_id, username, role, status) VALUES (7, 2, 'hr', 'hr_manager', 'active'), (7, 4, 'emp', 'employee', 'active')")
        app = run.create_app()
        app.config["TESTING"] = True
        app.mysql = self.db
        self.app = app
        self.invites = []
        for p in (mock.patch("white_label_global_integration.get_template_context", return_value={}),
                  mock.patch("account_flows.send_invite", side_effect=lambda uid, **kw: self.invites.append((uid, kw)) or True)):
            p.start()
            self.addCleanup(p.stop)

    def client(self, role="hr_manager"):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s.update(user="hr" if role == "hr_manager" else "emp", user_id=2 if role == "hr_manager" else 4,
                     company_id=7, company_role=role, company_name="Firma")
        return c

    def test_preview_changes_nothing_then_confirm_creates_and_invites(self):
        c = self.client()
        prev = c.post("/hr/employees/bulk-invite", data={"action": "preview", "file": (io.BytesIO(CSV.encode()), "m.csv")},
                      content_type="multipart/form-data")
        self.assertEqual(prev.status_code, 200)
        self.assertIn("1 klar", prev.get_data(as_text=True))
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM users WHERE email='mette@firma.dk'")["c"], 0)
        done = c.post("/hr/employees/bulk-invite", data={"action": "confirm", "csv_text": CSV})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM company_users WHERE email='mette@firma.dk'")["c"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM company_users WHERE email='chef@firma.dk'")["c"], 0)
        self.assertEqual(len(self.invites), 1)
        self.assertEqual(self.invites[0][1]["email"], "mette@firma.dk")
        self.assertIn("1 oprettet", done.get_data(as_text=True))

    def test_existing_account_is_linked_not_invited(self):
        done = self.client().post("/hr/employees/bulk-invite",
                                  data={"action": "confirm", "csv_text": "navn;email\nGammel;gammel@firma.dk\n"})
        self.assertIn("Eksisterende bruger tilknyttet", done.get_data(as_text=True))
        self.assertEqual(self.invites, [])

    def test_employee_cannot_use_it_and_anonymous_goes_to_login(self):
        resp = self.client("employee").post("/hr/employees/bulk-invite", data={"action": "confirm", "csv_text": CSV})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS c FROM users WHERE email='mette@firma.dk'")["c"], 0)
        anon = self.app.test_client().get("/hr/employees/bulk-invite")
        self.assertIn("/login", anon.headers["Location"])

    def test_bad_file_is_rejected_politely(self):
        resp = self.client().post("/hr/employees/bulk-invite", data={"action": "preview", "csv_text": "foo\n1\n"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("navn", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
