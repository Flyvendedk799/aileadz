"""order_fulfillment.case_states: one batched "where does it stand" reading for a list of orders."""
import json

import order_fulfillment


class _Cur:
    def __init__(self, details=(), changes=(), reviews=(), fail=False):
        self._sets = [list(details), list(changes), list(reviews)]
        self._fail = fail
        self.queries = []
        self._rows = []

    def execute(self, sql, params=None):
        if self._fail:
            raise RuntimeError("table missing")
        self.queries.append((sql, list(params or [])))
        self._rows = self._sets[len(self.queries) - 1]

    def fetchall(self):
        return self._rows


def test_empty_batch_asks_nothing():
    cur = _Cur()
    assert order_fulfillment.case_states(cur, []) == {}
    assert cur.queries == []


def test_batch_uses_three_queries_and_reads_each_order():
    rows = [
        {"order_id": "A", "status": "pending_approval"},
        {"order_id": "B", "status": "booked"},
        {"order_id": "C", "status": "approved"},
    ]
    cur = _Cur(
        details=[{"order_id": "B", "booking_json": json.dumps({}), "completion_state": None}],
        changes=[{"order_id": "B"}],
    )
    out = order_fulfillment.case_states(cur, rows)
    assert len(cur.queries) == 3
    assert all(q[1] == ["A", "B", "C"] for q in cur.queries)
    assert set(out) == {"A", "B", "C"}
    for case in out.values():
        assert {"status_label", "waiting_on_label", "next_step"} <= set(case)
    # An open change request changes whose move it is on the booked order.
    plain = order_fulfillment.case_states(_Cur(), [rows[1]])["B"]
    assert out["B"]["waiting_on"] != plain["waiting_on"] or out["B"]["next_step"] != plain["next_step"]


def test_a_failing_lookup_degrades_to_the_status_reading():
    out = order_fulfillment.case_states(_Cur(fail=True), [{"order_id": "A", "status": "approved"}])
    assert out["A"]["status"] == "approved"
    assert out["A"]["next_step"]
