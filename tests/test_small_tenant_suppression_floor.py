"""Small-tenant k-anon suppression floor on the HR-exposed analytics (plan #9).

report_query.conversion_funnel and report_query.attributed_revenue return
SCALAR company-level counts / sums whose only safety rests on aggregation —
neither had a suppress_small_groups call (verified). The HR buyer is now shown
these figures for its OWN company (never platform-wide), so for a very small
tenant a single-session funnel or a single attributed order is re-identifying.

Plan #9 adds an explicit small-tenant suppression floor: when the underlying
session cohort is below kanon.K_DEFAULT, the figure is zeroed and a Danish
anon_note is set. cohort_retention already suppresses sub-k cohorts row-by-row
(verified in report_query) and needs no floor flag.

These tests lock in:
  1. conversion_funnel(suppress_floor=True) zeroes EVERY stage + rate and sets
     suppressed/anon_note when sessions are below the floor, but passes the
     figures through unchanged above the floor.
  2. attributed_revenue(suppress_floor=True) zeroes revenue/orders/sessions/AOV
     and sets suppressed/anon_note when attributed sessions are below the floor,
     but passes through above it.
  3. The floor is OFF by default (admin platform-wide path is unchanged).
  4. An EMPTY result (0 sessions) is "no data", not a privacy leak — it is never
     marked suppressed, so the empty-state copy still renders.

Uses a fake cursor (no live MySQL); _cursor is patched directly so no Flask app
context is needed.
"""

import os
import unittest
from unittest import mock

import run  # noqa: F401  installs the pymysql->MySQLdb shim
import report_query
import kanon


K = kanon.K_DEFAULT


class FakeCursor:
    """Returns a staged dict per fetchone() call (the funnel issues two SELECTs:
    fact row then order row; attributed_revenue issues one)."""

    def __init__(self, rows):
        self._rows = list(rows)
        self._i = 0
        self.closed = False

    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        if self._i < len(self._rows):
            row = self._rows[self._i]
            self._i += 1
            return row
        return {}

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


def _patch_cursor(rows):
    return mock.patch.object(report_query, "_cursor",
                             return_value=FakeCursor(rows))


# ── conversion_funnel ────────────────────────────────────────────────────────

class ConversionFunnelFloorTests(unittest.TestCase):

    def test_below_floor_suppresses_every_stage(self):
        # sessions below k -> a single learner's whole journey is re-identifiable.
        fact = {'sessions': K - 1, 'searches': K - 1, 'shown': K - 1}
        order = {'ordered': 1}
        with _patch_cursor([fact, order]):
            out = report_query.conversion_funnel(42, 30, suppress_floor=True)
        self.assertTrue(out['suppressed'])
        self.assertTrue(out['anon_note'])
        for k_ in ('sessions', 'searches', 'shown', 'ordered'):
            self.assertEqual(out[k_], 0, f"{k_} not zeroed below floor")
        for k_ in ('rate_search', 'rate_shown', 'rate_ordered', 'overall_rate'):
            self.assertEqual(out[k_], 0.0, f"{k_} not zeroed below floor")

    def test_at_or_above_floor_passes_through(self):
        fact = {'sessions': K + 5, 'searches': K + 2, 'shown': K + 1}
        order = {'ordered': K}
        with _patch_cursor([fact, order]):
            out = report_query.conversion_funnel(42, 30, suppress_floor=True)
        self.assertFalse(out['suppressed'])
        self.assertIsNone(out['anon_note'])
        self.assertEqual(out['sessions'], K + 5)
        self.assertEqual(out['ordered'], K)
        # Rates are computed (non-suppressed): ordered/sessions > 0.
        self.assertGreater(out['overall_rate'], 0.0)

    def test_floor_off_by_default_admin_path_unchanged(self):
        # Platform-wide admin call must not suppress even a small figure.
        fact = {'sessions': K - 1, 'searches': K - 1, 'shown': 0}
        order = {'ordered': 0}
        with _patch_cursor([fact, order]):
            out = report_query.conversion_funnel(None, 30)  # suppress_floor=False
        self.assertFalse(out['suppressed'])
        self.assertEqual(out['sessions'], K - 1)

    def test_empty_funnel_is_not_marked_suppressed(self):
        # 0 sessions == no data, not a privacy leak; keep the empty state honest.
        fact = {'sessions': 0, 'searches': 0, 'shown': 0}
        order = {'ordered': 0}
        with _patch_cursor([fact, order]):
            out = report_query.conversion_funnel(42, 30, suppress_floor=True)
        self.assertFalse(out['suppressed'])
        self.assertIsNone(out['anon_note'])
        self.assertEqual(out['sessions'], 0)


