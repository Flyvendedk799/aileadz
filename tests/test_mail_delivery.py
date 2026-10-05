import datetime
import unittest
from unittest import mock
from flask import Flask
from tests.sqlite_mysql import SqliteMysql
import mail_delivery as delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.db = SqliteMysql()
        self.app = Flask(__name__)
        self.app.mysql = self.db
        ctx = self.app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)
        self.addCleanup(self.db.raw.close)

    def test_queue_deduplicates_per_recipient_and_survives_sender_failure(self):
        delivery.enqueue("one@example.invalid", "Subject", "order_booked", dedupe_key="order:1")
        delivery.enqueue("one@example.invalid", "Subject", "order_booked", dedupe_key="order:1")
        delivery.enqueue("two@example.invalid", "Subject", "order_booked", dedupe_key="order:1")
        self.assertEqual(len(self.db.query("SELECT * FROM mail_outbox")), 2)
        result = delivery.drain(deliver=lambda *a, **k: False)
        self.assertEqual(result["retry"], 2)
        self.assertEqual({r["state"] for r in self.db.query("SELECT state FROM mail_outbox")}, {"pending"})
        result = delivery.drain(deliver=lambda *a, **k: True, now=datetime.datetime.now() + datetime.timedelta(hours=1))
        self.assertEqual(result["sent"], 2)
        self.assertEqual(delivery.drain(deliver=mock.Mock())["sent"], 0)

    def test_unknown_transport_result_is_not_blindly_replayed(self):
        delivery.enqueue("one@example.invalid", "Subject", "order_booked")

        def uncertain(*a, **kw):
            raise TimeoutError("transport uncertainty")

        self.assertEqual(delivery.drain(deliver=uncertain)["uncertain"], 1)
        sender = mock.Mock()
        delivery.drain(deliver=sender, now=datetime.datetime.now() + datetime.timedelta(days=1))
        sender.assert_not_called()

    def test_staging_rolls_back_with_the_business_transaction(self):
        cur = self.db.connection.cursor()
        delivery.enqueue("one@example.invalid", "Subject", "order_booked", cursor=cur)
        self.db.connection.rollback()
        self.assertEqual(self.db.query("SELECT * FROM mail_outbox"), [])

    def test_expired_sending_lease_requires_explicit_recovery(self):
        delivery.enqueue("one@example.invalid", "Subject", "order_booked")
        self.db.execute("UPDATE mail_outbox SET state='sending',locked_until='2020-01-01'")
        sender = mock.Mock()
        delivery.drain(deliver=sender)
        sender.assert_not_called()
        self.assertEqual(self.db.one("SELECT state FROM mail_outbox")["state"], "uncertain")

    def test_failed_report_does_not_record_sent(self):
        import scheduled_reports

        self.db.raw.execute(
            "CREATE TABLE company_report_schedules (id INTEGER PRIMARY KEY,company_id INTEGER,report_type TEXT,cadence TEXT,department TEXT,last_sent_at TEXT,last_status TEXT,enabled INTEGER)"
        )
        self.db.execute("INSERT INTO company_report_schedules VALUES (1,7,'budget','weekly',NULL,NULL,NULL,1)")
        cur = self.db.connection.cursor()
        with (
            mock.patch("report_exports.build", return_value=(["x"], [["value"]])),
            mock.patch.object(scheduled_reports, "_hr_emails", return_value=["one@example.invalid"]),
        ):
            result = scheduled_reports.run_due_schedules(cur, send=lambda *a: False)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["errors"], 1)
        self.assertIsNone(self.db.one("SELECT last_sent_at FROM company_report_schedules")["last_sent_at"])
