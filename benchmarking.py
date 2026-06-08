"""
benchmarking.py — cross-tenant industry benchmarking (the data network-effect
feature: "hvordan klarer du dig vs. branchen?").

A company admin sees THEIR OWN company's metrics next to the ANONYMISED peer
cohort — never any individual competitor's data. The peer cohort is the set of
*other* tenant companies in the same industry (broadened to industry-only when a
size-cohort would be too sparse). The requesting company is always counted in
its own cohort.

The k-anonymity contract (critical)
-----------------------------------
A cohort statistic (mean / median / the company's percentile) is returned ONLY
when the cohort contains at least ``k`` DISTINCT tenant companies, decided by
``kanon.is_cohort_safe(n_companies)`` (K_DEFAULT = 5). The unit of anonymity here
is the COMPANY, not the employee: with fewer than k companies an "aggregate" can
be reverse-engineered toward a single competitor (e.g. a 2-company average plus
your own value reveals the other company exactly). So when the cohort is below k
we return NO cohort numbers for that metric and surface a Danish status line
instead. We NEVER compute a cohort stat from < k companies, and we NEVER return
any per-company / per-competitor breakdown — only aggregates over the whole safe
cohort. The company's OWN value is always fine to show to itself.

Safety contract
---------------
Every function is GUARDED and NEVER raises — it is called from a request handler
and must never crash boot or a dashboard. On any error / missing table / stale
connection we return a coherent, safe, empty-ish structure that the template can
render unconditionally. Every query is correctly scoped (a per-company value is
scoped to that company; cohort stats are aggregates over the peer set).
"""

import logging

logger = logging.getLogger(__name__)

# k-anonymity helpers. Guarded import: if kanon is unavailable we FAIL CLOSED on
# anonymity — no cohort numbers are ever returned without a positive safety
# decision from kanon, and the import never crashes boot.
try:
    import kanon as _kanon
except Exception:  # pragma: no cover - boot-safety guard
    _kanon = None


def _k():
    """The active minimum cohort size (number of distinct companies). Guarded;
    defaults to 5 when kanon is unavailable so the gate is never weaker than the
    documented default."""
    try:
        if _kanon is not None:
            return int(getattr(_kanon, 'K_DEFAULT', 5))
    except Exception:
        pass
    return 5


def _cohort_safe(n_companies):
    """True iff a cohort of ``n_companies`` DISTINCT companies may be shown.

    Delegates to ``kanon.is_cohort_safe`` so the density gate is the same one
    used everywhere else. FAILS CLOSED: if kanon is missing or anything goes
    wrong we compare against the default k ourselves and, on total failure,
    refuse (return False) — never expose a sub-k cohort."""
    try:
        if _kanon is not None:
            return bool(_kanon.is_cohort_safe(n_companies, _k()))
        return int(n_companies) >= _k()
    except Exception:
        return False


# Danish status line shown when a cohort is below k.
def _too_few_note():
    try:
        return (
            'Ikke nok virksomheder i branchen til anonym benchmarking endnu '
            '(kræver mindst k={}).'.format(_k())
        )
    except Exception:
        return ('Ikke nok virksomheder i branchen til anonym benchmarking endnu '
                '(kræver mindst k=5).')


# ---------------------------------------------------------------------------
# DB plumbing (mirrors report_query.py: guarded cursor + self-heal)
# ---------------------------------------------------------------------------

def _cursor():
    """Return a DictCursor on the live Flask-MySQL connection, self-healing a
    stale connection once. ``None`` when no cursor can be obtained (callers treat
    that as "no data")."""
    try:
        from flask import current_app
        mysql = getattr(current_app, 'mysql', None)
        if mysql is None:
            return None
        try:
            return mysql.connection.cursor(_dict_cursor())
        except Exception:
            try:
                import db_compat
                db_compat.refresh_flask_mysql_connection(mysql)
            except Exception:
                return None
            return mysql.connection.cursor(_dict_cursor())
    except Exception:
        return None


def _dict_cursor():
    try:
        import MySQLdb.cursors
        return MySQLdb.cursors.DictCursor
    except Exception:
        return None


def _close(cur):
    if cur is not None:
        try:
            cur.close()
        except Exception:
            pass


def _int(value, default=0):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value, ndigits=1):
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Cohort resolution
# ---------------------------------------------------------------------------

