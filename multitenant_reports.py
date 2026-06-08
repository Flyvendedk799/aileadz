"""
Multi-Tenant Reports System
Enterprise-grade analytics and reporting for company workspaces
Replaces the single-tenant reports.py with company-scoped analytics
"""

from flask import Blueprint, render_template, session, redirect, url_for, flash, current_app, request, jsonify
from auth_decorators import require_company
import MySQLdb.cursors
from collections import defaultdict
import datetime
import json
import re
from datetime import timedelta

# Max age (days) of the newest company_analytics snapshot row for the
# pre-aggregated daily-usage series to be considered "fresh enough" to serve
# instead of the live GROUP BY recompute. The nightly rollup writes yesterday's
# row, so a snapshot up to 2 days old still means the job is running on cadence;
# anything staler means the substrate is cold and we recompute live.
_ANALYTICS_SNAPSHOT_MAX_AGE_DAYS = 2


def _daily_usage_from_snapshot(cur, company_id, days_back=90):
    """Serve the 90-day daily chatbot-usage series from the pre-aggregated
    company_analytics snapshot when a fresh-enough one exists.

    Returns a ``{ 'YYYY-MM-DD': count }`` dict mirroring the live GROUP BY in
    reports(), or ``None`` when no fresh snapshot is available (in which case the
    caller falls back to the live recompute). NEVER raises — a snapshot read
    problem must transparently degrade to live recompute, never break the page.

    "Fresh enough" = the most recent snapshot row for this company is no older
    than ``_ANALYTICS_SNAPSHOT_MAX_AGE_DAYS`` (i.e. the nightly rollup job is
    running on cadence). This is a count-only, company-scoped daily aggregate
    (no people-level data), so it carries no k-anon exposure.
    """
    try:
        cur.execute(
            """SELECT MAX(date) AS max_date
               FROM company_analytics WHERE company_id = %s""",
            (company_id,),
        )
        row = cur.fetchone() or {}
        max_date = row.get('max_date') if isinstance(row, dict) else (row[0] if row else None)
        if not max_date:
            return None  # table never written for this company -> recompute live
        today = datetime.date.today()
        if hasattr(max_date, 'date'):
            max_date = max_date.date()
        age = (today - max_date).days
        if age > _ANALYTICS_SNAPSHOT_MAX_AGE_DAYS:
            return None  # snapshot is stale -> the job isn't running; recompute live
        cur.execute(
            """SELECT date AS day, total_queries AS cnt
               FROM company_analytics
               WHERE company_id = %s
                 AND date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
               ORDER BY date ASC""",
            (company_id, days_back),
        )
        usage = {}
        for r in cur.fetchall() or []:
            day = r['day'] if isinstance(r, dict) else r[0]
            cnt = r['cnt'] if isinstance(r, dict) else r[1]
            label = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
            usage[label] = int(cnt or 0)
        return usage
    except Exception:
        return None


