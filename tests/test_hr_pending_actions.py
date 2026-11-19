"""HR's pending actions name the company's next moves on orders past approval."""
import json

from flask import Flask

import hr_tools


class _Cur:
    def __init__(self, orders, changes=()):
        self.orders, self.changes, self._rows = orders, list(changes), []
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append((" ".join(sql.split()), tuple(params or ())))
        if "FROM course_orders" in sql:
            self._rows = self.orders
        elif "course_order_changes" in sql:
            self._rows = self.changes
        elif "COUNT(*)" in sql:
            self._rows = [{"cnt": 0}]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


def _run(monkeypatch, cur):
    monkeypatch.setattr(hr_tools, "_get_cursor", lambda: cur)
    app = Flask(__name__)
    app.secret_key = "t"
    with app.test_request_context():
        from flask import session
        session["company_id"] = 7
        return json.loads(hr_tools._execute_get_pending_actions({}))


def test_orders_waiting_on_the_company_are_listed_per_stage(monkeypatch):
    cur = _Cur([{"order_id": "A", "status": "approved", "variant_date": None},
                {"order_id": "B", "status": "approved", "variant_date": None},
                {"order_id": "C", "status": "booked", "variant_date": None}],
               changes=[{"order_id": "C"}])
    out = _run(monkeypatch, cur)
    by_type = {a["type"]: a for a in out["actions"]}
    assert by_type["order_booker"]["count"] == 2
    assert by_type["order_change"]["count"] == 1
    assert "ændringsønske" in by_type["order_change"]["message"]
    orders_sql = next(q for q in cur.seen if "FROM course_orders" in q[0])
    assert "company_id = %s" in orders_sql[0] and orders_sql[1] == (7,)


def test_nothing_waiting_means_no_order_actions(monkeypatch):
    out = _run(monkeypatch, _Cur([]))
    assert not [a for a in out["actions"] if a["type"].startswith("order_")]
