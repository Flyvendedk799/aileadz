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

        # Get current employee skills
        cur.execute("""
            SELECT esm.skill_name, esm.current_level, cu.department, cu.user_id
            FROM employee_skills_matrix esm
            JOIN company_users cu ON esm.employee_id = cu.user_id AND esm.company_id = cu.company_id
            WHERE esm.company_id = %s AND cu.status = 'active'
        """, (company_id,))
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


def get_roi_metrics(app, company_id):
    """
    Phase 3.3: Calculate training ROI metrics for a company.
    """
    import MySQLdb.cursors

    with app.app_context():
        conn = app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)

        metrics = {
            'total_training_spend': 0,
            'employees_trained': 0,
            'courses_completed': 0,
            'spend_per_employee': 0,
            'courses_per_kr': 0,
            'avg_completion_days': 0,
            'department_roi': [],
        }

        try:
            # Total spend
            cur.execute("""
                SELECT COALESCE(SUM(price), 0) AS total_spend,
                       COUNT(DISTINCT user_id) AS employees_trained,
                       COUNT(CASE WHEN completion_status = 'completed' THEN 1 END) AS completed
                FROM course_orders
                WHERE company_id = %s AND status NOT IN ('cancelled', 'rejected')
            """, (company_id,))
            row = cur.fetchone()
            metrics['total_training_spend'] = float(row['total_spend'] or 0)
            metrics['employees_trained'] = row['employees_trained'] or 0
            metrics['courses_completed'] = row['completed'] or 0

            if metrics['employees_trained'] > 0:
                metrics['spend_per_employee'] = round(metrics['total_training_spend'] / metrics['employees_trained'])
            if metrics['total_training_spend'] > 0:
                metrics['courses_per_kr'] = round(metrics['courses_completed'] / (metrics['total_training_spend'] / 1000), 2)

            # Avg time to completion
            cur.execute("""
                SELECT AVG(DATEDIFF(completion_date, created_at)) AS avg_days
                FROM course_orders
                WHERE company_id = %s AND completion_status = 'completed' AND completion_date IS NOT NULL
            """, (company_id,))
            row = cur.fetchone()
            metrics['avg_completion_days'] = round(row['avg_days'] or 0, 1)

            # Department breakdown
            cur.execute("""
                SELECT co.department,
                       COALESCE(SUM(co.price), 0) AS dept_spend,
                       COUNT(DISTINCT co.user_id) AS dept_employees,
                       COUNT(CASE WHEN co.completion_status = 'completed' THEN 1 END) AS dept_completed,
                       COUNT(*) AS dept_total
                FROM course_orders co
                WHERE co.company_id = %s AND co.department IS NOT NULL AND co.department != ''
                AND co.status NOT IN ('cancelled', 'rejected')
                GROUP BY co.department
                ORDER BY dept_spend DESC
            """, (company_id,))
            for r in cur.fetchall():
                spend = float(r['dept_spend'] or 0)
                completed = r['dept_completed'] or 0
                total = r['dept_total'] or 0
                metrics['department_roi'].append({
                    'department': r['department'],
                    'spend': spend,
                    'employees': r['dept_employees'] or 0,
                    'completed': completed,
                    'total_orders': total,
                    'completion_rate': round(completed / total * 100, 1) if total > 0 else 0,
                    'cost_per_completion': round(spend / completed) if completed > 0 else 0,
                })
        except Exception as e:
            logger.warning(f"ROI metrics error: {e}")

        cur.close()
        return metrics


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
