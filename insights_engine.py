"""
Phase 3.2 & 3.4: AI-powered conversation insights & predictive analytics.
Generates actionable intelligence from chatbot_interactions for HR dashboards.
"""
import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# k-anonymity helpers (roadmap value-4, Theme A/E). Guarded: if the module is
# missing the analytics still compute — just without small-cohort suppression.
# A missing k-anon import must NEVER crash an insights/ROI call.
try:
    import kanon as _kanon
except Exception:  # pragma: no cover - boot-safety guard
    _kanon = None


def generate_company_insights(app, company_id):
    """
    Analyze recent chatbot interactions for a company and generate insights.
    Stores results in company_insights table.
    Called on-demand from HR dashboard or by a scheduled job.
    """
    import MySQLdb.cursors

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        # Gather recent data (last 30 days)
        cur.execute("""
            SELECT ci.query_text, ci.query_type, ci.tools_used, ci.products_shown,
                   ci.feedback_rating, ci.conversation_depth, ci.response_time_ms,
                   ci.created_at, cu.department
            FROM chatbot_interactions ci
            JOIN users u ON ci.username = u.username
            JOIN company_users cu ON u.id = cu.user_id AND cu.company_id = %s
            WHERE ci.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            ORDER BY ci.created_at DESC
            LIMIT 500
        """, (company_id,))
        interactions = cur.fetchall()

        if len(interactions) < 5:
            cur.close()
            return []

        insights = []

        # --- 1. Trending topics by department ---
        dept_topics = {}
        dept_users = {}
        for i in interactions:
            dept = i.get('department') or 'Unknown'
            qt = i.get('query_type') or 'general'
            if dept not in dept_topics:
                dept_topics[dept] = {}
                dept_users[dept] = set()
            dept_topics[dept][qt] = dept_topics[dept].get(qt, 0) + 1
            # Track distinct people behind each department so a single-employee
            # department isn't surfaced as a "trending" cohort. interactions rows
            # don't carry username here, so fall back to session/depth as a
            # cohort proxy where present.
            uid = i.get('username') or i.get('user_id') or i.get('session_id')
            if uid is not None:
                dept_users[dept].add(uid)

        # k-anonymity: only surface a department's trend if the department is a
        # cohort of at least k people. When we cannot resolve distinct users
        # (no per-row identity), fall back to the interaction volume as a
        # conservative proxy. Guarded — without k-anon, behaviour is unchanged.
        _kk = None
        if _kanon is not None:
            try:
                _kk = _kanon.K_DEFAULT
            except Exception:
                _kk = None

        for dept, topics in dept_topics.items():
            if not topics:
                continue
            if _kk is not None:
                distinct = len(dept_users.get(dept) or ())
                cohort = distinct if distinct > 0 else sum(topics.values())
                if cohort < _kk:
                    # Sub-k department — skip to avoid profiling an individual.
                    continue
            top_topic = max(topics, key=topics.get)
            count = topics[top_topic]
            if count >= 3:
                insights.append({
                    'type': 'trending_topic',
                    'severity': 'info',
                    'title': f'{dept}: Stigende interesse for "{top_topic}"',
                    'body': f'{dept}-afdelingen har haft {count} interaktioner om "{top_topic}" de sidste 30 dage.',
                    'data': {'department': dept, 'topic': top_topic, 'count': count},
                })

        # --- 2. Low satisfaction alerts ---
        low_fb = [i for i in interactions if i.get('feedback_rating') and i['feedback_rating'] > 0 and i['feedback_rating'] <= 2]
        if len(low_fb) >= 3:
            insights.append({
                'type': 'low_satisfaction',
                'severity': 'warning',
                'title': f'{len(low_fb)} interaktioner med lav tilfredshed',
                'body': 'Flere medarbejdere har givet lav feedback. Overvej at gennemgå chatbot-svarene.',
                'data': {'count': len(low_fb)},
            })

        # --- 3. Unanswered needs (searches with no orders) ---
        search_interactions = [i for i in interactions if i.get('tools_used') and 'search_courses' in i['tools_used']]
        orders_count = 0
        try:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM course_orders
                WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            """, (company_id,))
            orders_count = cur.fetchone()['cnt'] or 0
        except Exception:
            pass

        if len(search_interactions) > 10 and orders_count == 0:
            insights.append({
                'type': 'no_conversions',
                'severity': 'warning',
                'title': 'Mange soegninger, ingen ordrer',
                'body': f'{len(search_interactions)} kursussoegninger de sidste 30 dage, men ingen ordrer. '
                        'Medarbejderne finder maaske ikke det rette.',
                'data': {'searches': len(search_interactions), 'orders': orders_count},
            })

        # --- 4. Engagement drop detection ---
        recent_7 = [i for i in interactions if i['created_at'] and i['created_at'] >= datetime.now() - timedelta(days=7)]
        prev_7 = [i for i in interactions
                   if i['created_at'] and datetime.now() - timedelta(days=14) <= i['created_at'] < datetime.now() - timedelta(days=7)]
        if len(prev_7) > 5 and len(recent_7) < len(prev_7) * 0.5:
            insights.append({
                'type': 'engagement_drop',
                'severity': 'warning',
                'title': 'Fald i chatbot-brug',
                'body': f'Brug er faldet fra {len(prev_7)} til {len(recent_7)} interaktioner (uge-over-uge).',
                'data': {'prev_week': len(prev_7), 'this_week': len(recent_7)},
            })

        # --- 5. Popular products not ordered ---
        all_shown = []
        for i in interactions:
            if i.get('products_shown'):
                try:
                    handles = json.loads(i['products_shown'])
                    all_shown.extend(handles)
                except Exception:
                    pass
        if all_shown:
            from collections import Counter
            shown_counts = Counter(all_shown).most_common(3)
            for handle, cnt in shown_counts:
                if cnt >= 5:
                    # Check if any orders for this product
                    try:
                        cur.execute("""
                            SELECT COUNT(*) AS cnt FROM course_orders
                            WHERE company_id = %s AND product_handle = %s
                        """, (company_id, handle))
                        ordered = cur.fetchone()['cnt'] or 0
                    except Exception:
                        ordered = 0
                    if ordered == 0:
                        insights.append({
                            'type': 'popular_unordered',
                            'severity': 'info',
                            'title': f'Populaert kursus uden ordrer: {handle}',
                            'body': f'"{handle}" er vist {cnt} gange, men ingen har bestilt det endnu.',
                            'data': {'handle': handle, 'shown': cnt, 'ordered': 0},
                        })

        # --- 6. Try GPT summary if API key available ---
        openai_key = os.environ.get('OPENAI_API_KEY')
        if openai_key and len(interactions) >= 10:
            try:
                import openai
                sample = interactions[:30]
                queries_text = "\n".join(
                    f"- [{i.get('department','?')}] {i.get('query_type','?')}: {(i.get('query_text') or '')[:100]}"
                    for i in sample
                )
                client = openai.OpenAI(api_key=openai_key)
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": (
                            "Du er en HR-analyseassistent. Analyser disse chatbot-interaktioner og giv "
                            "2-3 korte, handlingsrettede indsigter paa dansk. Max 150 ord total."
                        )},
                        {"role": "user", "content": f"Seneste 30 chatbot-interaktioner:\n{queries_text}"}
                    ],
                    max_tokens=300,
                    temperature=0.3,
                )
                ai_summary = resp.choices[0].message.content.strip()
                if ai_summary:
                    insights.append({
                        'type': 'ai_summary',
                        'severity': 'info',
                        'title': 'AI-genereret indsigt',
                        'body': ai_summary,
                        'data': {'interactions_analyzed': len(sample)},
                    })
            except Exception as e:
                logger.warning(f"AI insight generation failed: {e}")

        # Store insights
        for ins in insights:
            try:
                cur.execute("""
                    INSERT INTO company_insights
                    (company_id, insight_type, title, body, data, severity, generated_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW(), DATE_ADD(NOW(), INTERVAL 7 DAY))
                """, (
                    company_id, ins['type'], ins['title'], ins['body'],
                    json.dumps(ins.get('data', {})), ins['severity']
                ))
            except Exception as e:
                logger.warning(f"Failed to store insight: {e}")

        # C5: surface new high-signal insights as an in-app notification card for
        # HR/admins (same company_notifications shape the dashboard reads). Guarded.
        try:
            alerts = [i for i in insights if i.get('severity') in ('warning', 'critical', 'danger')]
            if alerts:
                top = alerts[0]
                title = "Ny AI-indsigt" + (f" (+{len(alerts) - 1} mere)" if len(alerts) > 1 else "")
                urgent = 1 if any(i.get('severity') in ('critical', 'danger') for i in alerts) else 0
                cur.execute(
                    """INSERT INTO company_notifications
                           (company_id, recipient_user_id, sender_user_id, target_roles,
                            title, message, is_urgent, is_read)
                       VALUES (%s, NULL, NULL, %s, %s, %s, %s, 0)""",
                    (company_id, '["company_admin","hr_manager"]', title[:255],
                     f"{top.get('title', '')} — {top.get('body', '')}", urgent),
                )
        except Exception as e:
            logger.warning(f"Insight notification skipped: {e}")

        conn.commit()
        cur.close()
        return insights


def get_skill_gap_analysis(app, company_id, department=None):
    """
    Phase 3.1: Compare employee skills against company skill targets.
    Returns heatmap data: {department: {skill: {target, avg_current, gap, employees}}}
    """
    import MySQLdb.cursors

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        # Get targets
        if department:
            cur.execute("""
                SELECT * FROM company_skill_targets
                WHERE company_id = %s AND (department = %s OR department IS NULL OR department = '')
            """, (company_id, department))
        else:
            cur.execute("SELECT * FROM company_skill_targets WHERE company_id = %s", (company_id,))
        targets = cur.fetchall()

        if not targets:
            cur.close()
            return None

        # Get current employee skills (merge enterprise + chatbot sources)
        # CANONICAL SCALE 1-5: map the chatbot 4-label enum onto the same 1-5
        # scale the HR matrix/targets and the viz use, so a chatbot "ekspert"
        # reaches 5 and gaps cannot flip sign against a 5/5 target.
        level_map = "CASE us.skill_level WHEN 'begynder' THEN 1 WHEN 'mellem' THEN 2 WHEN 'avanceret' THEN 4 WHEN 'ekspert' THEN 5 ELSE 2 END"
        cur.execute(f"""
            SELECT skill_name, current_level, department, user_id FROM (
                SELECT esm.skill_name, esm.current_level, cu.department, cu.user_id
                FROM employee_skills_matrix esm
                JOIN company_users cu ON esm.employee_id = cu.user_id AND esm.company_id = cu.company_id
                WHERE esm.company_id = %s AND cu.status = 'active'
                UNION ALL
                SELECT us.skill_name, {level_map} as current_level, cu2.department, cu2.user_id
                FROM user_skills us
                JOIN users u ON us.username = u.username
                JOIN company_users cu2 ON cu2.user_id = u.id AND cu2.company_id = %s AND cu2.status = 'active'
                WHERE NOT EXISTS (
                    SELECT 1 FROM employee_skills_matrix esm2
                    WHERE esm2.employee_id = cu2.user_id
                      AND esm2.company_id = %s
                      AND esm2.skill_name = us.skill_name
                )
            ) combined
        """, (company_id, company_id, company_id))
        skills = cur.fetchall()
        cur.close()

        # Build heatmap
        heatmap = {}
        for t in targets:
            dept = t['department'] or 'All'
            skill = t['skill_name']
            if dept not in heatmap:
                heatmap[dept] = {}
            heatmap[dept][skill] = {
                'target_level': t['target_level'],
                'priority': t['priority'],
                'employees': 0,
                'total_current': 0,
                'avg_current': 0,
                'gap': 0,
                'status': 'unknown',
            }

        for s in skills:
            dept = s['department'] or 'All'
            skill = s['skill_name']
            # Match against department-specific or global targets
            for target_dept in [dept, 'All']:
                if target_dept in heatmap and skill in heatmap[target_dept]:
                    entry = heatmap[target_dept][skill]
                    entry['employees'] += 1
                    entry['total_current'] += (s['current_level'] or 0)

        # Calculate averages and gap status
        for dept in heatmap:
            for skill in heatmap[dept]:
                e = heatmap[dept][skill]
                if e['employees'] > 0:
                    e['avg_current'] = round(e['total_current'] / e['employees'], 1)
                e['gap'] = e['target_level'] - e['avg_current']
                if e['gap'] <= 0:
                    e['status'] = 'green'
                elif e['gap'] <= 1:
                    e['status'] = 'yellow'
                else:
                    e['status'] = 'red'

        # k-anonymity: a department whose largest measured skill cohort is below
        # k represents only a handful of (possibly one) employees, so its skill
        # heatmap would profile that individual. Suppress those departments. The
        # 'All' bucket is a cross-department aggregate and is left intact when it
        # is itself k-safe. Guarded — without k-anon the heatmap is unchanged.
        if _kanon is not None and heatmap:
            try:
                dept_rows = [
                    {'department': dept,
                     '_cohort': max((s.get('employees', 0) for s in skills.values()),
                                    default=0)}
                    for dept, skills in heatmap.items()
                ]
                kept, note = _kanon.suppress_small_groups(dept_rows, '_cohort')
                safe_depts = {r['department'] for r in kept if isinstance(r, dict)}
                if note and note.get('suppressed'):
                    for dept in list(heatmap.keys()):
                        if dept not in safe_depts:
                            heatmap.pop(dept, None)
                    heatmap['_anon_note'] = note.get('note_da')
            except Exception:
                pass

        return heatmap


# ---------------------------------------------------------------------------
# Skill-uplift loop READ side (plan #19).
#
# The write side (skill_history.record_snapshot) appends an immutable row to
# employee_skill_history on every actual skill-level change. These two readers
# turn that trail into the buyer-facing evidence the product promises:
#
#   * get_skill_growth_trend — company-wide AVERAGE measured skill level per
#     month (a line chart on skill_gaps.html). Proves the level moved over time.
#   * get_uplift_roi         — measured aggregate level LIFT vs training spend
#     (avg level lift per 1000 kr, a headline on roi.html). Upgrades ROI from
#     cost/throughput to MEASURED outcome.
#
# BOTH are aggregate, company-scoped, and k-anon-safe: they emit only
# company-level numbers and SUPPRESS any bucket whose distinct-employee cohort
# is below kanon.K_DEFAULT (so a single learner's trajectory can never be
# re-identified from a month's average or the lift figure). They NEVER return
# people-level rows — that is the GDPR/k-anon bar for people-level analytics.
# ---------------------------------------------------------------------------

def _k_default():
    """Active k floor (kanon.K_DEFAULT), or a safe 5 if k-anon is unavailable."""
    try:
        return int(_kanon.K_DEFAULT) if _kanon is not None else 5
    except Exception:
        return 5


def get_skill_growth_trend(app, company_id, months=6):
    """Company-wide monthly average measured skill level (k-anon-safe).

    Reads employee_skill_history (the append-only trajectory) and, for each of
    the last ``months`` calendar months, computes the AVERAGE level of the
    latest snapshot per (employee, skill) recorded in that month — i.e. "where
    did the measured skill levels sit that month". A rising line is direct
    evidence that training moved levels upward.

    k-anonymity: a month whose distinct-employee count is below k would expose a
    handful of (possibly one) people's levels, so that month is SUPPRESSED from
    the series (its average is dropped). The series only ever carries
    company-level aggregates. Returns a safe-empty dict on no data / error.

    Returns::

        {'labels': ['2026-01', ...],        # months with a k-safe cohort
         'avg_levels': [3.2, ...],          # company avg level that month
         'employee_counts': [12, ...],      # distinct employees that month
         'months': <int>, 'k': <int>,
         'suppressed_months': <int>, 'anon_note': <str|None>,
         'has_data': <bool>}
    """
    import MySQLdb.cursors

    try:
        months = int(months)
    except (TypeError, ValueError):
        months = 6
    if months < 1:
        months = 1
    if months > 36:
        months = 36

    k = _k_default()
    out = {'labels': [], 'avg_levels': [], 'employee_counts': [],
           'months': months, 'k': k, 'suppressed_months': 0,
           'anon_note': None, 'has_data': False}

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        try:
            # One pass over the company's history within the window. AVG over the
            # rows in each month, plus the DISTINCT-employee cohort that drives
            # the k-anon decision. month label is 'YYYY-MM'.
            cur.execute("""
                SELECT DATE_FORMAT(captured_at, '%%Y-%%m')      AS ym,
                       AVG(level)                               AS avg_level,
                       COUNT(DISTINCT employee_id)              AS employees
                FROM employee_skill_history
                WHERE company_id = %s
                  AND captured_at >= DATE_SUB(
                        DATE_FORMAT(CURDATE(), '%%Y-%%m-01'),
                        INTERVAL %s MONTH)
                GROUP BY ym
                ORDER BY ym ASC
            """, (company_id, months - 1))
            rows = cur.fetchall() or []
        except Exception as e:
            logger.warning(f"skill growth trend error: {e}")
            rows = []
        finally:
            try:
                cur.close()
            except Exception:
                pass

    suppressed = 0
    for r in rows:
        try:
            n = int(r.get('employees') or 0)
        except (TypeError, ValueError):
            n = 0
        if n < k:
            # Sub-k month: suppress entirely (never reveal its average).
            suppressed += 1
            continue
        try:
            avg = round(float(r.get('avg_level') or 0), 2)
        except (TypeError, ValueError):
            avg = 0.0
        out['labels'].append(r.get('ym'))
        out['avg_levels'].append(avg)
        out['employee_counts'].append(n)

    out['suppressed_months'] = suppressed
    out['has_data'] = bool(out['labels'])
    if suppressed and _kanon is not None:
        try:
            out['anon_note'] = _kanon.anon_note(k)
        except Exception:
            out['anon_note'] = None
    return out


def get_uplift_roi(app, company_id, fiscal_year=None):
    """Measured aggregate skill LIFT vs training spend for a fiscal year.

    The skill-uplift headline for roi.html: instead of spend+completion only,
    this quantifies how much measured skill level the year's training BOUGHT.

      * ``total_lift``  = SUM of positive (level - previous_level) deltas across
        every employee_skill_history row captured in the fiscal year (only
        increases count — a manager correcting a level downward is not "uplift").
      * ``spend``       = the same real SUM(course_orders.price) the ROI engine
        uses for the year.
      * ``lift_per_1000_kr`` = total_lift / (spend / 1000) — "skill points gained
        per 1.000 kr invested", the measured-outcome metric.

    k-anonymity: the lift figure is built from at least ``employees`` distinct
    people; if that distinct-employee cohort is below k the aggregate is
    SUPPRESSED (a single learner's lift would be re-identifying). Aggregate,
    company-scoped, never people-level. Safe-zero on no data / error.

    Returns::

        {'fiscal_year': <int>, 'total_lift': <float>, 'levels_raised': <int>,
         'employees': <int>, 'skills': <int>, 'spend': <float>,
         'lift_per_1000_kr': <float>, 'avg_lift_per_employee': <float>,
         'suppressed': <bool>, 'anon_note': <str|None>, 'has_data': <bool>, 'k': <int>}
    """
    import MySQLdb.cursors
    import datetime as _dt

    if fiscal_year is None:
        try:
            fiscal_year = _dt.datetime.now().year
        except Exception:
            fiscal_year = 0

    k = _k_default()
    out = {'fiscal_year': fiscal_year, 'total_lift': 0.0, 'levels_raised': 0,
           'employees': 0, 'skills': 0, 'spend': 0.0, 'lift_per_1000_kr': 0.0,
           'avg_lift_per_employee': 0.0, 'suppressed': False,
           'anon_note': None, 'has_data': False, 'k': k}

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        try:
            # Aggregate measured lift for the year. Only POSITIVE deltas (an
            # actual increase) count toward uplift; previous_level NULL means the
            # first observation — no measurable delta yet, so it is excluded.
            cur.execute("""
                SELECT
                    COALESCE(SUM(GREATEST(level - previous_level, 0)), 0) AS total_lift,
                    SUM(CASE WHEN level > previous_level THEN 1 ELSE 0 END) AS levels_raised,
                    COUNT(DISTINCT CASE WHEN level > previous_level THEN employee_id END) AS employees,
                    COUNT(DISTINCT CASE WHEN level > previous_level THEN skill_name END) AS skills
                FROM employee_skill_history
                WHERE company_id = %s
                  AND previous_level IS NOT NULL
                  AND YEAR(captured_at) = %s
            """, (company_id, fiscal_year))
            row = cur.fetchone() or {}

            cur.execute("""
                SELECT COALESCE(SUM(price), 0) AS spend
                FROM course_orders
                WHERE company_id = %s
                  AND status NOT IN ('cancelled', 'rejected')
                  AND YEAR(created_at) = %s
            """, (company_id, fiscal_year))
            srow = cur.fetchone() or {}
        except Exception as e:
            logger.warning(f"uplift ROI error: {e}")
            row, srow = {}, {}
        finally:
            try:
                cur.close()
            except Exception:
                pass

    employees = int(row.get('employees') or 0)
    out['levels_raised'] = int(row.get('levels_raised') or 0)
    out['skills'] = int(row.get('skills') or 0)
    out['employees'] = employees
    out['spend'] = float(srow.get('spend') or 0)

    # k-anon: the measured lift rests on `employees` distinct people. Below k,
    # the aggregate could re-identify a single learner's progress — suppress it.
    if 0 < employees < k:
        out['suppressed'] = True
        if _kanon is not None:
            try:
                out['anon_note'] = _kanon.anon_note(k)
            except Exception:
                out['anon_note'] = None
        return out

    out['total_lift'] = round(float(row.get('total_lift') or 0), 1)
    if out['spend'] > 0 and out['total_lift'] > 0:
        out['lift_per_1000_kr'] = round(out['total_lift'] / (out['spend'] / 1000), 2)
    if employees > 0 and out['total_lift'] > 0:
        out['avg_lift_per_employee'] = round(out['total_lift'] / employees, 2)
    out['has_data'] = out['total_lift'] > 0 or out['levels_raised'] > 0
    return out


# ---------------------------------------------------------------------------
# Canonical ROI engine (roadmap value-4, Theme D/E).
#
# This is the SINGLE source of truth for training-ROI numbers. The HEADLINE /
# CFO figures are GROUNDED in real data only:
#   - total training investment  = SUM(course_orders.price), scoped by company_id
#                                   + fiscal period (the order's created_at year)
#   - completion counts / rates  = course_orders.completion_status
#   - cost per completion        = real spend / real completions
#   - budget utilisation         = department_budgets.spent vs .annual_budget
#   - budget OVERRUN             = departments where spent > annual_budget
#
# The fabricated productivity multiplier (0.15 * rating) NEVER touches any
# headline number. It lives ONLY inside metrics['scenario'] — a clearly
# labelled, explicitly hypothetical projection that the template renders in a
# separate "Estimeret scenarie — ikke faktiske tal" panel.
#
# enterprise_analytics.calculate_learning_roi delegates here, so there is one
# computation and the two engines can never diverge.
# ---------------------------------------------------------------------------

# Hypothetical-only assumption: productivity gain per performance-rating point
# above the 3.0 baseline. NOT a measured value — used solely for the labelled
# scenario projection, never for a headline ROI figure.
_SCENARIO_PRODUCTIVITY_PER_RATING = 0.15
_SCENARIO_RATING_BASELINE = 3.0


def get_roi_metrics(app, company_id, fiscal_year=None):
    """
    Canonical training-ROI computation for a company (roadmap value-4).

    All headline metrics are grounded in real data and scoped by company_id +
    fiscal period. The only estimated/hypothetical content lives under the
    clearly-labelled metrics['scenario'] key and is never mixed into a headline.

    Args:
        app:          Flask app (used for app_context()).
        company_id:   tenant id — EVERY query is scoped by this.
        fiscal_year:  fiscal period for spend/budget; defaults to current year.

    Returns a dict that is safe-zero on empty data.
    """
    import MySQLdb.cursors
    import datetime as _dt

    if fiscal_year is None:
        try:
            fiscal_year = _dt.datetime.now().year
        except Exception:
            fiscal_year = 0

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        metrics = {
            'fiscal_year': fiscal_year,
            # --- real, grounded headline figures ---
            'total_training_spend': 0.0,
            'employees_trained': 0,
            'courses_completed': 0,
            'courses_total': 0,
            'completion_rate': 0.0,
            'spend_per_employee': 0,
            'cost_per_completion': 0,
            'courses_per_kr': 0,
            'avg_completion_days': 0,
            'department_roi': [],
            # --- budget (real) ---
            'budget_total': 0.0,
            'budget_spent': 0.0,
            'budget_remaining': 0.0,
            'budget_utilization': 0.0,
            'budget_overruns': [],
            'has_data': False,
            # --- hypothetical projection (explicitly NOT real) ---
            'scenario': None,
        }

        try:
            # ── Real spend / completions, scoped by company_id + fiscal year ──
            cur.execute("""
                SELECT COALESCE(SUM(price), 0) AS total_spend,
                       COUNT(DISTINCT user_id) AS employees_trained,
                       COUNT(CASE WHEN completion_status = 'completed' THEN 1 END) AS completed,
                       COUNT(*) AS total_orders
                FROM course_orders
                WHERE company_id = %s
                  AND status NOT IN ('cancelled', 'rejected')
                  AND YEAR(created_at) = %s
            """, (company_id, fiscal_year))
            row = cur.fetchone() or {}
            metrics['total_training_spend'] = float(row.get('total_spend') or 0)
            metrics['employees_trained'] = int(row.get('employees_trained') or 0)
            metrics['courses_completed'] = int(row.get('completed') or 0)
            metrics['courses_total'] = int(row.get('total_orders') or 0)

            if metrics['courses_total'] > 0:
                metrics['completion_rate'] = round(
                    metrics['courses_completed'] / metrics['courses_total'] * 100, 1)
            if metrics['employees_trained'] > 0:
                metrics['spend_per_employee'] = round(
                    metrics['total_training_spend'] / metrics['employees_trained'])
            if metrics['courses_completed'] > 0:
                metrics['cost_per_completion'] = round(
                    metrics['total_training_spend'] / metrics['courses_completed'])
            if metrics['total_training_spend'] > 0:
                metrics['courses_per_kr'] = round(
                    metrics['courses_completed'] / (metrics['total_training_spend'] / 1000), 2)

            metrics['has_data'] = metrics['courses_total'] > 0 or metrics['total_training_spend'] > 0

            # ── Avg time to completion (real) ──
            cur.execute("""
                SELECT AVG(DATEDIFF(completion_date, created_at)) AS avg_days
                FROM course_orders
                WHERE company_id = %s
                  AND completion_status = 'completed'
                  AND completion_date IS NOT NULL
                  AND YEAR(created_at) = %s
            """, (company_id, fiscal_year))
            row = cur.fetchone() or {}
            metrics['avg_completion_days'] = round(float(row.get('avg_days') or 0), 1)

            # ── Department breakdown (real) ──
            cur.execute("""
                SELECT co.department,
                       COALESCE(SUM(co.price), 0) AS dept_spend,
                       COUNT(DISTINCT co.user_id) AS dept_employees,
                       COUNT(CASE WHEN co.completion_status = 'completed' THEN 1 END) AS dept_completed,
                       COUNT(*) AS dept_total
                FROM course_orders co
                WHERE co.company_id = %s
                  AND co.department IS NOT NULL AND co.department != ''
                  AND co.status NOT IN ('cancelled', 'rejected')
                  AND YEAR(co.created_at) = %s
                GROUP BY co.department
                ORDER BY dept_spend DESC
            """, (company_id, fiscal_year))
            for r in cur.fetchall():
                spend = float(r.get('dept_spend') or 0)
                completed = int(r.get('dept_completed') or 0)
                total = int(r.get('dept_total') or 0)
                metrics['department_roi'].append({
                    'department': r.get('department'),
                    'spend': spend,
                    'employees': int(r.get('dept_employees') or 0),
                    'completed': completed,
                    'total_orders': total,
                    'completion_rate': round(completed / total * 100, 1) if total > 0 else 0,
                    'cost_per_completion': round(spend / completed) if completed > 0 else 0,
                })

            # k-anonymity: a department whose trained-employee count is below k
            # would expose an individual's training spend / ROI. Suppress those
            # rows from the per-department breakdown. The company-wide headline
            # figures (total_training_spend, employees_trained, …) are computed
            # separately above and stay intact. Guarded — if k-anon is missing,
            # the unsuppressed breakdown is returned exactly as before.
            if _kanon is not None and metrics['department_roi']:
                try:
                    kept, note = _kanon.suppress_small_groups(
                        metrics['department_roi'], 'employees')
                    metrics['department_roi'] = kept
                    if note and note.get('suppressed'):
                        metrics['department_roi_anon_note'] = note.get('note_da')
                except Exception:
                    pass

            # ── Budget utilisation + OVERRUNS (real) ──
            cur.execute("""
                SELECT department,
                       COALESCE(annual_budget, 0) AS annual_budget,
                       COALESCE(spent, 0) AS spent
                FROM department_budgets
                WHERE company_id = %s AND fiscal_year = %s
            """, (company_id, fiscal_year))
            for b in cur.fetchall():
                annual = float(b.get('annual_budget') or 0)
                spent = float(b.get('spent') or 0)
                metrics['budget_total'] += annual
                metrics['budget_spent'] += spent
                if spent > annual and annual > 0:
                    metrics['budget_overruns'].append({
                        'department': b.get('department'),
                        'annual_budget': annual,
                        'spent': spent,
                        'overrun': round(spent - annual),
                        'overrun_pct': round((spent - annual) / annual * 100, 1),
                    })
            metrics['budget_remaining'] = metrics['budget_total'] - metrics['budget_spent']
            if metrics['budget_total'] > 0:
                metrics['budget_utilization'] = round(
                    metrics['budget_spent'] / metrics['budget_total'] * 100, 1)
            metrics['budget_overruns'].sort(key=lambda x: x['overrun'], reverse=True)

            # ── Hypothetical scenario projection (NOT real) ──────────────────
            # Computed separately and explicitly flagged. The 0.15 multiplier
            # lives ONLY here and never drives a headline number. The template
            # renders this under "Estimeret scenarie — ikke faktiske tal".
            metrics['scenario'] = _build_roi_scenario(cur, company_id, metrics['total_training_spend'])

        except Exception as e:
            logger.warning(f"ROI metrics error: {e}")

        try:
            cur.close()
        except Exception:
            pass
        return metrics


def _build_roi_scenario(cur, company_id, total_investment):
    """
    Build the HYPOTHETICAL ROI projection (Theme E "estimeret scenarie").

    This is the ONLY place the fabricated productivity multiplier lives. It is
    never mixed into a real headline figure — the caller stores it under the
    metrics['scenario'] key and the template labels it as a non-factual
    projection with the assumption shown.

    Returns None when there is no investment to project against (safe-zero).
    """
    investment = float(total_investment or 0)
    if investment <= 0:
        return None
    try:
        cur.execute("""
            SELECT AVG(performance_rating) AS avg_rating
            FROM company_users
            WHERE company_id = %s AND status = 'active'
              AND performance_rating IS NOT NULL
        """, (company_id,))
        row = cur.fetchone() or {}
        avg_rating = row.get('avg_rating')
    except Exception as e:
        logger.warning(f"ROI scenario rating lookup error: {e}")
        avg_rating = None

    if avg_rating is None:
        return None

    avg_rating = float(avg_rating)
    # Hypothetical only — clearly an assumption, not measured.
    performance_improvement = (avg_rating - _SCENARIO_RATING_BASELINE) / _SCENARIO_RATING_BASELINE
    estimated_productivity_gain = performance_improvement * _SCENARIO_PRODUCTIVITY_PER_RATING
    estimated_value = investment * (1 + estimated_productivity_gain)
    estimated_roi_pct = ((estimated_value - investment) / investment * 100) if investment > 0 else 0.0

    return {
        'is_estimate': True,
        'avg_performance_rating': round(avg_rating, 2),
        'baseline_rating': _SCENARIO_RATING_BASELINE,
        'productivity_per_rating_point': _SCENARIO_PRODUCTIVITY_PER_RATING,
        'estimated_productivity_gain_pct': round(estimated_productivity_gain * 100, 1),
        'estimated_value_generated': round(estimated_value, 2),
        'estimated_roi_percentage': round(estimated_roi_pct, 2),
        'assumption_text': (
            "Antagelse: {:.0f}% produktivitetsgevinst pr. point i "
            "performance-rating over basislinjen {:.1f}."
        ).format(_SCENARIO_PRODUCTIVITY_PER_RATING * 100, _SCENARIO_RATING_BASELINE),
    }


def get_predictive_data(app, company_id):
    """
    Phase 3.4: Basic predictive analytics.
    Returns employees at risk, trending courses, churn risk.
    """
    import MySQLdb.cursors

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        predictions = {
            'at_risk_employees': [],
            'trending_courses': [],
            'churn_risk': [],
        }

        try:
            # Churn = active employees whose *last learning activity* is 30+ days
            # old. The signal is a unified GREATEST() over every way an employee
            # touches the platform — not chatbot-only — mirroring the coalesce
            # _execute_hr_inactive_employees already uses (hr_tools.py), so the
            # predictive path stops flagging learners-who-never-chat as inactive.
            # Sources: last_chatbot_interaction, last_active_at, most recent
            # course_orders.created_at, most recent
            # employee_learning_progress.last_accessed (all company-scoped).
            # Anchored at '1970-01-01' so a NULL source never wins the GREATEST;
            # employees with NO activity at all (all four NULL) are excluded —
            # they're never-onboarded, not churn.
            cur.execute("""
                SELECT cu.user_id, u.username, cu.department, cu.job_title,
                       cu.last_chatbot_interaction, cu.total_chatbot_queries,
                       GREATEST(
                           COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                           COALESCE(cu.last_active_at, '1970-01-01'),
                           COALESCE(ord.last_order_at, '1970-01-01'),
                           COALESCE(prog.last_progress_at, '1970-01-01')
                       ) AS last_learning_activity,
                       DATEDIFF(NOW(), GREATEST(
                           COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                           COALESCE(cu.last_active_at, '1970-01-01'),
                           COALESCE(ord.last_order_at, '1970-01-01'),
                           COALESCE(prog.last_progress_at, '1970-01-01')
                       )) AS days_inactive
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN (
                    SELECT user_id, MAX(created_at) AS last_order_at
                    FROM course_orders
                    WHERE company_id = %s
                    GROUP BY user_id
                ) ord ON ord.user_id = cu.user_id
                LEFT JOIN (
                    SELECT user_id, MAX(last_accessed) AS last_progress_at
                    FROM employee_learning_progress
                    WHERE company_id = %s
                    GROUP BY user_id
                ) prog ON prog.user_id = cu.user_id
                WHERE cu.company_id = %s AND cu.status = 'active'
                AND GREATEST(
                        COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                        COALESCE(cu.last_active_at, '1970-01-01'),
                        COALESCE(ord.last_order_at, '1970-01-01'),
                        COALESCE(prog.last_progress_at, '1970-01-01')
                    ) > '1970-01-01'
                AND GREATEST(
                        COALESCE(cu.last_chatbot_interaction, '1970-01-01'),
                        COALESCE(cu.last_active_at, '1970-01-01'),
                        COALESCE(ord.last_order_at, '1970-01-01'),
                        COALESCE(prog.last_progress_at, '1970-01-01')
                    ) < DATE_SUB(NOW(), INTERVAL 30 DAY)
                ORDER BY days_inactive DESC
                LIMIT 10
            """, (company_id, company_id, company_id))
            predictions['churn_risk'] = cur.fetchall()

            # Employees with skill gaps (current_level < target_level by 2+)
            cur.execute("""
                SELECT u.username, cu.department, cu.job_title,
                       esm.skill_name, esm.current_level,
                       cst.target_level, (cst.target_level - esm.current_level) AS gap
                FROM employee_skills_matrix esm
                JOIN company_users cu ON esm.employee_id = cu.user_id AND esm.company_id = cu.company_id
                JOIN users u ON cu.user_id = u.id
                JOIN company_skill_targets cst ON esm.company_id = cst.company_id
                     AND esm.skill_name = cst.skill_name
                     AND (cst.department = cu.department OR cst.department IS NULL OR cst.department = '')
                WHERE esm.company_id = %s AND cu.status = 'active'
                AND (cst.target_level - esm.current_level) >= 2
                ORDER BY gap DESC
                LIMIT 15
            """, (company_id,))
            predictions['at_risk_employees'] = cur.fetchall()

            # Trending course searches (what employees search for most, last 14 days)
            cur.execute("""
                SELECT ci.query_text, COUNT(*) AS cnt
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id AND cu.company_id = %s
                WHERE ci.created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)
                AND ci.tools_used LIKE '%%search_courses%%'
                AND ci.query_text IS NOT NULL
                GROUP BY ci.query_text
                ORDER BY cnt DESC
                LIMIT 10
            """, (company_id,))
            predictions['trending_courses'] = cur.fetchall()

        except Exception as e:
            logger.warning(f"Predictive analytics error: {e}")

        cur.close()
        return predictions


def _coerce_dept(value):
    """Normalise a department label, folding NULL/empty into a stable bucket."""
    s = (str(value).strip() if value is not None else "")
    return s or "Ukendt"


def aggregate_workforce_risk(predictions, *, k=None, drill_down=False,
                             drill_limit=10):
    """Turn raw ``get_predictive_data`` output into an AGGREGATE-FIRST, k-anon
    -safe workforce-risk payload suitable for the conversational HR advisor.

    ``get_predictive_data`` returns NAMED INDIVIDUALS (username / user_id /
    job_title / department for churn_risk and at_risk_employees) with NO
    suppression — surfacing those rows verbatim through an AI tool would let the
    model verbalise identifiable at-risk/churn employees with no k-anon floor, a
    GDPR / k-anonymity hard-constraint violation (HR_VALUE_PLAN risk register).

    This helper is PURE (no DB, no Flask) so the suppression logic is unit
    -testable, and it is the single place the advisor's net-new k-anon floor
    lives. It returns:

      * per-DEPARTMENT counts for churn risk and skill-gap risk, with every
        department cohort below ``k`` SUPPRESSED via
        ``kanon.suppress_small_groups`` (counts only — no names);
      * per-SKILL counts for skill-gap risk, suppressed the same way;
      * trending unmet learning demand, keeping only query terms whose volume is
        itself >= ``k`` (a term searched by a single person can be re-identifying);
      * company-wide totals (already aggregated, hence safe);
      * a Danish anonymity note when anything was suppressed.

    Individual NAMES are NEVER included unless ``drill_down=True`` is passed
    explicitly (the executor gates this behind a manager role + an explicit
    confirm), and even then names are only emitted for departments whose cohort
    is itself k-safe — naming the sole member of a 1-person department IS the
    de-anonymisation we are guarding against.

    Guarded: any malformed input degrades to empty aggregates rather than
    raising, so a bad upstream payload can never crash a tool call.

    Args:
        predictions: the dict returned by ``get_predictive_data``
                     (``churn_risk`` / ``at_risk_employees`` / ``trending_courses``).
        k:           minimum cohort size; defaults to env-resolved K_DEFAULT.
        drill_down:  when True, attach a capped, k-safe-department-only list of
                     named individuals under ``*_individuals``.
        drill_limit: cap on the number of named individuals per drill-down list.

    Returns:
        dict with ``churn_risk`` / ``skill_gap_risk`` / ``trending_demand``
        sections plus ``anon`` metadata.
    """
    kk = None
    if _kanon is not None:
        try:
            kk = _kanon._coerce_k(k)
        except Exception:
            kk = None
    if kk is None:
        # k-anon module missing: fail CLOSED — pick a conservative default and
        # suppress with a local floor so we never emit sub-k named/aggregate rows.
        try:
            kk = int(k) if k else 5
        except Exception:
            kk = 5
        if kk < 1:
            kk = 5

    out = {
        "k": kk,
        "churn_risk": {"total": 0, "by_department": []},
        "skill_gap_risk": {"total": 0, "by_department": [], "by_skill": []},
        "trending_demand": {"terms": []},
        "anon": {"suppressed_groups": 0, "note_da": ""},
    }
    if not isinstance(predictions, dict):
        return out

    def _suppress(rows, count_key):
        """Drop cohorts below k. Returns (kept_rows, n_suppressed)."""
        if _kanon is not None:
            try:
                kept, note = _kanon.suppress_small_groups(rows, count_key, k=kk)
                return kept, int(note.get("suppressed", 0) or 0)
            except Exception:
                pass
        # Local fail-closed fallback when kanon is unavailable / errored.
        kept, dropped = [], 0
        for r in rows:
            try:
                if int(r.get(count_key) or 0) >= kk:
                    kept.append(r)
                else:
                    dropped += 1
            except Exception:
                dropped += 1
        return kept, dropped

    total_suppressed = 0

    # ── 1) Churn risk: per-department distinct-employee counts ──
    churn_rows = predictions.get("churn_risk") or []
    churn_by_dept = {}
    churn_members = {}  # dept -> [named individuals] (only used for drill-down)
    churn_total = 0
    for r in churn_rows:
        if not isinstance(r, dict):
            continue
        churn_total += 1
        dept = _coerce_dept(r.get("department"))
        churn_by_dept[dept] = churn_by_dept.get(dept, 0) + 1
        churn_members.setdefault(dept, []).append(r)
    out["churn_risk"]["total"] = churn_total
    churn_dept_rows = [{"department": d, "count": c} for d, c in churn_by_dept.items()]
    kept, dropped = _suppress(churn_dept_rows, "count")
    total_suppressed += dropped
    out["churn_risk"]["by_department"] = sorted(
        kept, key=lambda x: x.get("count", 0), reverse=True)

    # ── 2) Skill-gap risk: per-department + per-skill distinct-employee counts ──
    gap_rows = predictions.get("at_risk_employees") or []
    gap_dept_members = {}   # dept -> set(username)
    gap_skill_members = {}  # skill -> set(username)
    gap_employees = set()
    gap_member_rows = {}    # dept -> [rows] for drill-down
    for r in gap_rows:
        if not isinstance(r, dict):
            continue
        uname = r.get("username")
        dept = _coerce_dept(r.get("department"))
        skill = (str(r.get("skill_name")).strip() if r.get("skill_name") else "") or "Ukendt"
        key = uname if uname is not None else (dept, skill, r.get("gap"))
        gap_employees.add(uname if uname is not None else id(r))
        gap_dept_members.setdefault(dept, set()).add(key)
        gap_skill_members.setdefault(skill, set()).add(key)
        gap_member_rows.setdefault(dept, []).append(r)
    out["skill_gap_risk"]["total"] = len(gap_employees)
    gap_dept_count_rows = [
        {"department": d, "count": len(members)}
        for d, members in gap_dept_members.items()
    ]
    kept_d, dropped_d = _suppress(gap_dept_count_rows, "count")
    total_suppressed += dropped_d
    out["skill_gap_risk"]["by_department"] = sorted(
        kept_d, key=lambda x: x.get("count", 0), reverse=True)
    gap_skill_count_rows = [
        {"skill": s, "count": len(members)}
        for s, members in gap_skill_members.items()
    ]
    kept_s, dropped_s = _suppress(gap_skill_count_rows, "count")
    total_suppressed += dropped_s
    out["skill_gap_risk"]["by_skill"] = sorted(
        kept_s, key=lambda x: x.get("count", 0), reverse=True)

    # ── 3) Trending unmet learning demand: keep only k-safe-volume terms ──
    trend_rows = predictions.get("trending_courses") or []
    safe_terms = []
    for r in trend_rows:
        if not isinstance(r, dict):
            continue
        try:
            cnt = int(r.get("cnt") or 0)
        except Exception:
            cnt = 0
        qt = (str(r.get("query_text")).strip() if r.get("query_text") else "")
        if not qt:
            continue
        if cnt >= kk:
            safe_terms.append({"query": qt, "count": cnt})
        else:
            total_suppressed += 1
    out["trending_demand"]["terms"] = sorted(
        safe_terms, key=lambda x: x.get("count", 0), reverse=True)

    # ── 4) Optional manager-scoped drill-down (k-safe departments only) ──
    if drill_down:
        try:
            dl = int(drill_limit)
        except Exception:
            dl = 10
        if dl < 0:
            dl = 10
        safe_churn_depts = {row["department"] for row in out["churn_risk"]["by_department"]}
        churn_named = []
        for dept in safe_churn_depts:
            for r in churn_members.get(dept, []):
                churn_named.append({
                    "username": r.get("username"),
                    "department": dept,
                    "job_title": r.get("job_title"),
                    "days_inactive": r.get("days_inactive"),
                })
        churn_named.sort(key=lambda x: (x.get("days_inactive") or 0), reverse=True)
        out["churn_risk"]["individuals"] = churn_named[:dl]

        safe_gap_depts = {row["department"] for row in out["skill_gap_risk"]["by_department"]}
        gap_named = []
        for dept in safe_gap_depts:
            for r in gap_member_rows.get(dept, []):
                gap_named.append({
                    "username": r.get("username"),
                    "department": dept,
                    "skill": r.get("skill_name"),
                    "current_level": r.get("current_level"),
                    "target_level": r.get("target_level"),
                    "gap": r.get("gap"),
                })
        gap_named.sort(key=lambda x: (x.get("gap") or 0), reverse=True)
        out["skill_gap_risk"]["individuals"] = gap_named[:dl]

    out["anon"]["suppressed_groups"] = total_suppressed
    if total_suppressed:
        try:
            out["anon"]["note_da"] = (
                _kanon.anon_note(kk) if _kanon is not None
                else f"Grupper under k={kk} er skjult af hensyn til anonymitet"
            )
        except Exception:
            out["anon"]["note_da"] = f"Grupper under k={kk} er skjult af hensyn til anonymitet"
    return out
