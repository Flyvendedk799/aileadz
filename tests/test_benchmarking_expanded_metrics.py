"""Expanded cross-tenant benchmarking metrics (plan #15).

benchmarking.py previously exposed 4 commerce/AI metrics (spend_per_employee,
completion_rate, courses_per_employee, ai_adoption_rate) behind the company-level
k-anon cohort gate. Plan #15 adds four more behind the SAME gate:

  * skill_gap_closure   — % of targeted skill level reached (higher = better)
  * budget_utilization  — % of allocated learning budget spent (higher = better)
  * engagement_rate     — % active in last 30d on the unified signal (higher)
  * avg_completion_days — avg days to complete a course (LOWER is better)

These tests lock in:
  1. All eight metric keys are present in the payload, with the new four.
  2. avg_completion_days is direction-aware: a LOWER value ranks in a HIGHER
     percentile (so "higher percentile = better" stays consistent for the viz).
  3. higher-is-better metrics keep the normal "<= yours" percentile.
  4. The k-anon density gate still governs ALL metrics: a sub-k cohort returns
     NO cohort_avg / cohort_median / your_percentile for the new metrics either.
  5. _percentile_of direction flag is correct in isolation.
  6. The payload never leaks a per-company breakdown — only aggregates / own value.

We mock cohort_for + _company_value_maps so no Flask app context / live MySQL is
needed (mirrors test_small_tenant_suppression_floor.py's fake-DB approach).
"""

import unittest
from unittest import mock

import run  # noqa: F401  installs the pymysql->MySQLdb shim
import benchmarking
import kanon


K = kanon.K_DEFAULT


NEW_KEYS = (
    'skill_gap_closure',
    'budget_utilization',
    'engagement_rate',
    'avg_completion_days',
)
ALL_KEYS = (
    'spend_per_employee', 'completion_rate', 'courses_per_employee',
    'ai_adoption_rate',
) + NEW_KEYS


def _safe_cohort(n):
    """A k-safe cohort of ``n`` distinct company ids (1..n), industry set."""
    ids = list(range(1, n + 1))
    return {
        'company_id': 1,
        'industry': 'Tech',
        'company_size': 'medium',
        'cohort_company_ids': ids,
        'cohort_size': n,
        'k': K,
    }


def _value_maps(per_company):
    """Build the {metric: {company_id: value}} structure benchmarking expects
    from a {company_id: {metric: value}} convenience dict."""
    maps = {key: {} for key in ALL_KEYS}
    for cid, metrics in per_company.items():
        for key in ALL_KEYS:
            maps[key][cid] = metrics.get(key, 0.0)
    return maps


def _run_benchmark(cohort, value_maps, company_id=1):
    with mock.patch.object(benchmarking, 'cohort_for', return_value=cohort), \
         mock.patch.object(benchmarking, '_company_value_maps',
                           return_value=value_maps):
        return benchmarking.benchmark(company_id)


class ExpandedMetricsPresenceTests(unittest.TestCase):

    def test_all_eight_metrics_present(self):
        n = K + 1
        per_company = {c: {key: 10.0 for key in ALL_KEYS} for c in range(1, n + 1)}
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company))
        keys = [m['key'] for m in out['metrics']]
        for key in ALL_KEYS:
            self.assertIn(key, keys, f"missing metric {key}")
        # No duplicates / unexpected keys.
        self.assertEqual(len(keys), len(ALL_KEYS))

    def test_new_metrics_carry_higher_is_better_flag(self):
        n = K + 1
        per_company = {c: {key: 5.0 for key in ALL_KEYS} for c in range(1, n + 1)}
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company))
        by_key = {m['key']: m for m in out['metrics']}
        # avg_completion_days is the only lower-is-better metric.
        self.assertFalse(by_key['avg_completion_days']['higher_is_better'])
        for key in ('skill_gap_closure', 'budget_utilization', 'engagement_rate'):
            self.assertTrue(by_key[key]['higher_is_better'])