# ── attributed_revenue ───────────────────────────────────────────────────────

class AttributedRevenueFloorTests(unittest.TestCase):

    def test_below_floor_suppresses_revenue(self):
        # A single attributed order's DKK value is re-identifiable.
        row = {'revenue': 49999.0, 'orders': 1, 'sessions': K - 1}
        with _patch_cursor([row]):
            out = report_query.attributed_revenue(42, 90, suppress_floor=True)
        self.assertTrue(out['suppressed'])
        self.assertTrue(out['anon_note'])
        self.assertEqual(out['revenue'], 0.0)
        self.assertEqual(out['orders'], 0)
        self.assertEqual(out['sessions'], 0)
        self.assertEqual(out['avg_order_value'], 0.0)

    def test_at_or_above_floor_passes_through(self):
        row = {'revenue': 120000.0, 'orders': K + 3, 'sessions': K + 2}
        with _patch_cursor([row]):
            out = report_query.attributed_revenue(42, 90, suppress_floor=True)
        self.assertFalse(out['suppressed'])
        self.assertIsNone(out['anon_note'])
        self.assertEqual(out['revenue'], 120000.0)
        self.assertEqual(out['orders'], K + 3)
        self.assertGreater(out['avg_order_value'], 0.0)

    def test_floor_off_by_default(self):
        row = {'revenue': 49999.0, 'orders': 1, 'sessions': 1}
        with _patch_cursor([row]):
            out = report_query.attributed_revenue(None, 90)  # default no floor
        self.assertFalse(out['suppressed'])
        self.assertEqual(out['orders'], 1)
        self.assertEqual(out['revenue'], 49999.0)

    def test_empty_attribution_is_not_marked_suppressed(self):
        row = {'revenue': 0, 'orders': 0, 'sessions': 0}
        with _patch_cursor([row]):
            out = report_query.attributed_revenue(42, 90, suppress_floor=True)
        self.assertFalse(out['suppressed'])
        self.assertEqual(out['revenue'], 0.0)


# ── floor helper resolves from kanon ─────────────────────────────────────────

class FloorHelperTests(unittest.TestCase):

    def test_floor_k_tracks_kanon_default(self):
        self.assertEqual(report_query._floor_k(), kanon.K_DEFAULT)

    def test_floor_k_never_below_one_when_kanon_missing(self):
        with mock.patch.object(report_query, "_kanon", None):
            self.assertGreaterEqual(report_query._floor_k(), 1)

    def test_small_tenant_note_is_nonempty_string(self):
        note = report_query._small_tenant_note(K)
        self.assertIsInstance(note, str)
        self.assertTrue(note.strip())


# ── HR routes pass suppress_floor=True / never platform-wide ─────────────────

class HRRouteWiringSourceTests(unittest.TestCase):
    """Structural guard: the HR routes must request the floor and scope to the
    tenant (company['id']), never None / platform-wide."""

    def setUp(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "hr_dashboard", "__init__.py"),
                  "r", encoding="utf-8") as fh:
            self.src = fh.read()

    def test_hr_funnel_requests_floor_and_tenant_scope(self):
        self.assertIn("conversion_funnel(company['id'], days, suppress_floor=True)",
                      self.src)

    def test_hr_attributed_revenue_requests_floor_and_tenant_scope(self):
        self.assertIn("attributed_revenue(\n", self.src)
        self.assertIn("company['id'], 90, suppress_floor=True)", self.src)

    def test_hr_retention_is_tenant_scoped(self):
        self.assertIn("cohort_retention(company['id'], months)", self.src)


if __name__ == "__main__":
    unittest.main()
