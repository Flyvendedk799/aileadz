from flask import Blueprint, render_template, session, redirect, url_for, flash, current_app
import MySQLdb.cursors
from collections import defaultdict
import datetime
import json
import logging

admin_reports_bp = Blueprint('admin_reports', __name__, template_folder='templates')


@admin_reports_bp.route('/admin/chatbot-dashboard')
def chatbot_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        flash("Adgang naegtet.", "danger")
        return redirect(url_for('dashboard.dashboard'))

    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # --- KPI metrics ---
    total_chatbot_queries = 0
    avg_conversation_length = 0
    avg_chatbot_response_time = 0
    conversion_rate = 0
    pending_orders_count = 0
    total_revenue = 0

    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM chatbot_interactions")
        row = cur.fetchone()
        total_chatbot_queries = row['cnt'] if row else 0
    except Exception:
        pass

    try:
        cur.execute("""
            SELECT AVG(query_count) AS avg_len
            FROM (
                SELECT session_id, COUNT(*) AS query_count
                FROM chatbot_interactions
                GROUP BY session_id
            ) t
        """)
        row = cur.fetchone()
        avg_conversation_length = round(row['avg_len'] or 0, 1)
    except Exception:
        pass

    try:
        cur.execute("SELECT AVG(response_time_ms) AS avg_rt FROM chatbot_interactions WHERE response_time_ms IS NOT NULL")
        row = cur.fetchone()
        avg_chatbot_response_time = round(row['avg_rt'] or 0)
    except Exception:
        pass

    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM course_orders WHERE status = 'pending'")
        row = cur.fetchone()
        pending_orders_count = row['cnt'] if row else 0
    except Exception:
        pass

    try:
        cur.execute("SELECT SUM(price) AS rev FROM course_orders WHERE status IN ('completed', 'paid')")
        row = cur.fetchone()
        total_revenue = row['rev'] or 0
    except Exception:
        pass

    try:
        total_convs = 0
        converting_convs = 0
        cur.execute("SELECT COUNT(DISTINCT session_id) AS cnt FROM chatbot_interactions")
        row = cur.fetchone()
        total_convs = row['cnt'] if row else 0
        cur.execute("SELECT COUNT(DISTINCT chatbot_session_id) AS cnt FROM course_orders WHERE chatbot_session_id IS NOT NULL AND chatbot_session_id != ''")
        row = cur.fetchone()
        converting_convs = row['cnt'] if row else 0
        conversion_rate = round((converting_convs / total_convs * 100) if total_convs else 0, 1)
    except Exception:
        pass

    # --- Daily chatbot activity (last 30 days) ---
    daily_chatbot_query_labels = []
    daily_chatbot_query_data = []
    try:
        cur.execute("""
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
            GROUP BY DATE(created_at)
            ORDER BY day ASC
        """)
        rows = cur.fetchall()
        daily_chatbot_query_labels = [r['day'].strftime('%Y-%m-%d') if hasattr(r['day'], 'strftime') else str(r['day']) for r in rows]
        daily_chatbot_query_data = [r['cnt'] for r in rows]
    except Exception:
        pass

    # --- Query type distribution ---
    query_type_distribution = {}
    try:
        cur.execute("""
            SELECT COALESCE(query_type, 'unknown') AS qtype, COUNT(*) AS cnt
            FROM chatbot_interactions
            GROUP BY query_type
            ORDER BY cnt DESC
            LIMIT 10
        """)
        for r in cur.fetchall():
            query_type_distribution[r['qtype']] = r['cnt']
    except Exception:
        pass
    if not query_type_distribution:
        query_type_distribution = {"Generelt": 1}

    # --- Category distribution ---
    category_distribution = {}
    try:
        cur.execute("""
            SELECT COALESCE(category, 'Andet') AS cat, COUNT(*) AS cnt
            FROM chatbot_interactions
            GROUP BY category
            ORDER BY cnt DESC
            LIMIT 10
        """)
        for r in cur.fetchall():
            category_distribution[r['cat']] = r['cnt']
    except Exception:
        pass
    if not category_distribution:
        category_distribution = {"Andet": 1}

    # --- User locations ---
    user_locations = {}
    try:
        cur.execute("""
            SELECT COALESCE(user_location, 'Ukendt') AS loc, COUNT(*) AS cnt
            FROM chatbot_interactions
            WHERE user_location IS NOT NULL
            GROUP BY user_location
            ORDER BY cnt DESC
            LIMIT 10
        """)
        for r in cur.fetchall():
            user_locations[r['loc']] = r['cnt']
    except Exception:
        pass
    if not user_locations:
        user_locations = {"Ukendt": 1}

    # --- Tool usage breakdown ---
    tool_usage = {}
    try:
        cur.execute("""
            SELECT tools_used FROM chatbot_interactions
            WHERE tools_used IS NOT NULL AND tools_used != ''
        """)
        for r in cur.fetchall():
            for tool in r['tools_used'].split(','):
                tool = tool.strip()
                if tool:
                    tool_usage[tool] = tool_usage.get(tool, 0) + 1
        # Sort by count desc, keep top 10
        tool_usage = dict(sorted(tool_usage.items(), key=lambda x: x[1], reverse=True)[:10])
    except Exception:
        pass
    if not tool_usage:
        tool_usage = {"Ingen data": 1}

    # --- Feedback ratings ---
    avg_feedback = 0
    feedback_distribution = {}
    try:
        cur.execute("""
            SELECT AVG(feedback_rating) AS avg_fb
            FROM chatbot_interactions
            WHERE feedback_rating IS NOT NULL AND feedback_rating > 0
        """)
        row = cur.fetchone()
        avg_feedback = round(row['avg_fb'] or 0, 1)
    except Exception:
        pass
    try:
        cur.execute("""
            SELECT feedback_rating AS rating, COUNT(*) AS cnt
            FROM chatbot_interactions
            WHERE feedback_rating IS NOT NULL AND feedback_rating > 0
            GROUP BY feedback_rating
            ORDER BY feedback_rating
        """)
        for r in cur.fetchall():
            feedback_distribution[str(r['rating'])] = r['cnt']
    except Exception:
        pass

    # --- Conversation depth ---
    avg_depth = 0
    try:
        cur.execute("""
            SELECT AVG(conversation_depth) AS avg_d
            FROM chatbot_interactions
            WHERE conversation_depth IS NOT NULL AND conversation_depth > 0
        """)
        row = cur.fetchone()
        avg_depth = round(row['avg_d'] or 0, 1)
    except Exception:
        pass

    # --- Logged-in vs anonymous ---
    logged_in_count = 0
    anonymous_count = 0
    try:
        cur.execute("""
            SELECT
                SUM(CASE WHEN is_logged_in = 1 THEN 1 ELSE 0 END) AS logged_in,
                SUM(CASE WHEN is_logged_in = 0 OR is_logged_in IS NULL THEN 1 ELSE 0 END) AS anonymous
            FROM chatbot_interactions
        """)
        row = cur.fetchone()
        logged_in_count = row['logged_in'] or 0
        anonymous_count = row['anonymous'] or 0
    except Exception:
        pass

    # --- Products shown by chatbot (most recommended) ---
    products_shown_ranking = {}
    try:
        cur.execute("""
            SELECT products_shown FROM chatbot_interactions
            WHERE products_shown IS NOT NULL AND products_shown != ''
        """)
        for r in cur.fetchall():
            try:
                handles = json.loads(r['products_shown'])
                for h in handles:
                    if h:
                        products_shown_ranking[h] = products_shown_ranking.get(h, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        products_shown_ranking = dict(sorted(products_shown_ranking.items(), key=lambda x: x[1], reverse=True)[:10])
    except Exception:
        pass

    # --- Quality score distribution ---
    avg_quality = 0
    try:
        cur.execute("""
            SELECT AVG(interaction_quality_score) AS avg_q
            FROM chatbot_interactions
            WHERE interaction_quality_score IS NOT NULL AND interaction_quality_score > 0
        """)
        row = cur.fetchone()
        avg_quality = round(row['avg_q'] or 0, 2)
    except Exception:
        pass

    # --- Conversion attribution (Phase 2.1) ---
    conversion_by_tool = {}
    avg_queries_before_order = 0
    try:
        cur.execute("""
            SELECT COALESCE(recommended_by_tool, 'direct') AS tool, COUNT(*) AS cnt
            FROM course_orders
            WHERE recommended_by_tool IS NOT NULL AND recommended_by_tool != ''
            GROUP BY recommended_by_tool
            ORDER BY cnt DESC
        """)
        for r in cur.fetchall():
            conversion_by_tool[r['tool']] = r['cnt']
    except Exception:
        pass
    try:
        cur.execute("""
            SELECT AVG(chatbot_queries_before_order) AS avg_q
            FROM course_orders
            WHERE chatbot_queries_before_order > 0
        """)
        row = cur.fetchone()
        avg_queries_before_order = round(row['avg_q'] or 0, 1)
    except Exception:
        pass

    # --- Pending approvals (Phase 2.2) ---
    total_pending_approvals = 0
    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM order_approvals WHERE status = 'pending'")
        row = cur.fetchone()
        total_pending_approvals = row['cnt'] or 0
    except Exception:
        pass

    # --- Popular courses ---
    popular_courses = []
    try:
        cur.execute("""
            SELECT product_handle, COUNT(*) AS cnt
            FROM course_orders
            GROUP BY product_handle
            ORDER BY cnt DESC
            LIMIT 10
        """)
        popular_courses = [(r['product_handle'], r['cnt']) for r in cur.fetchall()]
    except Exception:
        pass

    # --- Recent orders ---
    recent_orders = []
    try:
        cur.execute("""
            SELECT order_id, product_title, price, status, created_at, user_email
            FROM course_orders
            ORDER BY created_at DESC
            LIMIT 20
        """)
        for r in cur.fetchall():
            r['order_id_short'] = str(r['order_id'])[:8] if r.get('order_id') else ''
            recent_orders.append(r)
    except Exception:
        pass

    # --- Frequent questions ---
    frequent_questions = []
    try:
        cur.execute("""
            SELECT query_text AS text, COUNT(*) AS count,
                   (SELECT ci2.response_text FROM chatbot_interactions ci2 WHERE ci2.query_text = chatbot_interactions.query_text LIMIT 1) AS top_response
            FROM chatbot_interactions
            WHERE query_text IS NOT NULL
            GROUP BY query_text
            ORDER BY count DESC
            LIMIT 15
        """)
        frequent_questions = cur.fetchall()
    except Exception:
        pass

    # --- Recent conversations ---
    recent_conversations = []
    try:
        cur.execute("""
            SELECT session_id,
                   MIN(created_at) AS start_time,
                   TIMESTAMPDIFF(MINUTE, MIN(created_at), MAX(created_at)) AS duration,
                   COUNT(*) AS query_count
            FROM chatbot_interactions
            GROUP BY session_id
            ORDER BY start_time DESC
            LIMIT 20
        """)
        for r in cur.fetchall():
            r['topics'] = []
            r['start_time'] = r['start_time'].strftime('%Y-%m-%d %H:%M') if hasattr(r['start_time'], 'strftime') else str(r['start_time'])
            r['duration'] = f"{r['duration'] or 0} min"
            recent_conversations.append(r)
    except Exception:
        pass

    cur.close()

    return render_template('fm/reports_dashboard.html',
                           total_chatbot_queries=total_chatbot_queries,
                           avg_conversation_length=avg_conversation_length,
                           avg_chatbot_response_time=avg_chatbot_response_time,
                           conversion_rate=conversion_rate,
                           pending_orders_count=pending_orders_count,
                           total_revenue=total_revenue,
                           daily_chatbot_query_labels=daily_chatbot_query_labels,
                           daily_chatbot_query_data=daily_chatbot_query_data,
                           query_type_distribution=query_type_distribution,
                           category_distribution=category_distribution,
                           user_locations=user_locations,
                           popular_courses=popular_courses,
                           recent_orders=recent_orders,
                           frequent_questions=frequent_questions,
                           recent_conversations=recent_conversations,
                           tool_usage=tool_usage,
                           avg_feedback=avg_feedback,
                           feedback_distribution=feedback_distribution,
                           avg_depth=avg_depth,
                           avg_quality=avg_quality,
                           logged_in_count=logged_in_count,
                           anonymous_count=anonymous_count,
                           products_shown_ranking=products_shown_ranking,
                           conversion_by_tool=conversion_by_tool,
                           avg_queries_before_order=avg_queries_before_order,
                           total_pending_approvals=total_pending_approvals)


@admin_reports_bp.route('/admin/ai-cost')
def ai_cost_dashboard():
    if 'user' not in session or session.get('role') != 'admin':
        flash("Adgang naegtet.", "danger")
        return redirect(url_for('dashboard.dashboard'))

    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # --- Agent run KPIs (last 30 days) ---
    total_runs = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_cached_tokens = 0
    total_tokens = 0
    avg_tokens = 0
    avg_latency = 0
    p95_latency = 0
    fallback_rate = 0
    fallback_count = 0
    failed_count = 0

    try:
        cur.execute("""
            SELECT
                COUNT(*) AS cnt,
                COALESCE(SUM(input_tokens), 0) AS in_tok,
                COALESCE(SUM(output_tokens), 0) AS out_tok,
                COALESCE(SUM(cached_tokens), 0) AS cache_tok,
                COALESCE(AVG(latency_ms), 0) AS avg_lat,
                SUM(CASE WHEN fallback_reason IS NOT NULL AND fallback_reason != '' THEN 1 ELSE 0 END) AS fb_cnt,
                SUM(CASE WHEN status IN ('error', 'failed', 'timeout') THEN 1 ELSE 0 END) AS fail_cnt
            FROM ai_agent_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        """)
        row = cur.fetchone()
        if row:
            total_runs = row['cnt'] or 0
            total_input_tokens = int(row['in_tok'] or 0)
            total_output_tokens = int(row['out_tok'] or 0)
            total_cached_tokens = int(row['cache_tok'] or 0)
            total_tokens = total_input_tokens + total_output_tokens
            avg_latency = round(row['avg_lat'] or 0)
            fallback_count = int(row['fb_cnt'] or 0)
            failed_count = int(row['fail_cnt'] or 0)
            if total_runs:
                avg_tokens = round(total_tokens / total_runs)
                fallback_rate = round(fallback_count / total_runs * 100, 1)
    except Exception:
        pass

    # --- p95 latency (computed in Python; MySQL lacks percentile funcs) ---
    try:
        cur.execute("""
            SELECT latency_ms FROM ai_agent_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
              AND latency_ms IS NOT NULL
            ORDER BY latency_ms ASC
        """)
        latencies = [r['latency_ms'] for r in cur.fetchall() if r['latency_ms'] is not None]
        if latencies:
            idx = int(round(0.95 * (len(latencies) - 1)))
            p95_latency = int(latencies[idx])
    except Exception:
        pass

    # --- Model split ---
    model_split = {}
    try:
        cur.execute("""
            SELECT COALESCE(NULLIF(model, ''), 'ukendt') AS model, COUNT(*) AS cnt
            FROM ai_agent_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY COALESCE(NULLIF(model, ''), 'ukendt')
            ORDER BY cnt DESC
            LIMIT 10
        """)
        for r in cur.fetchall():
            model_split[r['model']] = r['cnt']
    except Exception:
        pass

    # --- Runtime / scope split ---
    scope_split = {}
    try:
        cur.execute("""
            SELECT COALESCE(NULLIF(agent_scope, ''), 'ukendt') AS scope, COUNT(*) AS cnt
            FROM ai_agent_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY COALESCE(NULLIF(agent_scope, ''), 'ukendt')
            ORDER BY cnt DESC
            LIMIT 10
        """)
        for r in cur.fetchall():
            scope_split[r['scope']] = r['cnt']
    except Exception:
        pass

    # --- Top tools by call count ---
    top_tools_by_count = []
    try:
        cur.execute("""
            SELECT COALESCE(NULLIF(tool_name, ''), 'ukendt') AS tool_name,
                   COUNT(*) AS call_count,
                   COALESCE(AVG(latency_ms), 0) AS avg_lat,
                   SUM(CASE WHEN status IN ('error', 'failed', 'timeout') THEN 1 ELSE 0 END) AS err_cnt
            FROM ai_tool_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY COALESCE(NULLIF(tool_name, ''), 'ukendt')
            ORDER BY call_count DESC
            LIMIT 15
        """)
        for r in cur.fetchall():
            top_tools_by_count.append({
                'tool_name': r['tool_name'],
                'call_count': r['call_count'],
                'avg_latency': round(r['avg_lat'] or 0),
                'error_count': int(r['err_cnt'] or 0),
            })
    except Exception:
        pass

    # --- Top tools by avg latency (min 3 calls to avoid noise) ---
    top_tools_by_latency = []
    try:
        cur.execute("""
            SELECT COALESCE(NULLIF(tool_name, ''), 'ukendt') AS tool_name,
                   COUNT(*) AS call_count,
                   COALESCE(AVG(latency_ms), 0) AS avg_lat
            FROM ai_tool_runs
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
              AND latency_ms IS NOT NULL
            GROUP BY COALESCE(NULLIF(tool_name, ''), 'ukendt')
            HAVING COUNT(*) >= 3
            ORDER BY avg_lat DESC
            LIMIT 15
        """)
        for r in cur.fetchall():
            top_tools_by_latency.append({
                'tool_name': r['tool_name'],
                'call_count': r['call_count'],
                'avg_latency': round(r['avg_lat'] or 0),
            })
    except Exception:
        pass

    # --- Daily run volume (last 30 days) for trend ---
    daily_run_labels = []
    daily_run_data = []
    try:
        cur.execute("""
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM ai_agent_runs
            WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
            GROUP BY DATE(created_at)
            ORDER BY day ASC
        """)
        rows = cur.fetchall()
        daily_run_labels = [r['day'].strftime('%Y-%m-%d') if hasattr(r['day'], 'strftime') else str(r['day']) for r in rows]
        daily_run_data = [r['cnt'] for r in rows]
    except Exception:
        pass

    # --- Recent runs (~20) ---
    recent_runs = []
    try:
        cur.execute("""
            SELECT run_id, agent_scope, runtime, model, status, fallback_reason,
                   latency_ms, input_tokens, output_tokens, cached_tokens,
                   username, created_at
            FROM ai_agent_runs
            ORDER BY created_at DESC
            LIMIT 20
        """)
        for r in cur.fetchall():
            r['run_id_short'] = str(r['run_id'])[:10] if r.get('run_id') else ''
            r['total_tokens'] = int((r.get('input_tokens') or 0)) + int((r.get('output_tokens') or 0))
            r['has_fallback'] = bool(r.get('fallback_reason'))
            r['created_at_fmt'] = r['created_at'].strftime('%Y-%m-%d %H:%M') if hasattr(r.get('created_at'), 'strftime') else str(r.get('created_at') or '')
            recent_runs.append(r)
    except Exception:
        pass

    cur.close()

    return render_template('fm/ai_cost.html',
                           total_runs=total_runs,
                           total_tokens=total_tokens,
                           total_input_tokens=total_input_tokens,
                           total_output_tokens=total_output_tokens,
                           total_cached_tokens=total_cached_tokens,
                           avg_tokens=avg_tokens,
                           avg_latency=avg_latency,
                           p95_latency=p95_latency,
                           fallback_rate=fallback_rate,
                           fallback_count=fallback_count,
                           failed_count=failed_count,
                           model_split=model_split,
                           scope_split=scope_split,
                           top_tools_by_count=top_tools_by_count,
                           top_tools_by_latency=top_tools_by_latency,
                           daily_run_labels=daily_run_labels,
                           daily_run_data=daily_run_data,
                           recent_runs=recent_runs)
