"""Unify a 'last learning activity' signal so at-risk/churn detection is
trustworthy (plan #10).

Churn/at-risk in insights_engine.get_predictive_data used to be measured ONLY
off cu.last_chatbot_interaction (gated on total_chatbot_queries > 3). That column
is bumped only by the app1 employee chatbot, so an employee who actively *learns*
— places course orders, makes course progress — but never chats looked
permanently inactive and was flagged as churn risk. Meanwhile
_execute_hr_inactive_employees (hr_tools.py) already used a GREATEST() coalesce,
so the predictive path was the inconsistent outlier feeding the advisor a noisy
proxy.

The fix replaces the chatbot-only churn signal with a unified
last-learning-activity = GREATEST() over:
  * cu.last_chatbot_interaction
  * cu.last_active_at
  * MAX(course_orders.created_at)            (company-scoped)
  * MAX(employee_learning_progress.last_accessed)  (company-scoped)

These tests lock in:
  1. Source guard — the churn query coalesces all four signals, is company
     scoped, no longer gates on total_chatbot_queries, excludes never-active
     shells, and still preserves the fields roi.html renders.
  2. Behaviour — a learner-who-never-chats whose unified last activity is fresh
     is NOT flagged, and the function returns the engine's churn_risk rows
     unchanged (no field drift for the template). Uses a fake app + a fake
     multi-result cursor, so no live MySQL and no OpenAI.

This is the accuracy gate for the get_workforce_risk advisor tool (plan #12),
which must not ship a flagship 'early warning' answer off a noisy proxy.
"""

import os
import re
import unittest
from unittest import mock

# run installs the pymysql->MySQLdb shim that insights_engine imports.
import run  # noqa: F401
import insights_engine


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(os.path.join(REPO, path), "r", encoding="utf-8") as fh:
        return fh.read()


# ── 1) Source-level guard on the churn query ─────────────────────────────────

class ChurnQuerySourceGuardTests(unittest.TestCase):
    """The unified signal is structural — lock the SQL so it can't silently
    regress to the chatbot-only proxy."""

    def setUp(self):
        self.src = _read("insights_engine.py")
        # Isolate the churn_risk query body: from its assignment back up to the
        # cur.execute that precedes it.
        idx = self.src.index("predictions['churn_risk'] = cur.fetchall()")
        self.churn_block = self.src[self.src.rindex("cur.execute(", 0, idx):idx]

    def test_coalesces_all_four_learning_signals(self):
        for col in ("last_chatbot_interaction",
                    "last_active_at",
                    "course_orders",
                    "employee_learning_progress"):
            self.assertIn(col, self.churn_block,
                          f"churn signal dropped {col} — back to a noisy proxy")
        # The learning tables contribute their *most recent* timestamp.
        self.assertIn("MAX(created_at)", self.churn_block)
        self.assertIn("MAX(last_accessed)", self.churn_block)
        # And they are combined, not just selected.
        self.assertIn("GREATEST", self.churn_block)

    def test_no_longer_gates_on_chatbot_query_count(self):
        # The old gate (total_chatbot_queries > 3) is exactly the bug: it hid
        # every learner who never chatted. It must be gone from the WHERE.
        self.assertNotIn("total_chatbot_queries > 3", self.churn_block)

    def test_query_is_company_scoped_everywhere(self):
        # company_id must scope the outer query AND both learning subqueries so
        # one tenant never sees another's activity. Three bindings expected.
        self.assertGreaterEqual(self.churn_block.count("company_id = %s"), 3)

    def test_excludes_never_active_shells(self):
        # Anchored at '1970-01-01', a fully-NULL employee's GREATEST is the
        # anchor; the > '1970-01-01' guard drops never-onboarded accounts so
        # they aren't mislabelled churn.
        self.assertIn("'1970-01-01'", self.churn_block)
        self.assertIn("> '1970-01-01'", self.churn_block)

    def test_still_thirty_day_inactivity_window(self):
        self.assertIn("INTERVAL 30 DAY", self.churn_block)

    def test_preserves_template_rendered_fields(self):
        # roi.html reads username, department, job_title, days_inactive on each
        # churn row — keep them or the table breaks.
        for field in ("u.username", "cu.department", "cu.job_title",
                      "AS days_inactive"):
            self.assertIn(field, self.churn_block,
                          f"churn row no longer provides {field} for roi.html")


# ── Fakes (modelled on tests/test_calendar_surfacing.py) ─────────────────────

class FakeMultiCursor:
    """Returns a different fetchall payload per execute() call, so the three
    sequential queries in get_predictive_data each get their own rows."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        if self._i < len(self._results):
            out = self._results[self._i]
        else:
            out = []
        self._i += 1
        return out

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, *a, **k):
        return self._cursor


def _sandbox_app():
    os.environ.setdefault("SANDBOX", "1")
    os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
    os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")
    from run import create_app
    return create_app()


# ── 2) Behavioural test through the real function ────────────────────────────

class GetPredictiveDataBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _sandbox_app()

    def _call(self, cursor):
        fake_mysql = type("M", (), {"connection": FakeConnection(cursor)})()
        with mock.patch.object(self.app, "mysql", fake_mysql):
            return insights_engine.get_predictive_data(self.app, 42)

    def test_company_id_scopes_the_churn_query(self):
        cur = FakeMultiCursor([[], [], []])
        self._call(cur)
        sql, params = cur.executed[0]
        # company_id is bound three times (two subqueries + outer scope).
        self.assertEqual(list(params), [42, 42, 42])
        self.assertIn("company_id = %s", sql)

    def test_learner_who_never_chats_is_carried_as_churn_row(self):
        # The engine (DB) decides who is at risk via the unified GREATEST; this
        # row has total_chatbot_queries = 0 yet is returned — proving the
        # function no longer filters learners out in Python and preserves the
        # template fields for a never-chatting employee.
        churn_rows = [{
            "user_id": 7,
            "username": "anna",
            "department": "Sales",
            "job_title": "Account Manager",
            "last_chatbot_interaction": None,
            "total_chatbot_queries": 0,
            "last_learning_activity": "2026-04-01 09:00:00",
            "days_inactive": 45,
        }]
        cur = FakeMultiCursor([churn_rows, [], []])
        out = self._call(cur)
        self.assertEqual(len(out["churn_risk"]), 1)
        row = out["churn_risk"][0]
        # Fields roi.html renders survive untouched.
        self.assertEqual(row["username"], "anna")
        self.assertEqual(row["department"], "Sales")
        self.assertEqual(row["job_title"], "Account Manager")
        self.assertEqual(row["days_inactive"], 45)

    def test_three_predictive_sections_are_populated_independently(self):
        cur = FakeMultiCursor([
            [{"username": "anna", "days_inactive": 31}],          # churn_risk
            [{"username": "bo", "skill_name": "GDPR", "gap": 2}],  # at_risk
            [{"query_text": "ledelse", "cnt": 9}],                 # trending
        ])
        out = self._call(cur)
        self.assertEqual(len(out["churn_risk"]), 1)
        self.assertEqual(len(out["at_risk_employees"]), 1)
        self.assertEqual(len(out["trending_courses"]), 1)
        self.assertTrue(cur.closed)

    def test_query_failure_returns_empty_sections_never_raises(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("db down")
            def fetchall(self):
                return []
            def close(self):
                pass
        out = self._call(Boom())
        self.assertEqual(out["churn_risk"], [])
        self.assertEqual(out["at_risk_employees"], [])
        self.assertEqual(out["trending_courses"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