def cohort_for(company_id):
    """Resolve the requesting company's industry (+ size) and its peer cohort.

    The cohort is the set of tenant company_ids in the SAME industry, INCLUDING
    the requesting company itself (the unit of anonymity is the company, and the
    requester counts toward the k threshold). Size is resolved and returned for
    display, but the cohort is industry-scoped on purpose: a (industry, size)
    cohort would usually be too sparse to ever clear k, which would make the
    whole feature silently empty. Industry is the broadest sensible peer set.

    Returns a dict (never raises)::

        {
          'company_id': int|None,
          'industry': str|None,        # None when the company has no industry set
          'company_size': str|None,
          'cohort_company_ids': [int, ...],   # includes company_id; [] if unknown
          'cohort_size': int,                 # len(cohort_company_ids)
          'k': int,
        }
    """
    out = {
        'company_id': company_id,
        'industry': None,
        'company_size': None,
        'cohort_company_ids': [],
        'cohort_size': 0,
        'k': _k(),
    }
    if company_id is None:
        return out

    cur = _cursor()
    if cur is None:
        return out

    try:
        cid = int(company_id)
        # 1) resolve this company's industry + size.
        cur.execute(
            "SELECT industry, company_size FROM companies WHERE id = %s LIMIT 1",
            (cid,),
        )
        row = cur.fetchone() or {}
        industry = (row.get('industry') or '').strip() or None
        size = (row.get('company_size') or '').strip() or None
        out['industry'] = industry
        out['company_size'] = size

        # No industry on the company -> no peer cohort can be formed. We still
        # return the company itself as a (size-1) cohort so its OWN values render,
        # but no cohort stats will clear the density gate.
        if not industry:
            out['cohort_company_ids'] = [cid]
            out['cohort_size'] = 1
            return out

        # 2) resolve peer company_ids in the SAME industry (case-insensitive,
        # trimmed). Only count "real" active-ish tenants: any company row in the
        # industry. The requesting company is included via the OR so it always
        # counts toward k even if some status filter would otherwise drop it.
        cur.execute(
            """
            SELECT id
            FROM companies
            WHERE (LOWER(TRIM(industry)) = LOWER(TRIM(%s)) OR id = %s)
              AND industry IS NOT NULL AND TRIM(industry) <> ''
            """,
            (industry, cid),
        )
        ids = []
        for r in cur.fetchall() or []:
            rid = _int(r.get('id'), None) if isinstance(r, dict) else _int(r[0], None)
            if rid is not None:
                ids.append(rid)
        # de-dup, ensure the requester is present.
        idset = set(ids)
        idset.add(cid)
        out['cohort_company_ids'] = sorted(idset)
        out['cohort_size'] = len(idset)
    except Exception as e:
        logger.warning("cohort_for failed (company_id=%s): %s", company_id, e)
        # Fail safe: at minimum the company is its own (sub-k) cohort.
        try:
            out['cohort_company_ids'] = [int(company_id)]
            out['cohort_size'] = 1
        except Exception:
            pass
    finally:
        _close(cur)

    return out


# ---------------------------------------------------------------------------
# Per-company metric vectors
# ---------------------------------------------------------------------------
#
# Each metric is computed as ONE value PER company across the cohort. The
# requesting company's value is picked out of (or recomputed from) that same
# vector. The cohort aggregate is then mean/median over the OTHER-INCLUDED whole
# vector — but only ever the aggregate is returned, never the vector.
#
# Metrics:
#   spend_per_employee  — total course spend / active employees (DKK/medarbejder)
#   completion_rate     — % of course orders completed
#   courses_per_employee— completed courses / active employees
#   ai_adoption_rate    — % of active employees who have used the AI advisor
#   skill_gap_closure   — % of targeted skill level actually reached (avg over
#                         (employee-skill, target) pairs of min(cur/target,1)).
#                         Higher = gaps more closed. CANONICAL SCALE 1-5 (the same
#                         scale the HR matrix / company_skill_targets use), so a
#                         ratio is scale-consistent and never exceeds 100%.
#   budget_utilization  — % of the company's allocated learning budget actually
#                         spent (realised spend / annual budget across
#                         department_budgets). Higher = more of the allocated
#                         training investment is being used.
#   engagement_rate     — % of active employees with ANY learning activity in the
#                         last 30 days, on the unified last-activity signal
#                         (GREATEST over course_orders / learning progress /
#                         chatbot / last_active_at) — not the chatbot-only proxy.
#   avg_completion_days — avg days from course start to completion. LOWER is
#                         better (faster throughput), so this metric is
#                         direction-aware (higher_is_better=False).
# ---------------------------------------------------------------------------

