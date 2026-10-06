"""The mail and delivery page is triageable: times, coloured status, filters, order links,
retry only where it makes sense, and a banner when the queue has stalled."""

import datetime
import os
import unittest
from unittest import mock

os.environ.setdefault("SANDBOX", "1")

import mail_delivery  # noqa: E402
from tests.sqlite_platform import PlatformDB, client_as, make_app, render_patches  # noqa: E402


def _ago(**kw):
    return (datetime.datetime.now() - datetime.timedelta(**kw)).strftime("%Y-%m-%d %H:%M:%S")


class MailPageBase(unittest.TestCase):
    def setUp(self):
        self.db = PlatformDB()
        self.app = make_app(self.db)
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        patcher = render_patches()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db.execute("INSERT INTO companies (id, company_name, company_slug) VALUES (7,'Firma A/S','firma'),(8,'Andet','andet')")
        self.hr = client_as(self.app, user="hr", user_id=3, company_id=7, company_role="hr_manager", company_name="Firma A/S")

    def mail(self, to, subject, state="pending", company=7, created=None, attempts=0, order_id=None, sent=None, available=None):
        created = created or _ago(minutes=1)
        self.db.execute(
            "INSERT INTO mail_outbox (id,company_id,to_email,subject,payload_json,dedupe_key,state,attempts,available_at,created_at,sent_at,order_id) "
            "VALUES (%s,%s,%s,%s,'{}',%s,%s,%s,%s,%s,%s,%s)",
            (subject + to, company, to, subject, subject + to, state, attempts, available or created, created, sent, order_id),
        )

    def page(self, query=""):
        resp = self.hr.get("/hr/leveringer" + query)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)


class MailPageTests(MailPageBase):
    def test_pending_row_never_shows_a_retry_button_but_says_since_when(self):
        self.mail("a@firma.dk", "Venter", "pending")
        html = self.page()
        self.assertIn("I kø siden", html)
        self.assertNotIn("Prøv afsendelse igen", html)

    def test_retry_only_for_failed_and_uncertain(self):
        for state in ("sent", "skipped", "sending"):
            self.mail(state + "@firma.dk", "Mail " + state, state)
        self.assertNotIn("Prøv afsendelse igen", self.page())
        self.mail("f@firma.dk", "Fejlet", "failed", attempts=3)
        self.mail("u@firma.dk", "Usikker", "uncertain", attempts=1)
        html = self.page()
        self.assertEqual(html.count("Prøv afsendelse igen"), 2)
        self.assertIn('name="confirm_uncertain"', html)

    def test_server_refuses_to_retry_a_pending_mail(self):
        self.mail("a@firma.dk", "Venter", "pending", attempts=2)
        row_id = self.db.one("SELECT id FROM mail_outbox")["id"]
        resp = self.hr.post("/hr/leveringer", data={"delivery_id": row_id})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.db.one("SELECT attempts FROM mail_outbox")["attempts"], 2)

    def test_times_and_coloured_status(self):
        self.mail("a@firma.dk", "Sendt mail", "sent", created="2026-10-01 08:30:00", sent="2026-10-01 08:31:00")
        self.mail("b@firma.dk", "Fejlet mail", "failed", attempts=2)
        html = self.page()
        self.assertIn("01.10.2026 08:30", html)
        self.assertIn("01.10.2026 08:31", html)
        self.assertIn("status-pill green", html)
        self.assertIn("status-pill red", html)

    def test_filter_by_state_and_search_and_period(self):
        self.mail("old@firma.dk", "Gammel fejl", "failed", created=_ago(days=20))
        self.mail("new@firma.dk", "Frisk fejl", "failed", created=_ago(days=1))
        self.mail("ok@firma.dk", "Fin mail", "sent", created=_ago(days=1))
        failed = self.page("?state=failed")
        self.assertIn("Frisk fejl", failed)
        self.assertIn("Gammel fejl", failed)
        self.assertNotIn("Fin mail", failed)
        week = self.page("?state=failed&period=7")
        self.assertIn("Frisk fejl", week)
        self.assertNotIn("Gammel fejl", week)
        self.assertIn("Fin mail", self.page("?q=fin"))
        self.assertNotIn("Frisk fejl", self.page("?q=ok%40firma"))

    def test_other_companies_mail_is_never_listed(self):
        self.mail("x@andet.dk", "Hemmelig", "failed", company=8)
        self.assertNotIn("Hemmelig", self.page())

    def test_pagination_is_50_per_page(self):
        for i in range(55):
            self.mail("p%d@firma.dk" % i, "Mail %02d" % i, "sent", created=_ago(minutes=i + 2))
        first, second = self.page(), self.page("?page=2")
        self.assertIn("Side 1 af 2", first)
        self.assertEqual(first.count("Åbn bestilling"), 0)
        self.assertIn("Mail 00", first)
        self.assertNotIn("Mail 54", first)
        self.assertIn("Mail 54", second)

    def test_order_mail_links_to_its_order(self):
        self.mail("a@firma.dk", "Din bestilling", "sent", order_id="ord-123")
        self.assertIn('href="/hr/order/ord-123/details"', self.page())

    def test_banner_when_the_oldest_pending_mail_is_old(self):
        self.mail("a@firma.dk", "Frisk", "pending", created=_ago(minutes=2))
        self.assertNotIn("Mails venter på at blive sendt", self.page())
        self.mail("b@firma.dk", "Gammel", "pending", created=_ago(minutes=45))
        html = self.page()
        self.assertIn("Mails venter på at blive sendt", html)
        self.assertNotIn("/admin/system-health", html)

    def test_banner_ignores_a_retry_scheduled_for_later(self):
        later = (datetime.datetime.now() + datetime.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.mail("a@firma.dk", "Backoff", "pending", created=_ago(hours=2), attempts=2, available=later)
        self.assertNotIn("Mails venter på at blive sendt", self.page())

    def test_platform_admin_banner_links_to_system_health(self):
        self.mail("b@firma.dk", "Gammel", "pending", created=_ago(minutes=45))
        admin = client_as(self.app, user="root", user_id=1, role="admin")
        html = admin.get("/hr/leveringer").get_data(as_text=True)
        self.assertIn("Mails venter på at blive sendt", html)
        self.assertIn("/admin/system-health", html)


class EnqueueOrderLinkTests(MailPageBase):
    def test_order_mail_paths_store_the_order_id(self):
        import order_service

        with mock.patch("email_service._resolve_branding", return_value={}):
            order_service._send_email_safe("a@firma.dk", "Bestilt", "order_booked", 7, order_id="ord-9")
            order_service._send_email_safe("b@firma.dk", "Ændring", "business_update", 7, related_order_id="ord-10")
        rows = {r["to_email"]: r["order_id"] for r in self.db.query("SELECT to_email, order_id FROM mail_outbox")}
        self.assertEqual(rows, {"a@firma.dk": "ord-9", "b@firma.dk": "ord-10"})

    def test_plain_mail_has_no_order_link(self):
        mail_delivery.enqueue("a@firma.dk", "Rapport", "weekly", company_id=7)
        self.assertIsNone(self.db.one("SELECT order_id FROM mail_outbox")["order_id"])


if __name__ == "__main__":
    unittest.main()
