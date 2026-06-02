"""
Phase 3.2 & 3.4: AI-powered conversation insights & predictive analytics.
Generates actionable intelligence from chatbot_interactions for HR dashboards.
"""
import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


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
        for i in interactions:
            dept = i.get('department') or 'Unknown'
            qt = i.get('query_type') or 'general'
            if dept not in dept_topics:
                dept_topics[dept] = {}
            dept_topics[dept][qt] = dept_topics[dept].get(qt, 0) + 1

        for dept, topics in dept_topics.items():
            if not topics:
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
        level_map = "CASE us.skill_level WHEN 'begynder' THEN 1 WHEN 'mellem' THEN 2 WHEN 'avanceret' THEN 3 WHEN 'ekspert' THEN 4 ELSE 2 END"
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

        return heatmap


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
            # Employees who haven't used chatbot in 30+ days but were active before
            cur.execute("""
                SELECT cu.user_id, u.username, cu.department, cu.job_title,
                       cu.last_chatbot_interaction, cu.total_chatbot_queries,
                       DATEDIFF(NOW(), cu.last_chatbot_interaction) AS days_inactive
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                WHERE cu.company_id = %s AND cu.status = 'active'
                AND cu.total_chatbot_queries > 3
                AND cu.last_chatbot_interaction < DATE_SUB(NOW(), INTERVAL 30 DAY)
                ORDER BY days_inactive DESC
                LIMIT 10
            """, (company_id,))
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