class DirectionAwarePercentileTests(unittest.TestCase):

    def test_avg_completion_days_lower_value_ranks_higher(self):
        # Company 1 is the FASTEST (lowest days) -> should be top percentile.
        n = K + 1
        per_company = {}
        for c in range(1, n + 1):
            # days = c*10 -> company 1 = 10 days (best), company n = slowest.
            per_company[c] = {key: 0.0 for key in ALL_KEYS}
            per_company[c]['avg_completion_days'] = float(c * 10)
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company),
                             company_id=1)
        by_key = {m['key']: m for m in out['metrics']}
        days = by_key['avg_completion_days']
        self.assertTrue(days['safe'])
        # Fastest company ranks at the TOP (>= yours share == 100%).
        self.assertEqual(days['your_percentile'], 100.0)

    def test_avg_completion_days_slowest_ranks_lowest(self):
        n = K + 1
        per_company = {}
        for c in range(1, n + 1):
            per_company[c] = {key: 0.0 for key in ALL_KEYS}
            per_company[c]['avg_completion_days'] = float(c * 10)
        # Requesting company is the SLOWEST (id n) -> worst -> low percentile.
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company),
                             company_id=n)
        by_key = {m['key']: m for m in out['metrics']}
        days = by_key['avg_completion_days']
        # Only one company (itself) is >= it -> 1/n share.
        self.assertEqual(days['your_percentile'], round(1.0 / n * 100.0, 0))

    def test_higher_is_better_metric_uses_normal_rank(self):
        # skill_gap_closure: HIGHER is better. Company 1 has the HIGHEST -> top.
        n = K + 1
        per_company = {}
        for c in range(1, n + 1):
            per_company[c] = {key: 0.0 for key in ALL_KEYS}
            # closure = 100 - c -> company 1 highest.
            per_company[c]['skill_gap_closure'] = float(100 - c)
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company),
                             company_id=1)
        by_key = {m['key']: m for m in out['metrics']}
        closure = by_key['skill_gap_closure']
        self.assertTrue(closure['safe'])
        self.assertEqual(closure['your_percentile'], 100.0)


class PercentileOfDirectionUnitTests(unittest.TestCase):

    def test_higher_is_better_share_leq(self):
        # values: 10,20,30; yours=30 -> 3/3 leq -> 100.
        self.assertEqual(
            benchmarking._percentile_of(30, [10, 20, 30], True), 100.0)
        # yours=10 -> 1/3 leq -> 33.
        self.assertEqual(
            benchmarking._percentile_of(10, [10, 20, 30], True), 33.0)

    def test_lower_is_better_share_geq(self):
        # values: 10,20,30; yours=10 (best, lowest) -> 3/3 geq -> 100.
        self.assertEqual(
            benchmarking._percentile_of(10, [10, 20, 30], False), 100.0)
        # yours=30 (worst, highest) -> 1/3 geq -> 33.
        self.assertEqual(
            benchmarking._percentile_of(30, [10, 20, 30], False), 33.0)


class KAnonGateOnNewMetricsTests(unittest.TestCase):

    def test_sub_k_cohort_suppresses_new_metric_cohort_stats(self):
        n = K - 1  # below the floor
        cohort = {
            'company_id': 1, 'industry': 'Tech', 'company_size': 'small',
            'cohort_company_ids': list(range(1, n + 1)),
            'cohort_size': n, 'k': K,
        }
        per_company = {c: {key: 7.0 for key in ALL_KEYS} for c in range(1, n + 1)}
        out = _run_benchmark(cohort, _value_maps(per_company))
        self.assertFalse(out['safe'])
        by_key = {m['key']: m for m in out['metrics']}
        for key in NEW_KEYS:
            m = by_key[key]
            self.assertFalse(m['safe'], f"{key} should be locked sub-k")
            self.assertIsNone(m['cohort_avg'], f"{key} leaked cohort_avg sub-k")
            self.assertIsNone(m['cohort_median'])
            self.assertIsNone(m['your_percentile'])
            self.assertTrue(m['note'], f"{key} missing the too-few note")
            # The company's OWN value is always fine to show to itself.
            self.assertEqual(m['your_value'], 7.0)

    def test_safe_cohort_exposes_new_metric_aggregates(self):
        n = K + 2
        per_company = {}
        for c in range(1, n + 1):
            per_company[c] = {key: float(c) for key in ALL_KEYS}
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company))
        self.assertTrue(out['safe'])
        by_key = {m['key']: m for m in out['metrics']}
        for key in NEW_KEYS:
            m = by_key[key]
            self.assertTrue(m['safe'], f"{key} not exposed in safe cohort")
            self.assertIsNotNone(m['cohort_avg'])
            self.assertIsNotNone(m['cohort_median'])
            self.assertIsNotNone(m['your_percentile'])


class NoPerCompanyLeakTests(unittest.TestCase):

    def test_payload_has_no_per_company_breakdown(self):
        n = K + 1
        per_company = {c: {key: float(c) for key in ALL_KEYS}
                       for c in range(1, n + 1)}
        out = _run_benchmark(_safe_cohort(n), _value_maps(per_company))
        # Only aggregate keys may appear on each metric — never raw vectors,
        # company id lists, or per-peer values.
        allowed = {
            'key', 'label', 'unit', 'higher_is_better', 'your_value',
            'cohort_avg', 'cohort_median', 'your_percentile', 'safe', 'note',
        }
        for m in out['metrics']:
            self.assertTrue(set(m.keys()) <= allowed,
                            f"unexpected fields leaked: {set(m.keys()) - allowed}")
        # The top-level payload must not echo the cohort company id list.
        self.assertNotIn('cohort_company_ids', out)


if __name__ == '__main__':
    unittest.main()