# Order statuses that count as realised spend (an actual order, not cancelled).
_SPEND_STATUSES = ('pending', 'approved', 'completed', 'paid', 'confirmed', 'processing')


def _company_value_maps(cohort_ids):
    """Compute the per-company value for every metric, for every company in the
    cohort, in as few queries as possible.

    Returns a dict keyed by metric ->  {company_id: value}. Companies with no
    data simply get 0 (or are absent and treated as 0 by callers). Guarded;
    returns whatever it managed to compute, never raises.

    IMPORTANT: these per-company maps are INTERNAL ONLY. They are never returned
    to a caller / template — only the mean/median/percentile derived from them
    (and only when the cohort is k-safe) leave this module.
    """
    maps = {
        'spend_per_employee': {},
        'completion_rate': {},
        'courses_per_employee': {},
        'ai_adoption_rate': {},
        'skill_gap_closure': {},
        'budget_utilization': {},
        'engagement_rate': {},
        'avg_completion_days': {},
    }
    if not cohort_ids:
        return maps

    cur = _cursor()
    if cur is None:
        return maps

    try:
        ids = [int(c) for c in cohort_ids]
        if not ids:
            return maps
        placeholders = ', '.join(['%s'] * len(ids))

        # --- active employees per company (denominator for per-employee metrics)
        emp = {}
        cur.execute(
            """
            SELECT company_id, COUNT(*) AS n
            FROM company_users
            WHERE company_id IN ({ph}) AND status = 'active'
            GROUP BY company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            emp[_int(r.get('company_id'))] = _int(r.get('n'))

        # --- course_orders aggregates per company: spend, total, completed
        spend = {}
        orders_total = {}
        orders_completed = {}
        status_ph = ', '.join(['%s'] * len(_SPEND_STATUSES))
        cur.execute(
            """
            SELECT company_id,
                   COALESCE(SUM(CASE WHEN status IN ({sp}) THEN price END), 0) AS spend,
                   COUNT(*) AS total_orders,
                   COUNT(CASE WHEN completion_status = 'completed' THEN 1 END) AS completed_orders
            FROM course_orders
            WHERE company_id IN ({ph})
            GROUP BY company_id
            """.format(sp=status_ph, ph=placeholders),
            _SPEND_STATUSES + tuple(ids),
        )
        for r in cur.fetchall() or []:
            c = _int(r.get('company_id'))
            spend[c] = _float(r.get('spend'))
            orders_total[c] = _int(r.get('total_orders'))
            orders_completed[c] = _int(r.get('completed_orders'))

        # --- completed courses per company (from learning progress, the canonical
        # completion signal), fallback-friendly: count completed learning rows.
        completed_courses = {}
        cur.execute(
            """
            SELECT company_id,
                   COUNT(CASE WHEN status = 'completed' THEN 1 END) AS completed
            FROM employee_learning_progress
            WHERE company_id IN ({ph})
            GROUP BY company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            completed_courses[_int(r.get('company_id'))] = _int(r.get('completed'))

        # --- AI-advisor adoption: distinct employees who used the chatbot
        ai_users = {}
        cur.execute(
            """
            SELECT company_id,
                   COUNT(DISTINCT COALESCE(username, session_id)) AS users
            FROM chatbot_interactions
            WHERE company_id IN ({ph})
              AND (username IS NOT NULL AND username <> '' OR session_id IS NOT NULL)
            GROUP BY company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            ai_users[_int(r.get('company_id'))] = _int(r.get('users'))

        # --- skill-gap closure: per company, the avg over (employee-skill,
        # target) pairs of min(current_level / target_level, 1). The whole
        # rollup happens in SQL so only ONE scalar per company ever leaves the
        # DB — never a per-employee skill row. We join employee_skills_matrix to
        # the company's targets (department-specific OR global '') on skill_name,
        # bound target_level > 0 to avoid divide-by-zero, and LEAST(...,1) so an
        # over-target employee never inflates the rate above 100%. CANONICAL
        # SCALE 1-5: both sides are on the company_skill_targets / HR-matrix 1-5
        # scale, so the ratio is scale-consistent.
        gap_closure = {}
        cur.execute(
            """
            SELECT esm.company_id AS company_id,
                   AVG(LEAST(esm.current_level / cst.target_level, 1.0)) * 100.0 AS closure
            FROM employee_skills_matrix esm
            JOIN company_skill_targets cst
              ON cst.company_id = esm.company_id
             AND LOWER(TRIM(cst.skill_name)) = LOWER(TRIM(esm.skill_name))
            JOIN company_users cu
              ON cu.user_id = esm.employee_id
             AND cu.company_id = esm.company_id
             AND cu.status = 'active'
            WHERE esm.company_id IN ({ph})
              AND cst.target_level > 0
              AND (cst.department = '' OR cst.department IS NULL
                   OR LOWER(TRIM(cst.department)) = LOWER(TRIM(COALESCE(cu.department, ''))))
            GROUP BY esm.company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            gap_closure[_int(r.get('company_id'))] = _float(r.get('closure'))

        # --- budget utilization: realised spend / allocated annual budget,
        # rolled up to ONE scalar per company across department_budgets. We use
        # the budget table's own ``spent`` against ``annual_budget`` so the
        # number reflects HR's own ledger; companies with no budget set are
        # absent and treated as 0 (no allocation -> no utilisation signal).
        budget_util = {}
        cur.execute(
            """
            SELECT company_id,
                   COALESCE(SUM(annual_budget), 0) AS budget,
                   COALESCE(SUM(spent), 0) AS spent
            FROM department_budgets
            WHERE company_id IN ({ph})
            GROUP BY company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            c = _int(r.get('company_id'))
            total_budget = _float(r.get('budget'))
            total_spent = _float(r.get('spent'))
            if total_budget > 0:
                # Cap at 200% so a single wildly-over-budget peer can't dominate
                # the cohort mean; over-budget is still distinguishable from
                # under-budget but the tail is tamed for a fair benchmark.
                budget_util[c] = round(min(total_spent / total_budget * 100.0, 200.0), 1)
            else:
                budget_util[c] = 0.0

        # --- engagement: % of active employees with ANY learning activity in the
        # last 30 days, on the UNIFIED last-activity signal (course order,
        # learning-progress access, chatbot, or last_active_at) — NOT the
        # chatbot-only proxy. One scalar per company: a per-company COUNT of
        # active employees whose most-recent activity across all four sources is
        # within 30 days, over the active-employee denominator computed above.
        engaged = {}
        cur.execute(
            """
            SELECT cu.company_id AS company_id,
                   COUNT(*) AS engaged
            FROM company_users cu
            WHERE cu.company_id IN ({ph}) AND cu.status = 'active'
              AND GREATEST(
                    COALESCE(cu.last_active_at, '1970-01-01'),
                    COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                    COALESCE((SELECT MAX(co.created_at) FROM course_orders co
                              WHERE co.company_id = cu.company_id AND co.user_id = cu.user_id),
                             '1970-01-01'),
                    COALESCE((SELECT MAX(elp.last_accessed) FROM employee_learning_progress elp
                              WHERE elp.company_id = cu.company_id AND elp.user_id = cu.user_id),
                             '1970-01-01')
                  ) >= (NOW() - INTERVAL 30 DAY)
            GROUP BY cu.company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            engaged[_int(r.get('company_id'))] = _int(r.get('engaged'))

        # --- avg completion days: avg days from course start (or order creation
        # when started_at is missing) to completion, for completed orders only.
        # One scalar per company. LOWER is better (handled direction-aware).
        completion_days = {}
        cur.execute(
            """
            SELECT company_id,
                   AVG(DATEDIFF(completion_date, COALESCE(started_at, created_at))) AS days
            FROM course_orders
            WHERE company_id IN ({ph})
              AND completion_status = 'completed'
              AND completion_date IS NOT NULL
              AND COALESCE(started_at, created_at) IS NOT NULL
              AND DATEDIFF(completion_date, COALESCE(started_at, created_at)) >= 0
            GROUP BY company_id
            """.format(ph=placeholders),
            tuple(ids),
        )
        for r in cur.fetchall() or []:
            c = _int(r.get('company_id'))
            d = r.get('days')
            if d is not None:
                completion_days[c] = round(_float(d), 1)

        # --- assemble per-company values ---
        for c in ids:
            n_emp = emp.get(c, 0)

            # spend per employee (DKK / employee); 0 when no employees.
            if n_emp > 0:
                maps['spend_per_employee'][c] = round(spend.get(c, 0.0) / n_emp, 2)
            else:
                maps['spend_per_employee'][c] = 0.0

            # completion rate (% of orders completed); 0 when no orders.
            tot = orders_total.get(c, 0)
            if tot > 0:
                maps['completion_rate'][c] = round(
                    orders_completed.get(c, 0) / tot * 100.0, 1)
            else:
                maps['completion_rate'][c] = 0.0

            # courses completed per employee; 0 when no employees.
            if n_emp > 0:
                maps['courses_per_employee'][c] = round(
                    completed_courses.get(c, 0) / n_emp, 2)
            else:
                maps['courses_per_employee'][c] = 0.0

            # AI-advisor adoption (% of employees who used the advisor); capped at
            # 100 because distinct chatbot identities can exceed the active-roster
            # count (ex-employees, anon sessions).
            if n_emp > 0:
                rate = ai_users.get(c, 0) / n_emp * 100.0
                maps['ai_adoption_rate'][c] = round(min(rate, 100.0), 1)
            else:
                maps['ai_adoption_rate'][c] = 0.0

            # skill-gap closure (% of targeted level reached); already a SQL
            # scalar in [0,100], 0 when the company has no targets+skills.
            maps['skill_gap_closure'][c] = round(gap_closure.get(c, 0.0), 1)

            # budget utilization (% of allocated learning budget spent); already
            # computed/capped above, 0 when no budget is set.
            maps['budget_utilization'][c] = budget_util.get(c, 0.0)

            # engagement (% of active employees active in the last 30 days);
            # 0 when no employees.
            if n_emp > 0:
                e_rate = engaged.get(c, 0) / n_emp * 100.0
                maps['engagement_rate'][c] = round(min(e_rate, 100.0), 1)
            else:
                maps['engagement_rate'][c] = 0.0

            # avg completion days (lower is better); 0.0 when no completed orders
            # (treated as "no signal", a real 0 the percentile handles).
            maps['avg_completion_days'][c] = completion_days.get(c, 0.0)

    except Exception as e:
        logger.warning("_company_value_maps failed: %s", e)
    finally:
        _close(cur)

    return maps


# ---------------------------------------------------------------------------
# Aggregate stats over a per-company vector (k-gated by the CALLER)
# ---------------------------------------------------------------------------

def _mean(values):
    try:
        return sum(values) / len(values) if values else 0.0
    except Exception:
        return 0.0


def _median(values):
    try:
        if not values:
            return 0.0
        s = sorted(values)
        n = len(s)
        mid = n // 2
        if n % 2:
            return float(s[mid])
        return (s[mid - 1] + s[mid]) / 2.0
    except Exception:
        return 0.0


def _percentile_of(your_value, values, higher_is_better=True):
    """Percentile RANK of ``your_value`` within ``values`` (0..100): the share
    of cohort companies you rank at-or-above. Uses the full cohort vector (which
    already includes the requesting company), so it never leaks an individual
    peer. Returns None on bad input.

    Direction-aware: when ``higher_is_better`` (the default) the rank is the
    share of companies whose value is <= yours (more is better). When
    ``higher_is_better`` is False (e.g. avg completion days) the rank is the
    share of companies whose value is >= yours, so a LOWER value ranks HIGHER —
    keeping "higher percentile = better" consistent for the viz."""
    try:
        if not values:
            return None
        yv = float(your_value)
        if higher_is_better:
            better_or_equal = sum(1 for v in values if float(v) <= yv)
        else:
            better_or_equal = sum(1 for v in values if float(v) >= yv)
        return round(better_or_equal / len(values) * 100.0, 0)
    except Exception:
        return None


# Metric metadata: key -> (Danish label, unit suffix, value type, higher_is_better).
# ``higher_is_better`` makes the percentile rank direction-aware: for a metric
# where LOWER is better (avg_completion_days) a low value should rank HIGH, so we
# invert the rank. All other metrics are "more is better".
_METRICS = [
    ('spend_per_employee', 'Træningsforbrug pr. medarbejder', 'kr', 'money', True),
    ('completion_rate', 'Gennemførselsrate for kurser', '%', 'pct', True),
    ('courses_per_employee', 'Gennemførte kurser pr. medarbejder', '', 'num', True),
    ('ai_adoption_rate', 'Brug af AI-rådgiver', '%', 'pct', True),
    ('skill_gap_closure', 'Lukning af kompetencegab', '%', 'pct', True),
    ('budget_utilization', 'Udnyttelse af uddannelsesbudget', '%', 'pct', True),
    ('engagement_rate', 'Aktive medarbejdere (30 dage)', '%', 'pct', True),
    ('avg_completion_days', 'Gns. dage til gennemførsel', 'dage', 'num', False),
]


def benchmark(company_id):
    """Build the full benchmarking payload for ``company_id``.

    For each metric we return THIS company's own value plus — ONLY when the
    cohort is k-safe — the cohort mean, median and the company's percentile
    position computed across the peer companies. When the cohort is below k, the
    metric carries the company's own value but ``cohort_avg / cohort_median /
    your_percentile`` are ``None`` and ``safe`` is ``False`` with the Danish
    "too few companies" note. No per-company breakdown is ever included.

    Returns a JSON-serialisable dict (never raises)::

        {
          'industry': str|None,
          'company_size': str|None,
          'cohort_size': int,            # distinct companies (incl. self)
          'k': int,
          'safe': bool,                  # whole-cohort density verdict
          'metrics': [
             {'key','label','unit','higher_is_better':bool,'your_value',
              'cohort_avg'|None,'cohort_median'|None,'your_percentile'|None,
              'safe':bool,'note':str},
             ...
          ],
          'overall_note': str,
        }
    """
    k = _k()
    out = {
        'industry': None,
        'company_size': None,
        'cohort_size': 0,
        'k': k,
        'safe': False,
        'metrics': [],
        'overall_note': '',
    }

    try:
        coh = cohort_for(company_id)
        out['industry'] = coh.get('industry')
        out['company_size'] = coh.get('company_size')
        cohort_ids = coh.get('cohort_company_ids') or []
        cohort_size = coh.get('cohort_size') or len(cohort_ids)
        out['cohort_size'] = cohort_size

        # The single density verdict for the whole cohort. Because every metric
        # is aggregated over the SAME set of companies, one verdict governs all
        # of them: if there are < k companies, NO metric may show a cohort stat.
        cohort_is_safe = _cohort_safe(cohort_size)
        out['safe'] = bool(cohort_is_safe)

        # Per-company value maps (internal only).
        maps = _company_value_maps(cohort_ids)

        try:
            cid = int(company_id) if company_id is not None else None
        except Exception:
            cid = None

        for key, label, unit, _vtype, higher_is_better in _METRICS:
            vmap = maps.get(key, {})
            your_value = vmap.get(cid, 0.0) if cid is not None else 0.0

            metric = {
                'key': key,
                'label': label,
                'unit': unit,
                'higher_is_better': higher_is_better,
                'your_value': your_value,
                'cohort_avg': None,
                'cohort_median': None,
                'your_percentile': None,
                'safe': False,
                'note': '',
            }

            # DENSITY GATE: only ever compute a cohort stat when the cohort is
            # k-safe. A 1- or 4-company cohort takes the else branch and yields
            # NO cohort numbers — period.
            if cohort_is_safe:
                # Full per-company vector for this metric (includes self). We use
                # the cohort id list as the source of truth so a company missing
                # from the map still counts as 0 (a real value, not absence).
                values = [vmap.get(c, 0.0) for c in cohort_ids]
                # Defensive: only aggregate when we genuinely have >= k values.
                if _cohort_safe(len(values)):
                    metric['cohort_avg'] = _round(_mean(values), 1)
                    metric['cohort_median'] = _round(_median(values), 1)
                    metric['your_percentile'] = _percentile_of(
                        your_value, values, higher_is_better)
                    metric['safe'] = True
                    metric['note'] = ''
                else:
                    metric['note'] = _too_few_note()
            else:
                metric['note'] = _too_few_note()

            out['metrics'].append(metric)

        # Overall note.
        if not coh.get('industry'):
            out['overall_note'] = (
                'Din virksomhed har ingen branche registreret endnu, så vi kan '
                'ikke finde sammenlignelige virksomheder. Tilføj en branche under '
                'virksomhedsindstillinger for at låse op for benchmarking.'
            )
        elif cohort_is_safe:
            out['overall_note'] = (
                'Sammenligningstallene er anonyme gennemsnit på tværs af {n} '
                'virksomheder i branchen "{ind}". Ingen enkelt virksomheds tal '
                'vises nogensinde.'.format(n=cohort_size, ind=coh.get('industry'))
            )
        else:
            out['overall_note'] = _too_few_note()

    except Exception as e:
        logger.warning("benchmark failed (company_id=%s): %s", company_id, e)
        # Safe empty state: render the cards with own-value-only / locked cohort.
        if not out['metrics']:
            for key, label, unit, _vtype, higher_is_better in _METRICS:
                out['metrics'].append({
                    'key': key, 'label': label, 'unit': unit,
                    'higher_is_better': higher_is_better,
                    'your_value': 0.0, 'cohort_avg': None, 'cohort_median': None,
                    'your_percentile': None, 'safe': False,
                    'note': _too_few_note(),
                })
        if not out['overall_note']:
            out['overall_note'] = _too_few_note()

    return out