def create_multitenant_reports_blueprint():
    multitenant_reports_bp = Blueprint('multitenant_reports', __name__, template_folder='templates')

    def require_company_access():
        """Ensure user has access to company reports"""
        if 'user' not in session:
            flash("Please log in to view reports.", "danger")
            return redirect(url_for('auth.login'))
        
        if not session.get('company_id'):
            flash("You must be part of a company to view reports.", "danger")
            return redirect(url_for('auth.login'))
        return None

    def get_company_context():
        """Get current user's company context"""
        if 'user' not in session:
            return None
        
        conn = current_app.mysql.connection
        if not conn:
            return None

        try:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT c.*, cu.role as user_role, cu.department, cu.permissions
                FROM companies c
                JOIN company_users cu ON c.id = cu.company_id
                JOIN users u ON cu.user_id = u.id
                WHERE u.username = %s AND cu.status = 'active'
            """, (session['user'],))
            result = cur.fetchone()
            cur.close()
            return result
        except Exception as e:
            current_app.logger.error(f"Error getting company context: {e}")
            return None

    @multitenant_reports_bp.route('')
    @multitenant_reports_bp.route('/')
    @require_company
    def reports():
        """
        Company-specific reports dashboard
        Shows analytics scoped to the user's company only
        """
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        conn = current_app.mysql.connection
        if not conn:
            flash("Database connection not available.", "danger")
            return redirect(url_for('auth.login'))

        # Initialize analytics variables
        total_chatbot_queries = 0
        daily_chatbot_usage = defaultdict(int)
        hourly_usage_pattern = defaultdict(int)
        query_type_distribution = defaultdict(int)
        intent_distribution = defaultdict(int)
        category_distribution = defaultdict(int)
        user_locations = defaultdict(int)
        api_errors = 0
        no_results_queries = 0
        course_views = defaultdict(int)
        
        avg_conversation_length = 0
        total_conversations = 0
        conversion_rate = 0
        avg_response_time = 0
        
        frequent_questions = []
        recent_conversations = []
        recent_orders = []
        pending_orders_count = 0
        completed_orders_count = 0
        total_revenue = 0
        
        try:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cid = company['id']

            # ──────────────────────────────────────────────────────────────
            # Company chatbot analytics — REAL SQL GROUP BY aggregation.
            #
            # Previously this route pulled up to 1000 rows into Python and
            # categorised each one in a loop (string-scanning query_text,
            # fabricating session ids like "company_X_session_<date>_<hour>_
            # <dept>", and a hardcoded avg_response_time=250). It is now
            # entirely DB-side: COUNT / AVG / GROUP BY scoped to this tenant,
            # using the real columns (session_id, query_type,
            # tool_results_count, response_time_ms). Output shapes are kept
            # stable for company_reports.html.
            # ──────────────────────────────────────────────────────────────

            # 1) Headline counts + real avg response time (mirrors
            #    admin_reports.py:87 — AVG(response_time_ms)).
            cur.execute("""
                SELECT
                    COUNT(*) AS total_queries,
                    COUNT(DISTINCT session_id) AS total_sessions,
                    AVG(response_time_ms) AS avg_rt,
                    SUM(CASE WHEN COALESCE(tool_results_count, 0) = 0 THEN 1 ELSE 0 END) AS no_results
                FROM chatbot_interactions
                WHERE company_id = %s
            """, (cid,))
            _hdr = cur.fetchone() or {}
            total_chatbot_queries = int(_hdr.get('total_queries') or 0)
            total_conversations = int(_hdr.get('total_sessions') or 0)
            # Real avg response time scoped to the company (no more 250 ms placeholder).
            avg_response_time = round(_hdr.get('avg_rt') or 0)
            no_results_queries = int(_hdr.get('no_results') or 0)

            # Avg conversation length = queries / distinct sessions (DB-side counts).
            if total_conversations:
                avg_conversation_length = total_chatbot_queries / total_conversations

            # 2) Daily activity (last 90 days). Prefer the pre-aggregated
            #    company_analytics snapshot (written nightly by the
            #    company_analytics_rollup scheduler job) when a fresh-enough one
            #    exists — this is the heavy per-request GROUP BY DATE the rollup
            #    exists to displace. Falls back to the live recompute below when
            #    the snapshot is absent/stale so the page is never wrong.
            _snapshot_usage = _daily_usage_from_snapshot(cur, cid, days_back=90)
            if _snapshot_usage is not None:
                for label, cnt in _snapshot_usage.items():
                    daily_chatbot_usage[label] = cnt
            else:
                cur.execute("""
                    SELECT DATE(created_at) AS day, COUNT(*) AS cnt
                    FROM chatbot_interactions
                    WHERE company_id = %s
                      AND created_at >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                    GROUP BY DATE(created_at)
                    ORDER BY day ASC
                """, (cid,))
                for r in cur.fetchall():
                    day = r['day']
                    label = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
                    daily_chatbot_usage[label] = int(r['cnt'] or 0)

            # 3) Query-type distribution — GROUP BY query_type (real column).
            cur.execute("""
                SELECT COALESCE(NULLIF(query_type, ''), 'general') AS qtype, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s
                GROUP BY COALESCE(NULLIF(query_type, ''), 'general')
                ORDER BY cnt DESC
                LIMIT 12
            """, (cid,))
            for r in cur.fetchall():
                query_type_distribution[r['qtype']] = int(r['cnt'] or 0)
                intent_distribution[r['qtype']] = int(r['cnt'] or 0)

            # 4) Category distribution — GROUP BY category (real column).
            cur.execute("""
                SELECT COALESCE(NULLIF(category, ''), 'Andet') AS cat, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s
                GROUP BY COALESCE(NULLIF(category, ''), 'Andet')
                ORDER BY cnt DESC
                LIMIT 12
            """, (cid,))
            for r in cur.fetchall():
                category_distribution[r['cat']] = int(r['cnt'] or 0)

            # 5) User locations — GROUP BY user_location (real column).
            cur.execute("""
                SELECT COALESCE(NULLIF(user_location, ''), 'Ukendt') AS loc, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s AND user_location IS NOT NULL AND user_location <> ''
                GROUP BY user_location
                ORDER BY cnt DESC
                LIMIT 10
            """, (cid,))
            for r in cur.fetchall():
                user_locations[r['loc']] = int(r['cnt'] or 0)

            # 6) Course interest — GROUP BY product_handle on the orders the
            #    chatbot is attributed to (replaces the regex scrape of
            #    response_text). 'views' here = attributed order interest.
            cur.execute("""
                SELECT product_handle AS handle, COUNT(*) AS cnt
                FROM course_orders
                WHERE company_id = %s
                  AND product_handle IS NOT NULL AND product_handle <> ''
                GROUP BY product_handle
                ORDER BY cnt DESC
                LIMIT 10
            """, (cid,))
            for r in cur.fetchall():
                course_views[r['handle']] = int(r['cnt'] or 0)

            # 7) Frequent questions (top 10) — GROUP BY query_text, join back
            #    by MIN(id) for a representative response (no Python grouping).
            cur.execute("""
                SELECT t.query_text AS text, t.cnt AS count, ci.response_text AS top_response
                FROM (
                    SELECT query_text, COUNT(*) AS cnt, MIN(id) AS first_id
                    FROM chatbot_interactions
                    WHERE company_id = %s AND query_text IS NOT NULL AND query_text <> ''
                    GROUP BY query_text
                    ORDER BY cnt DESC
                    LIMIT 10
                ) t
                JOIN chatbot_interactions ci ON ci.id = t.first_id
                ORDER BY t.cnt DESC
            """, (cid,))
            for r in cur.fetchall():
                q_text = r.get('text') or ''
                top_res = r.get('top_response') or ''
                frequent_questions.append({
                    'text': (q_text[:100] + '...') if len(q_text) > 100 else q_text,
                    'count': int(r.get('count') or 0),
                    'top_response': (top_res[:200] + '...') if len(top_res) > 200 else top_res,
                })

            # 8) Recent conversations (top 10 real sessions) — GROUP BY
            #    session_id with real start/duration/depth from the DB.
            cur.execute("""
                SELECT ci.session_id,
                       MIN(ci.created_at) AS start_time,
                       MAX(ci.created_at) AS end_time,
                       COUNT(*) AS query_count,
                       MAX(cu.department) AS department
                FROM chatbot_interactions ci
                LEFT JOIN users u ON ci.username = u.username
                LEFT JOIN company_users cu ON u.id = cu.user_id AND cu.company_id = %s
                WHERE ci.company_id = %s
                  AND ci.session_id IS NOT NULL AND ci.session_id <> ''
                GROUP BY ci.session_id
                ORDER BY start_time DESC
                LIMIT 10
            """, (cid, cid))
            for r in cur.fetchall():
                st = r.get('start_time')
                en = r.get('end_time')
                duration = 0
                if isinstance(st, datetime.datetime) and isinstance(en, datetime.datetime):
                    duration = int((en - st).total_seconds() / 60)
                recent_conversations.append({
                    'session_id': str(r.get('session_id') or '')[:12],
                    'start_time': (
                        st.strftime('%Y-%m-%d %H:%M:%S')
                        if isinstance(st, datetime.datetime) else 'N/A'
                    ),
                    'duration': duration,
                    'query_count': int(r.get('query_count') or 0),
                    'topics': [],
                    'department': r.get('department') or 'Unknown',
                })

            # 2) Fetch company-specific orders
            try:
                cur.execute("""
                    SELECT 
                        co.order_id, co.product_title, co.product_handle, co.price, co.status,
                        co.created_at, co.updated_at, co.user_email, co.user_name, co.user_phone,
                        co.variant_date, co.variant_location, co.department, co.completion_status,
                        co.completion_date, co.approved_by,
                        u.username, cu.job_title, cu.department as emp_department
                    FROM course_orders co
                    LEFT JOIN users u ON co.user_id = u.id
                    LEFT JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
                    WHERE co.company_id = %s
                    ORDER BY co.created_at DESC
                    LIMIT 50
                """, (company['id'],))
                orders = cur.fetchall()
                
                for od in orders:
                    if od['status'] == 'pending':
                        pending_orders_count += 1
                    elif od['status'] == 'completed' or od['completion_status'] == 'completed':
                        completed_orders_count += 1
                        if od['price']:
                            total_revenue += float(od['price'])
                    
                    recent_orders.append({
                        'order_id': od['order_id'] or 'N/A',
                        'order_id_short': od['order_id'][:8] if od['order_id'] else 'N/A',
                        'product_title': od['product_title'] or 'Unknown Course',
                        'product_handle': od.get('product_handle', ''),
                        'price': od['price'] or '0',
                        'status': od.get('status', 'unknown'),
                        'completion_status': od.get('completion_status', 'not_started'),
                        'created_at': (
                            od['created_at'].strftime('%Y-%m-%d %H:%M')
                            if od['created_at'] else 'N/A'
                        ),
                        'completion_date': (
                            od['completion_date'].strftime('%Y-%m-%d %H:%M')
                            if od.get('completion_date') else 'N/A'
                        ),
                        'user_email': od.get('user_email', 'N/A'),
                        'user_name': od.get('user_name') or od.get('username', 'N/A'),
                        'department': od.get('department') or od.get('emp_department', 'N/A'),
                        'job_title': od.get('job_title', 'N/A'),
                        'variant_date': od.get('variant_date', ''),
                        'variant_location': od.get('variant_location', ''),
                        'category': 'Company Training'
                    })
                    
            except Exception as e:
                current_app.logger.error(f"Error fetching company orders: {e}")
            
            # Calculate conversion rates per course
            course_conversion_rates = {}
            if recent_orders:
                course_orders = defaultdict(int)
                for order in recent_orders:
                    handle = order.get('product_handle', '')
                    if handle:
                        course_orders[handle] += 1
                
                for handle, views in course_views.items():
                    orders = course_orders.get(handle, 0)
                    if views > 0:
                        conversion_rate = (orders / views) * 100
                        course_conversion_rates[handle] = round(conversion_rate, 1)
                    else:
                        course_conversion_rates[handle] = 0.0
            
            # Global conversion rate (total orders vs total queries)
            if total_chatbot_queries > 0:
                conversion_rate = (len(recent_orders) / total_chatbot_queries) * 100
            
            # Get company-specific analytics. The employee counts come from
            # company_users alone; the avg interaction quality from
            # chatbot_interactions alone. Previously these were a single query
            # whose `LEFT JOIN chatbot_interactions ci ON ci.company_id =
            # cu.company_id` produced a users×interactions cross product (e.g.
            # 100 users × 10k interactions = 1M intermediate rows) before
            # aggregating. Splitting them yields identical values with no blow-up.
            cur.execute("""
                SELECT
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_employees,
                    COUNT(DISTINCT cu.department) as total_departments,
                    COUNT(DISTINCT CASE WHEN cu.last_chatbot_interaction >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as active_users_30d
                FROM company_users cu
                WHERE cu.company_id = %s
            """, (company['id'],))
            company_stats = cur.fetchone() or {}

            cur.execute("""
                SELECT COALESCE(AVG(interaction_quality_score), 0) AS avg_interaction_quality
                FROM chatbot_interactions
                WHERE company_id = %s
            """, (company['id'],))
            _q_row = cur.fetchone()
            company_stats['avg_interaction_quality'] = (
                _q_row.get('avg_interaction_quality', 0) if isinstance(_q_row, dict) else 0
            )

            cur.close()
            
        except Exception as e:
            current_app.logger.error(f"Error fetching company analytics: {e}")
            # Provide fallback data
            if not daily_chatbot_usage:
                daily_chatbot_usage['2024-01-01'] = 0
            if not query_type_distribution:
                query_type_distribution['general'] = 0
            if not category_distribution:
                category_distribution['general'] = 0
            if not user_locations:
                user_locations['unknown'] = 0
            course_conversion_rates = {}
            company_stats = {
                'total_employees': 0,
                'active_employees': 0,
                'total_departments': 0,
                'avg_interaction_quality': 0,
                'active_users_30d': 0
            }
        
        # Prepare popular courses with conversion rates
        popular_courses_list = []
        sorted_courses = sorted(course_views.items(), key=lambda x: x[1], reverse=True)[:10]
        for handle, views in sorted_courses:
            conversion_rate_for_course = course_conversion_rates.get(handle, 0.0)
            popular_courses_list.append({
                'handle': handle,
                'views': views,
                'conversion_rate': conversion_rate_for_course
            })
        
        # Daily usage data for charts
        sorted_dates = sorted(daily_chatbot_usage.keys())
        daily_usage_data = [daily_chatbot_usage[d] for d in sorted_dates]
        
        # Calculate engagement metrics
        if company_stats['total_employees'] > 0:
            employee_engagement_rate = round((company_stats['active_users_30d'] / company_stats['total_employees']) * 100, 1)
        else:
            employee_engagement_rate = 0
        
        # C5: surface AI insights (generated by insights_engine) on this page too.
        recent_insights = []
        try:
            _icur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            _icur.execute("""
                SELECT id, insight_type, title, body, severity, generated_at
                FROM company_insights
                WHERE company_id = %s AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY generated_at DESC LIMIT 8
            """, (company['id'],))
            recent_insights = _icur.fetchall() or []
            _icur.close()
        except Exception:
            recent_insights = []

        return render_template(
            'fm/company_reports.html',
            company=company,
            company_stats=company_stats,
            recent_insights=recent_insights,
            
            # Chatbot Analytics
            total_chatbot_queries=total_chatbot_queries,
            avg_conversation_length=round(avg_conversation_length, 1),
            conversion_rate=round(conversion_rate, 2),
            avg_chatbot_response_time=avg_response_time,
            chatbot_api_errors=api_errors,
            no_results_queries=no_results_queries,
            employee_engagement_rate=employee_engagement_rate,
            
            # Chart Data
            daily_chatbot_query_labels=sorted_dates,
            daily_chatbot_query_data=daily_usage_data,
            hourly_usage_pattern=dict(hourly_usage_pattern),
            
            # Distributions
            query_type_distribution=dict(query_type_distribution),
            intent_distribution=dict(intent_distribution),
            category_distribution=dict(category_distribution),
            
            # Course Analytics
            popular_courses=popular_courses_list,
            frequent_questions=frequent_questions,
            
            # Location & Engagement
            user_locations=dict(user_locations),
            most_common_questions=[q['text'] for q in frequent_questions[:3]],
            
            # Conversations
            recent_conversations=recent_conversations,
            total_conversations=total_conversations,
            
            # Orders & Revenue
            recent_orders=recent_orders,
            pending_orders_count=pending_orders_count,
            completed_orders_count=completed_orders_count,
            total_revenue=total_revenue
        )

    @multitenant_reports_bp.route('/order/<order_id>')
    @require_company
    def order_detail(order_id):
        """
        Company-scoped order details
        """
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        conn = current_app.mysql.connection
        if not conn:
            flash("Database connection not available.", "danger")
            return redirect(url_for('multitenant_reports.reports'))

        try:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT co.*, u.username, cu.department, cu.job_title
                FROM course_orders co
                LEFT JOIN users u ON co.user_id = u.id
                LEFT JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
                WHERE co.order_id = %s AND co.company_id = %s
            """, (order_id, company['id']))
            order = cur.fetchone()
            cur.close()
            
            if not order:
                flash("Order not found or not accessible.", "danger")
                return redirect(url_for('multitenant_reports.reports'))
            
            return render_template('fm/mt_order_detail.html',
                                 company=company, order=order)
        except Exception as e:
            current_app.logger.error(f"Error fetching order details: {e}")
            flash("Error loading order details.", "danger")
            return redirect(url_for('multitenant_reports.reports'))

    @multitenant_reports_bp.route('/order/<order_id>/update', methods=['POST'])
    @require_company
    def update_order_status(order_id):
        """
        Update order status (company-scoped)
        Only HR managers and company admins can update orders
        """
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404
        
        # Check permissions
        if company['user_role'] not in ['company_admin', 'hr_manager']:
            return jsonify({'success': False, 'message': 'Insufficient permissions'}), 403
        
        if request.is_json:
            new_status = request.json.get('status')
        else:
            new_status = request.form.get('status')
        
        if not new_status:
            return jsonify({'success': False, 'message': 'No status provided'}), 400
        
        valid_statuses = ['pending', 'processing', 'confirmed', 'cancelled', 'completed']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'message': 'Invalid status'}), 400
        
        conn = current_app.mysql.connection
        if not conn:
            return jsonify({'success': False, 'message': 'Database connection error'}), 500

        try:
            cur = conn.cursor()
            
            # Verify order belongs to company
            cur.execute("""
                SELECT order_id FROM course_orders
                WHERE order_id = %s AND company_id = %s
            """, (order_id, company['id']))
            
            if not cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': 'Order not found'}), 404
            
            # Update order status
            cur.execute("""
                UPDATE course_orders
                SET status = %s, updated_at = NOW()
                WHERE order_id = %s AND company_id = %s
            """, (new_status, order_id, company['id']))
            
            if cur.rowcount == 0:
                cur.close()
                return jsonify({'success': False, 'message': 'No rows updated'}), 400
            
            # Log the action
            cur.execute("""
                INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                VALUES (%s, %s, 'order_status_updated', 'order', %s, %s)
            """, (
                company['id'], session.get('user_id'), order_id,
                json.dumps({'old_status': 'unknown', 'new_status': new_status, 'updated_by': session.get('user')})
            ))
            
            conn.commit()
            cur.close()

            current_app.logger.info(f"Order {order_id} status updated to {new_status} by {session.get('user')} in company {company['company_name']}")
            
            return jsonify({
                'success': True,
                'message': f'Order status updated to {new_status}',
                'new_status': new_status
            })
            
        except Exception as e:
            current_app.logger.error(f"Error updating order status: {e}")
            if 'cur' in locals():
                cur.close()
            return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500

    @multitenant_reports_bp.route('/analytics/export')
    @require_company
    def export_analytics():
        """
        Export company-specific analytics data as JSON
        """
        company = get_company_context()
        if not company:
            return jsonify({'error': 'Company not found'}), 404
        
        # Check permissions
        if company['user_role'] not in ['company_admin', 'hr_manager', 'department_head']:
            return jsonify({'error': 'Insufficient permissions'}), 403
        
        analytics_data = {
            'export_date': datetime.datetime.now().isoformat(),
            'company_id': company['id'],
            'company_name': company['company_name'],
            'company_slug': company.get('company_slug'),
            'exported_by': session.get('user'),
            'metrics': {},
            'time_series': {},
            'distributions': {}
        }

        # Populate the export with the SAME real, company-scoped aggregates the
        # reports page computes (was previously an empty {} stub). All numbers
        # come from REAL SQL GROUP BY / COUNT / AVG / SUM — never a placeholder.
        try:
            conn = current_app.mysql.connection
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            cid = company['id']

            # --- Headline metrics (mirror the reports() route) ---
            cur.execute("""
                SELECT
                    COUNT(*) AS total_queries,
                    COUNT(DISTINCT session_id) AS total_sessions,
                    AVG(response_time_ms) AS avg_rt,
                    SUM(CASE WHEN COALESCE(tool_results_count, 0) = 0 THEN 1 ELSE 0 END) AS no_results,
                    AVG(interaction_quality_score) AS avg_quality
                FROM chatbot_interactions
                WHERE company_id = %s
            """, (cid,))
            _h = cur.fetchone() or {}
            total_queries = int(_h.get('total_queries') or 0)
            total_sessions = int(_h.get('total_sessions') or 0)

            cur.execute("""
                SELECT COUNT(*) AS total_orders,
                       COUNT(CASE WHEN status = 'pending' THEN 1 END) AS pending_orders,
                       COUNT(CASE WHEN status = 'completed' OR completion_status = 'completed' THEN 1 END) AS completed_orders,
                       COALESCE(SUM(CASE WHEN status = 'completed' OR completion_status = 'completed' THEN price END), 0) AS total_revenue
                FROM course_orders
                WHERE company_id = %s
            """, (cid,))
            _o = cur.fetchone() or {}
            total_orders = int(_o.get('total_orders') or 0)

            analytics_data['metrics'] = {
                'total_chatbot_queries': total_queries,
                'total_conversations': total_sessions,
                'avg_conversation_length': round(total_queries / total_sessions, 1) if total_sessions else 0,
                'avg_response_time_ms': round(_h.get('avg_rt') or 0),
                'avg_interaction_quality': round(float(_h.get('avg_quality') or 0), 2),
                'no_results_queries': int(_h.get('no_results') or 0),
                'total_orders': total_orders,
                'pending_orders': int(_o.get('pending_orders') or 0),
                'completed_orders': int(_o.get('completed_orders') or 0),
                'total_revenue': float(_o.get('total_revenue') or 0),
                'conversion_rate': round(total_orders / total_queries * 100, 2) if total_queries else 0,
            }

            # --- Time series: daily volume (last 90 days) ---
            cur.execute("""
                SELECT DATE(created_at) AS day, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s
                  AND created_at >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                GROUP BY DATE(created_at)
                ORDER BY day ASC
            """, (cid,))
            daily = {}
            for r in cur.fetchall():
                day = r['day']
                label = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
                daily[label] = int(r['cnt'] or 0)
            analytics_data['time_series']['daily_chatbot_queries'] = daily

            # --- Distributions: query type + category ---
            cur.execute("""
                SELECT COALESCE(NULLIF(query_type, ''), 'general') AS qtype, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s
                GROUP BY COALESCE(NULLIF(query_type, ''), 'general')
                ORDER BY cnt DESC LIMIT 12
            """, (cid,))
            analytics_data['distributions']['query_type'] = {
                r['qtype']: int(r['cnt'] or 0) for r in cur.fetchall()
            }

            cur.execute("""
                SELECT COALESCE(NULLIF(category, ''), 'Andet') AS cat, COUNT(*) AS cnt
                FROM chatbot_interactions
                WHERE company_id = %s
                GROUP BY COALESCE(NULLIF(category, ''), 'Andet')
                ORDER BY cnt DESC LIMIT 12
            """, (cid,))
            analytics_data['distributions']['category'] = {
                r['cat']: int(r['cnt'] or 0) for r in cur.fetchall()
            }

            cur.close()
        except Exception as e:
            current_app.logger.error(f"Error building analytics export: {e}")

        response = jsonify(analytics_data)
        response.headers['Content-Disposition'] = (
            f"attachment; filename=company_analytics_{company['company_slug']}_{datetime.datetime.now().strftime('%Y%m%d')}.json"
        )
        return response

    @multitenant_reports_bp.route('/department/<department_name>')
    @require_company
    def department_analytics(department_name):
        """
        Department-specific analytics within the company
        """
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        # Check if user can view this department
        user_role = company['user_role']
        user_department = company['department']
        
        if user_role not in ['company_admin', 'hr_manager'] and user_department != department_name:
            flash("You don't have permission to view this department's analytics.", "danger")
            return redirect(url_for('multitenant_reports.reports'))
        
        conn = current_app.mysql.connection
        if not conn:
            flash("Database connection error.", "danger")
            return redirect(url_for('multitenant_reports.reports'))

        try:
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            
            # Get department-specific metrics
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_employees,
                    COUNT(DISTINCT co.id) as total_course_orders,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as total_investment,
                    COUNT(DISTINCT ci.id) as total_chatbot_interactions,
                    COALESCE(AVG(ci.interaction_quality_score), 0) as avg_interaction_quality
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN chatbot_interactions ci ON ci.company_id = cu.company_id AND ci.username = (
                    SELECT u.username FROM users u WHERE u.id = cu.user_id
                )
                WHERE cu.company_id = %s AND cu.department = %s
            """, (company['id'], department_name))
            
            dept_stats = cur.fetchone()
            
            # Get department employees
            cur.execute("""
                SELECT 
                    u.username, cu.job_title, cu.role, cu.hire_date,
                    COUNT(DISTINCT co.id) as courses_enrolled,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as courses_completed,
                    cu.total_chatbot_queries, cu.last_chatbot_interaction
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                WHERE cu.company_id = %s AND cu.department = %s AND cu.status = 'active'
                GROUP BY cu.user_id, u.username, cu.job_title, cu.role, cu.hire_date, cu.total_chatbot_queries, cu.last_chatbot_interaction
                ORDER BY courses_completed DESC, cu.total_chatbot_queries DESC
            """, (company['id'], department_name))
            
            dept_employees = cur.fetchall()
            
            cur.close()
            
            return render_template('fm/department_analytics.html',
                                 company=company,
                                 department_name=department_name,
                                 dept_stats=dept_stats,
                                 dept_employees=dept_employees)
            
        except Exception as e:
            current_app.logger.error(f"Error loading department analytics: {e}")
            flash("Error loading department analytics.", "danger")
            return redirect(url_for('multitenant_reports.reports'))

    return multitenant_reports_bp

# Create the blueprint instance
multitenant_reports_bp = create_multitenant_reports_blueprint()
