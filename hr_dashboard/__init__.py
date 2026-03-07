"""
HR Dashboard - Advanced Employee Management & Analytics
Enterprise-grade HR tools for managing employee learning and development
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
from datetime import datetime, timedelta, date
import json
import calendar
from collections import defaultdict
import csv
import io

def create_hr_dashboard_blueprint():
    hr_dashboard_bp = Blueprint('hr_dashboard', __name__, template_folder='templates')

    def require_hr_access():
        """Ensure user has HR management permissions"""
        if 'user' not in session:
            flash("Please log in to access HR features.", "danger")
            return redirect(url_for('auth.login'))
        
        if not session.get('company_id') or session.get('company_role') not in ['company_admin', 'hr_manager', 'department_head']:
            flash("You don't have permission to access HR features.", "danger")
            return redirect(url_for('dashboard.dashboard'))
        return None

    def get_company_context():
        """Get current user's company context"""
        if 'user' not in session:
            return None
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
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

    @hr_dashboard_bp.route('/')
    def dashboard():
        """Main HR Dashboard with key metrics and insights"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Key HR Metrics
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_employees,
                    COUNT(DISTINCT CASE WHEN cu.last_login >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as active_last_30_days,
                    COUNT(DISTINCT CASE WHEN cu.added_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as new_hires_30_days,
                    COUNT(DISTINCT cu.department) as total_departments,
                    AVG(DATEDIFF(NOW(), cu.hire_date)) as avg_tenure_days
                FROM company_users cu
                WHERE cu.company_id = %s
            """, (company['id'],))
            hr_metrics = cur.fetchone()
            
            # Learning & Development Metrics
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT co.id) as total_enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'in_progress' THEN co.id END) as in_progress_courses,
                    COUNT(DISTINCT CASE WHEN co.completion_deadline < NOW() AND co.completion_status != 'completed' THEN co.id END) as overdue_courses,
                    COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as total_training_investment,
                    COUNT(DISTINCT co.user_id) as employees_with_training
                FROM course_orders co
                WHERE co.company_id = %s
            """, (company['id'],))
            learning_metrics = cur.fetchone()
            
            # Company-specific Orders
            cur.execute("""
                SELECT 
                    co.order_id, co.product_title, co.product_handle, co.price, co.status,
                    co.created_at, co.updated_at, co.user_email, co.user_name, co.user_phone,
                    co.variant_date, co.variant_location, co.completion_status,
                    u.username, cu.department, cu.job_title
                FROM course_orders co
                JOIN users u ON co.user_id = u.id
                JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
                WHERE co.company_id = %s
                ORDER BY co.created_at DESC
                LIMIT 50
            """, (company['id'],))
            company_orders = cur.fetchall()
            
            # Process orders for display
            recent_orders = []
            pending_orders_count = 0
            completed_orders_count = 0
            total_revenue = 0
            
            for order in company_orders:
                if order['status'] == 'pending':
                    pending_orders_count += 1
                elif order['status'] == 'completed':
                    completed_orders_count += 1
                    if order['price']:
                        total_revenue += float(order['price'])
                
                recent_orders.append({
                    'order_id': order['order_id'] or 'N/A',
                    'order_id_short': order['order_id'][:8] if order['order_id'] else 'N/A',
                    'product_title': order['product_title'] or 'Unknown Course',
                    'product_handle': order.get('product_handle', ''),
                    'price': order['price'] or '0',
                    'status': order.get('status', 'unknown'),
                    'completion_status': order.get('completion_status', 'not_started'),
                    'created_at': (
                        order['created_at'].strftime('%Y-%m-%d %H:%M')
                        if order['created_at'] else 'N/A'
                    ),
                    'updated_at': (
                        order['updated_at'].strftime('%Y-%m-%d %H:%M')
                        if order.get('updated_at') else 'N/A'
                    ),
                    'user_email': order.get('user_email', 'N/A'),
                    'user_name': order.get('user_name', 'N/A'),
                    'user_phone': order.get('user_phone', 'N/A'),
                    'username': order.get('username', 'N/A'),
                    'department': order.get('department', 'N/A'),
                    'job_title': order.get('job_title', 'N/A'),
                    'variant_date': order.get('variant_date', ''),
                    'variant_location': order.get('variant_location', ''),
                    'category': 'Company Training'
                })
            
            # Company-specific Chatbot Interactions
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT ci.id) as total_chatbot_interactions,
                    COUNT(DISTINCT ci.username) as employees_using_chatbot,
                    COALESCE(AVG(ci.interaction_quality_score), 0) as avg_interaction_quality,
                    COUNT(DISTINCT CASE WHEN ci.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) THEN ci.username END) as active_users_7_days
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id
                WHERE cu.company_id = %s
            """, (company['id'],))
            engagement_metrics = cur.fetchone()
            
            # Department Performance
            cur.execute("""
                SELECT 
                    cu.department,
                    COUNT(DISTINCT cu.user_id) as employee_count,
                    COUNT(DISTINCT co.id) as total_enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    ROUND(
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                        NULLIF(COUNT(DISTINCT co.id), 0), 1
                    ) as completion_rate,
                    COUNT(DISTINCT CASE WHEN cu.last_chatbot_interaction >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as active_chatbot_users,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                WHERE cu.company_id = %s AND cu.status = 'active'
                GROUP BY cu.department
                ORDER BY employee_count DESC
            """, (company['id'],))
            department_performance = cur.fetchall()
            
            # Recent Activity & Alerts
            cur.execute("""
                SELECT 
                    'course_completion' as alert_type,
                    CONCAT(u.username, ' completed ', co.product_title) as message,
                    co.completion_date as alert_time,
                    'success' as alert_level
                FROM course_orders co
                JOIN users u ON co.user_id = u.id
                WHERE co.company_id = %s AND co.completion_status = 'completed' 
                AND co.completion_date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                
                UNION ALL
                
                SELECT 
                    'overdue_course' as alert_type,
                    CONCAT(u.username, ' has overdue course: ', co.product_title) as message,
                    co.completion_deadline as alert_time,
                    'warning' as alert_level
                FROM course_orders co
                JOIN users u ON co.user_id = u.id
                WHERE co.company_id = %s AND co.completion_deadline < NOW() 
                AND co.completion_status NOT IN ('completed', 'cancelled')
                
                UNION ALL
                
                SELECT 
                    'new_hire' as alert_type,
                    CONCAT('New employee added: ', u.username, ' (', cu.department, ')') as message,
                    cu.added_at as alert_time,
                    'info' as alert_level
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                WHERE cu.company_id = %s AND cu.added_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
                
                ORDER BY alert_time DESC
                LIMIT 10
            """, (company['id'], company['id'], company['id']))
            recent_alerts = cur.fetchall()
            
            # Top Performers
            cur.execute("""
                SELECT 
                    u.username, cu.department, cu.job_title,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as courses_completed,
                    COUNT(DISTINCT co.id) as courses_enrolled,
                    cu.total_chatbot_queries,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress,
                    cu.last_chatbot_interaction
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                WHERE cu.company_id = %s AND cu.status = 'active'
                GROUP BY cu.user_id, u.username, cu.department, cu.job_title, cu.total_chatbot_queries, cu.last_chatbot_interaction
                HAVING courses_completed > 0 OR cu.total_chatbot_queries > 0
                ORDER BY courses_completed DESC, cu.total_chatbot_queries DESC
                LIMIT 10
            """, (company['id'],))
            top_performers = cur.fetchall()
            
            # Learning Paths Progress
            cur.execute("""
                SELECT 
                    lp.path_name, lp.path_category,
                    COUNT(DISTINCT elp.user_id) as enrolled_employees,
                    COUNT(DISTINCT CASE WHEN elp.completed_at IS NOT NULL THEN elp.user_id END) as completed_employees,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress
                FROM learning_paths lp
                LEFT JOIN employee_learning_progress elp ON lp.id = elp.learning_path_id
                WHERE lp.company_id = %s AND lp.is_active = 1
                GROUP BY lp.id, lp.path_name, lp.path_category
                ORDER BY enrolled_employees DESC
                LIMIT 5
            """, (company['id'],))
            learning_paths_progress = cur.fetchall()
            
            # Phase 2.2: Pending approvals count for dashboard
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM order_approvals
                WHERE company_id = %s AND status = 'pending'
            """, (company['id'],))
            pending_approvals_count = cur.fetchone()['cnt'] or 0

            # Phase 2.3: Budget summary for dashboard
            import datetime as _dt
            _fy = _dt.datetime.now().year
            cur.execute("""
                SELECT COALESCE(SUM(annual_budget), 0) AS total_budget,
                       COALESCE(SUM(spent), 0) AS total_spent
                FROM department_budgets
                WHERE company_id = %s AND fiscal_year = %s
            """, (company['id'], _fy))
            budget_summary = cur.fetchone()
            total_training_budget = float(budget_summary['total_budget'] or 0)
            total_budget_spent = float(budget_summary['total_spent'] or 0)
            budget_remaining = total_training_budget - total_budget_spent
            budget_utilization = round((total_budget_spent / total_training_budget * 100), 1) if total_training_budget > 0 else 0

            # Trending Topics (Phase 1 quick win) - what employees are searching for
            cur.execute("""
                SELECT COALESCE(ci.query_type, 'general') AS topic,
                       COUNT(*) AS cnt
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id
                WHERE cu.company_id = %s
                  AND ci.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                GROUP BY ci.query_type
                ORDER BY cnt DESC
                LIMIT 8
            """, (company['id'],))
            trending_topics = cur.fetchall()

            # Tool usage within company
            cur.execute("""
                SELECT ci.tools_used
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id
                WHERE cu.company_id = %s
                  AND ci.tools_used IS NOT NULL AND ci.tools_used != ''
                  AND ci.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            """, (company['id'],))
            _company_tool_usage = {}
            for r in cur.fetchall():
                for tool in r['tools_used'].split(','):
                    tool = tool.strip()
                    if tool:
                        _company_tool_usage[tool] = _company_tool_usage.get(tool, 0) + 1
            company_tool_usage = dict(sorted(_company_tool_usage.items(), key=lambda x: x[1], reverse=True)[:6])

            # Avg feedback for company
            cur.execute("""
                SELECT AVG(ci.feedback_rating) AS avg_fb,
                       COUNT(CASE WHEN ci.feedback_rating > 0 THEN 1 END) AS fb_count
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id
                WHERE cu.company_id = %s
                  AND ci.feedback_rating IS NOT NULL AND ci.feedback_rating > 0
            """, (company['id'],))
            fb_row = cur.fetchone()
            company_avg_feedback = round(fb_row['avg_fb'] or 0, 1)
            company_feedback_count = fb_row['fb_count'] or 0

            # Phase 3.2: Recent AI insights
            cur.execute("""
                SELECT id, insight_type, title, body, severity, generated_at, is_read
                FROM company_insights
                WHERE company_id = %s AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY generated_at DESC LIMIT 6
            """, (company['id'],))
            recent_insights = cur.fetchall()

            # Phase 3.3: ROI quick metrics
            cur.execute("""
                SELECT COALESCE(SUM(price), 0) AS total_spend,
                       COUNT(DISTINCT user_id) AS trained,
                       COUNT(CASE WHEN completion_status = 'completed' THEN 1 END) AS completed
                FROM course_orders
                WHERE company_id = %s AND status NOT IN ('cancelled', 'rejected')
            """, (company['id'],))
            roi_row = cur.fetchone()
            roi_total_spend = float(roi_row['total_spend'] or 0)
            roi_employees_trained = roi_row['trained'] or 0
            roi_courses_completed = roi_row['completed'] or 0
            roi_spend_per_employee = round(roi_total_spend / roi_employees_trained) if roi_employees_trained > 0 else 0

            cur.close()

            # Calculate additional metrics
            if hr_metrics['total_employees'] > 0:
                engagement_rate = round((engagement_metrics['employees_using_chatbot'] / hr_metrics['total_employees']) * 100, 1) if engagement_metrics['employees_using_chatbot'] else 0
                training_participation_rate = round((learning_metrics['employees_with_training'] / hr_metrics['total_employees']) * 100, 1) if learning_metrics['employees_with_training'] else 0
            else:
                engagement_rate = 0
                training_participation_rate = 0
            
            if learning_metrics['total_enrollments'] > 0:
                completion_rate = round((learning_metrics['completed_courses'] / learning_metrics['total_enrollments']) * 100, 1)
            else:
                completion_rate = 0
            
            # Calculate conversion rate
            total_chatbot_queries = engagement_metrics.get('total_chatbot_interactions', 0)
            conversion_rate = 0
            if total_chatbot_queries > 0:
                conversion_rate = round((len(recent_orders) / total_chatbot_queries) * 100, 2)
            
            return render_template('hr_dashboard/dashboard.html',
                                 company=company,
                                 hr_metrics=hr_metrics,
                                 learning_metrics=learning_metrics,
                                 engagement_metrics=engagement_metrics,
                                 department_performance=department_performance,
                                 recent_alerts=recent_alerts,
                                 top_performers=top_performers,
                                 learning_paths_progress=learning_paths_progress,
                                 engagement_rate=engagement_rate,
                                 training_participation_rate=training_participation_rate,
                                 completion_rate=completion_rate,
                                 # Order-related data
                                 recent_orders=recent_orders,
                                 pending_orders_count=pending_orders_count,
                                 completed_orders_count=completed_orders_count,
                                 total_revenue=total_revenue,
                                 total_chatbot_queries=total_chatbot_queries,
                                 conversion_rate=conversion_rate,
                                 trending_topics=trending_topics,
                                 company_tool_usage=company_tool_usage,
                                 company_avg_feedback=company_avg_feedback,
                                 company_feedback_count=company_feedback_count,
                                 pending_approvals_count=pending_approvals_count,
                                 total_training_budget=total_training_budget,
                                 total_budget_spent=total_budget_spent,
                                 budget_remaining=budget_remaining,
                                 budget_utilization=budget_utilization,
                                 recent_insights=recent_insights,
                                 roi_total_spend=roi_total_spend,
                                 roi_employees_trained=roi_employees_trained,
                                 roi_courses_completed=roi_courses_completed,
                                 roi_spend_per_employee=roi_spend_per_employee)
            
        except Exception as e:
            current_app.logger.error(f"Error loading HR dashboard: {e}")
            flash("Error loading HR dashboard data.", "danger")
            return redirect(url_for('companies.dashboard'))

    @hr_dashboard_bp.route('/order/<order_id>/update', methods=['POST'])
    def update_company_order_status(order_id):
        """Updates the status of a company order"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Not authenticated'}), 401
        
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404
        
        if request.is_json:
            new_status = request.json.get('status')
        else:
            new_status = request.form.get('status')
        
        if not new_status:
            return jsonify({'success': False, 'message': 'No status provided'}), 400
        
        valid_statuses = ['pending', 'pending_approval', 'approved', 'rejected', 'processing', 'confirmed', 'cancelled', 'completed', 'invoiced', 'paid']
        if new_status not in valid_statuses:
            return jsonify({'success': False, 'message': 'Invalid status'}), 400
        
        try:
            cur = current_app.mysql.connection.cursor()
            
            # Check if order exists and belongs to the company
            cur.execute("""
                SELECT co.order_id FROM course_orders co
                WHERE co.order_id = %s AND co.company_id = %s
            """, (order_id, company['id']))
            
            if not cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': 'Order not found or access denied'}), 404
            
            # Update the order status
            cur.execute("""
                UPDATE course_orders
                SET status = %s, updated_at = NOW()
                WHERE order_id = %s AND company_id = %s
            """, (new_status, order_id, company['id']))
            
            if cur.rowcount == 0:
                cur.close()
                return jsonify({'success': False, 'message': 'No rows updated'}), 400
            
            # When marking completed, also update learning progress + employee counters
            if new_status == 'completed':
                try:
                    cur2 = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                    cur2.execute("""
                        SELECT user_id, company_id, product_handle, product_title
                        FROM course_orders WHERE order_id = %s
                    """, (order_id,))
                    order_row = cur2.fetchone()
                    if order_row and order_row['user_id']:
                        # Update completion status on the order itself
                        cur2.execute("""
                            UPDATE course_orders
                            SET completion_status = 'completed', completion_date = NOW()
                            WHERE order_id = %s
                        """, (order_id,))
                        # Insert/update employee_learning_progress
                        cur2.execute("""
                            INSERT INTO employee_learning_progress
                                (user_id, company_id, course_handle, content_name, status,
                                 progress_percentage, completed_at, created_at)
                            VALUES (%s, %s, %s, %s, 'completed', 100, NOW(), NOW())
                            ON DUPLICATE KEY UPDATE
                                status = 'completed', progress_percentage = 100, completed_at = NOW()
                        """, (order_row['user_id'], order_row['company_id'],
                              order_row.get('product_handle', ''), order_row.get('product_title', '')))
                        # Increment total_courses_completed on company_users
                        cur2.execute("""
                            UPDATE company_users
                            SET total_courses_completed = COALESCE(total_courses_completed, 0) + 1
                            WHERE company_id = %s AND user_id = %s
                        """, (order_row['company_id'], order_row['user_id']))
                    cur2.close()
                except Exception as lp_err:
                    current_app.logger.warning(f"Learning progress update failed: {lp_err}")

            current_app.mysql.connection.commit()
            cur.close()

            current_app.logger.info(f"Company order {order_id} status updated to {new_status} by HR user {session.get('user')} for company {company['id']}")

            return jsonify({
                'success': True,
                'message': f'Order status updated to {new_status}',
                'new_status': new_status
            })
            
        except Exception as e:
            current_app.logger.error(f"Error updating company order status: {e}")
            if 'cur' in locals():
                cur.close()
            return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500

    @hr_dashboard_bp.route('/order/<order_id>/details')
    def company_order_details(order_id):
        """View details of a specific company order"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get order details for the company
            cur.execute("""
                SELECT 
                    co.*, 
                    u.username, u.email,
                    cu.department, cu.job_title, cu.employee_id
                FROM course_orders co
                JOIN users u ON co.user_id = u.id
                JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
                WHERE co.order_id = %s AND co.company_id = %s
            """, (order_id, company['id']))
            
            order = cur.fetchone()
            cur.close()
            
            if not order:
                flash("Order not found.", "danger")
                return redirect(url_for('hr_dashboard.dashboard'))
            
            return render_template('hr_dashboard/order_details.html', 
                                 order=order, 
                                 company=company)
            
        except Exception as e:
            current_app.logger.error(f"Error loading company order details: {e}")
            flash("Error loading order details.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    # ── Phase 2.2: Approval Workflow ──

    @hr_dashboard_bp.route('/approvals')
    def pending_approvals():
        """View pending order approvals for this company"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT oa.*, co.product_title, co.price, co.product_handle,
                       co.variant_date, co.variant_location, co.user_email, co.user_name,
                       u.username AS requester_username,
                       cu.department, cu.job_title
                FROM order_approvals oa
                JOIN course_orders co ON oa.order_id = co.order_id
                JOIN users u ON oa.requester_user_id = u.id
                LEFT JOIN company_users cu ON oa.requester_user_id = cu.user_id AND oa.company_id = cu.company_id
                WHERE oa.company_id = %s
                ORDER BY
                    CASE oa.status WHEN 'pending' THEN 0 ELSE 1 END,
                    oa.requested_at DESC
                LIMIT 50
            """, (company['id'],))
            approvals = cur.fetchall()

            pending_count = sum(1 for a in approvals if a['status'] == 'pending')
            cur.close()

            return render_template('hr_dashboard/approvals.html',
                                   company=company,
                                   approvals=approvals,
                                   pending_count=pending_count)
        except Exception as e:
            current_app.logger.error(f"Error loading approvals: {e}")
            flash("Error loading approvals.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/approval/<int:approval_id>/decide', methods=['POST'])
    def decide_approval(approval_id):
        """Approve or reject an order"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Not authenticated'}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404

        data = request.get_json() if request.is_json else request.form
        decision = data.get('decision')  # 'approved' or 'rejected'
        notes = data.get('notes', '')

        if decision not in ('approved', 'rejected'):
            return jsonify({'success': False, 'message': 'Invalid decision'}), 400

        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Verify approval belongs to this company and is pending
            cur.execute("""
                SELECT oa.id, oa.order_id, co.price, co.department
                FROM order_approvals oa
                JOIN course_orders co ON oa.order_id = co.order_id
                WHERE oa.id = %s AND oa.company_id = %s AND oa.status = 'pending'
            """, (approval_id, company['id']))
            approval = cur.fetchone()
            if not approval:
                cur.close()
                return jsonify({'success': False, 'message': 'Approval not found or already decided'}), 404

            # Update approval
            cur.execute("""
                UPDATE order_approvals
                SET status = %s, notes = %s, approver_user_id = %s, decided_at = NOW()
                WHERE id = %s
            """, (decision, notes, session.get('user_id'), approval_id))

            # Update order status accordingly
            if decision == 'approved':
                cur.execute("""
                    UPDATE course_orders SET status = 'pending', approved_by = %s, updated_at = NOW()
                    WHERE order_id = %s
                """, (session.get('user_id'), approval['order_id']))
            else:
                cur.execute("""
                    UPDATE course_orders SET status = 'rejected', updated_at = NOW()
                    WHERE order_id = %s
                """, (approval['order_id'],))
                # Refund budget if rejected
                if approval.get('price') and float(approval['price']) > 0 and approval.get('department'):
                    import datetime as _dt
                    fiscal_year = _dt.datetime.now().year
                    cur.execute("""
                        UPDATE department_budgets
                        SET spent = GREATEST(0, spent - %s)
                        WHERE company_id = %s AND department = %s AND fiscal_year = %s
                    """, (float(approval['price']), company['id'], approval['department'], fiscal_year))

            current_app.mysql.connection.commit()
            cur.close()

            return jsonify({
                'success': True,
                'message': f'Ordre {approval["order_id"][:8]} er {"godkendt" if decision == "approved" else "afvist"}.',
                'decision': decision
            })
        except Exception as e:
            current_app.logger.error(f"Error processing approval: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    # ── Phase 2.3: Department Budget Management ──

    @hr_dashboard_bp.route('/budgets')
    def department_budgets():
        """View and manage department training budgets"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        try:
            import datetime as _dt
            fiscal_year = int(request.args.get('year', _dt.datetime.now().year))
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            cur.execute("""
                SELECT db.*,
                       (SELECT COUNT(DISTINCT cu.user_id) FROM company_users cu
                        WHERE cu.company_id = db.company_id AND cu.department = db.department AND cu.status = 'active') AS employee_count
                FROM department_budgets db
                WHERE db.company_id = %s AND db.fiscal_year = %s
                ORDER BY db.department
            """, (company['id'], fiscal_year))
            budgets = cur.fetchall()

            # Calculate totals
            total_budget = sum(float(b['annual_budget'] or 0) for b in budgets)
            total_spent = sum(float(b['spent'] or 0) for b in budgets)

            # Get all departments that don't have budgets yet
            cur.execute("""
                SELECT DISTINCT department FROM company_users
                WHERE company_id = %s AND status = 'active' AND department IS NOT NULL
                AND department NOT IN (
                    SELECT department FROM department_budgets
                    WHERE company_id = %s AND fiscal_year = %s
                )
            """, (company['id'], company['id'], fiscal_year))
            unbudgeted_depts = [r['department'] for r in cur.fetchall()]

            cur.close()

            return render_template('hr_dashboard/budgets.html',
                                   company=company,
                                   budgets=budgets,
                                   fiscal_year=fiscal_year,
                                   total_budget=total_budget,
                                   total_spent=total_spent,
                                   unbudgeted_depts=unbudgeted_depts)
        except Exception as e:
            current_app.logger.error(f"Error loading budgets: {e}")
            flash("Error loading budget data.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/budgets/save', methods=['POST'])
    def save_budget():
        """Create or update a department budget"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Not authenticated'}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404

        data = request.get_json() if request.is_json else request.form
        department = data.get('department', '').strip()
        annual_budget = float(data.get('annual_budget', 0))
        import datetime as _dt
        fiscal_year = int(data.get('fiscal_year', _dt.datetime.now().year))

        if not department or annual_budget < 0:
            return jsonify({'success': False, 'message': 'Invalid data'}), 400

        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("""
                INSERT INTO department_budgets (company_id, department, annual_budget, spent, fiscal_year)
                VALUES (%s, %s, %s, 0, %s)
                ON DUPLICATE KEY UPDATE annual_budget = %s
            """, (company['id'], department, annual_budget, fiscal_year, annual_budget))
            current_app.mysql.connection.commit()
            cur.close()
            return jsonify({'success': True, 'message': f'Budget for {department} gemt.'})
        except Exception as e:
            current_app.logger.error(f"Error saving budget: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    # ── Phase 3: Smart Analytics Routes ──

    @hr_dashboard_bp.route('/generate-insights', methods=['POST'])
    def generate_insights():
        """Generate AI insights for the company (on-demand)"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Not authenticated'}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404
        try:
            from insights_engine import generate_company_insights
            insights = generate_company_insights(current_app._get_current_object(), company['id'])
            return jsonify({'success': True, 'count': len(insights), 'insights': [
                {'title': i['title'], 'body': i['body'], 'severity': i['severity']} for i in insights
            ]})
        except Exception as e:
            current_app.logger.error(f"Insight generation error: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    @hr_dashboard_bp.route('/skill-gaps')
    def skill_gaps_view():
        """View skill gap heatmap"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        try:
            from insights_engine import get_skill_gap_analysis
            heatmap = get_skill_gap_analysis(current_app._get_current_object(), company['id'])

            # Get skill targets for management
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT * FROM company_skill_targets WHERE company_id = %s ORDER BY department, skill_name
            """, (company['id'],))
            targets = cur.fetchall()

            # Get departments
            cur.execute("""
                SELECT DISTINCT department FROM company_users
                WHERE company_id = %s AND status = 'active' AND department IS NOT NULL
            """, (company['id'],))
            departments = [r['department'] for r in cur.fetchall()]
            cur.close()

            return render_template('hr_dashboard/skill_gaps.html',
                                   company=company, heatmap=heatmap or {},
                                   targets=targets, departments=departments)
        except Exception as e:
            current_app.logger.error(f"Skill gaps error: {e}")
            flash("Error loading skill gap data.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/skill-targets/save', methods=['POST'])
    def save_skill_target():
        """Add or update a skill target"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False}), 404
        data = request.get_json() if request.is_json else request.form
        dept = data.get('department', '').strip()
        skill = data.get('skill_name', '').strip()
        target = int(data.get('target_level', 3))
        priority = data.get('priority', 'medium')
        if not skill:
            return jsonify({'success': False, 'message': 'Skill name required'}), 400
        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("""
                INSERT INTO company_skill_targets (company_id, department, skill_name, target_level, priority)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE target_level = %s, priority = %s
            """, (company['id'], dept or None, skill, target, priority, target, priority))
            current_app.mysql.connection.commit()
            cur.close()
            return jsonify({'success': True, 'message': f'Kompetencemaal for "{skill}" gemt.'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500

    @hr_dashboard_bp.route('/roi')
    def roi_dashboard():
        """Training ROI dashboard"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        try:
            from insights_engine import get_roi_metrics, get_predictive_data
            roi = get_roi_metrics(current_app._get_current_object(), company['id'])
            predictions = get_predictive_data(current_app._get_current_object(), company['id'])
            return render_template('hr_dashboard/roi.html',
                                   company=company, roi=roi, predictions=predictions)
        except Exception as e:
            current_app.logger.error(f"ROI dashboard error: {e}")
            flash("Error loading ROI data.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/employee-progress')
    def employee_progress():
        """Detailed employee progress tracking"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        # Get filters
        department_filter = request.args.get('department', '')
        status_filter = request.args.get('status', '')
        sort_by = request.args.get('sort', 'progress_desc')
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Build query with filters
            where_conditions = ["cu.company_id = %s"]
            params = [company['id']]
            
            if department_filter:
                where_conditions.append("cu.department = %s")
                params.append(department_filter)
            
            if status_filter:
                where_conditions.append("cu.status = %s")
                params.append(status_filter)
            
            # Sort options
            sort_options = {
                'name_asc': 'u.username ASC',
                'name_desc': 'u.username DESC',
                'progress_asc': 'avg_progress ASC',
                'progress_desc': 'avg_progress DESC',
                'courses_asc': 'courses_completed ASC',
                'courses_desc': 'courses_completed DESC',
                'engagement_asc': 'cu.total_chatbot_queries ASC',
                'engagement_desc': 'cu.total_chatbot_queries DESC'
            }
            
            order_by = sort_options.get(sort_by, 'avg_progress DESC')
            
            # Get detailed employee progress
            cur.execute(f"""
                SELECT 
                    cu.user_id, u.username, u.email,
                    cu.department, cu.job_title, cu.role, cu.employee_id,
                    cu.hire_date, cu.last_login, cu.last_chatbot_interaction,
                    cu.total_chatbot_queries, cu.status,
                    COUNT(DISTINCT co.id) as courses_enrolled,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as courses_completed,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'in_progress' THEN co.id END) as courses_in_progress,
                    COUNT(DISTINCT CASE WHEN co.completion_deadline < NOW() AND co.completion_status NOT IN ('completed', 'cancelled') THEN co.id END) as overdue_courses,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress,
                    COALESCE(SUM(elp.time_spent_minutes), 0) as total_time_spent,
                    COUNT(DISTINCT elp.learning_path_id) as learning_paths_enrolled,
                    manager.username as manager_name
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN users manager ON cu.manager_user_id = manager.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                WHERE {' AND '.join(where_conditions)}
                GROUP BY cu.user_id, u.username, u.email, cu.department, cu.job_title, cu.role, 
                         cu.employee_id, cu.hire_date, cu.last_login, cu.last_chatbot_interaction,
                         cu.total_chatbot_queries, cu.status, manager.username
                ORDER BY {order_by}
            """, params)
            
            employees = cur.fetchall()
            
            # Get departments for filter
            cur.execute("SELECT DISTINCT department FROM company_users WHERE company_id = %s ORDER BY department", (company['id'],))
            departments = [row['department'] for row in cur.fetchall()]
            
            # Get summary statistics
            cur.execute(f"""
                SELECT 
                    COUNT(DISTINCT cu.user_id) as total_count,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_count,
                    COALESCE(AVG(
                        (SELECT AVG(elp2.progress_percentage) 
                         FROM employee_learning_progress elp2 
                         WHERE elp2.user_id = cu.user_id AND elp2.company_id = cu.company_id)
                    ), 0) as overall_avg_progress,
                    COUNT(DISTINCT CASE WHEN cu.last_chatbot_interaction >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as active_learners
                FROM company_users cu
                WHERE {' AND '.join(where_conditions)}
            """, params)
            
            summary_stats = cur.fetchone()
            
            cur.close()
            
            return render_template('hr_dashboard/employee_progress.html',
                                 company=company,
                                 employees=employees,
                                 departments=departments,
                                 summary_stats=summary_stats,
                                 current_filters={
                                     'department': department_filter,
                                     'status': status_filter,
                                     'sort': sort_by
                                 })
            
        except Exception as e:
            current_app.logger.error(f"Error loading employee progress: {e}")
            flash("Error loading employee progress data.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/learning-analytics')
    def learning_analytics():
        """Advanced learning analytics and insights"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        # Get time period filter
        period = request.args.get('period', '30d')
        period_days = {'7d': 7, '30d': 30, '90d': 90, '1y': 365}.get(period, 30)
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Learning trends over time
            cur.execute("""
                SELECT 
                    DATE(co.created_at) as date,
                    COUNT(DISTINCT co.id) as enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completions,
                    COUNT(DISTINCT co.user_id) as unique_learners
                FROM course_orders co
                WHERE co.company_id = %s AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY DATE(co.created_at)
                ORDER BY date
            """, (company['id'], period_days))
            learning_trends = cur.fetchall()
            
            # Course popularity
            cur.execute("""
                SELECT 
                    co.product_title, co.product_handle,
                    COUNT(DISTINCT co.id) as enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completions,
                    ROUND(
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                        COUNT(DISTINCT co.id), 1
                    ) as completion_rate,
                    COALESCE(AVG(CASE WHEN co.completion_date IS NOT NULL THEN DATEDIFF(co.completion_date, co.created_at) END), 0) as avg_completion_days,
                    COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as revenue
                FROM course_orders co
                WHERE co.company_id = %s AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY co.product_title, co.product_handle
                HAVING enrollments > 0
                ORDER BY enrollments DESC
                LIMIT 10
            """, (company['id'], period_days))
            popular_courses = cur.fetchall()
            
            # Department learning comparison
            cur.execute("""
                SELECT 
                    cu.department,
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT co.user_id) as employees_with_training,
                    COUNT(DISTINCT co.id) as total_enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completions,
                    COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as training_investment,
                    ROUND(
                        COUNT(DISTINCT co.user_id) * 100.0 / COUNT(DISTINCT cu.user_id), 1
                    ) as participation_rate,
                    ROUND(
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                        NULLIF(COUNT(DISTINCT co.id), 0), 1
                    ) as completion_rate
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id 
                    AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                WHERE cu.company_id = %s AND cu.status = 'active'
                GROUP BY cu.department
                ORDER BY participation_rate DESC
            """, (period_days, company['id']))
            department_comparison = cur.fetchall()
            
            # Learning path effectiveness
            cur.execute("""
                SELECT 
                    lp.path_name, lp.path_category, lp.difficulty_level,
                    COUNT(DISTINCT elp.user_id) as enrolled_users,
                    COUNT(DISTINCT CASE WHEN elp.completed_at IS NOT NULL THEN elp.user_id END) as completed_users,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress,
                    COALESCE(AVG(elp.time_spent_minutes), 0) as avg_time_spent,
                    COALESCE(AVG(CASE WHEN elp.completed_at IS NOT NULL THEN DATEDIFF(elp.completed_at, elp.started_at) END), 0) as avg_completion_days
                FROM learning_paths lp
                LEFT JOIN employee_learning_progress elp ON lp.id = elp.learning_path_id
                    AND elp.started_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                WHERE lp.company_id = %s AND lp.is_active = 1
                GROUP BY lp.id, lp.path_name, lp.path_category, lp.difficulty_level
                ORDER BY enrolled_users DESC
            """, (period_days, company['id']))
            learning_path_effectiveness = cur.fetchall()
            
            # Engagement patterns
            cur.execute("""
                SELECT 
                    HOUR(ci.created_at) as hour_of_day,
                    COUNT(DISTINCT ci.id) as interactions,
                    COUNT(DISTINCT ci.username) as unique_users,
                    COALESCE(AVG(ci.interaction_quality_score), 0) as avg_quality
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                JOIN company_users cu ON u.id = cu.user_id
                WHERE cu.company_id = %s AND ci.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY HOUR(ci.created_at)
                ORDER BY hour_of_day
            """, (company['id'], period_days))
            hourly_engagement = cur.fetchall()
            
            # Skills gap analysis (based on course categories)
            cur.execute("""
                SELECT 
                    CASE 
                        WHEN co.product_title LIKE '%ledelse%' OR co.product_title LIKE '%leadership%' THEN 'Leadership'
                        WHEN co.product_title LIKE '%projekt%' OR co.product_title LIKE '%project%' THEN 'Project Management'
                        WHEN co.product_title LIKE '%kommunikation%' OR co.product_title LIKE '%communication%' THEN 'Communication'
                        WHEN co.product_title LIKE '%it%' OR co.product_title LIKE '%tech%' THEN 'Technology'
                        WHEN co.product_title LIKE '%salg%' OR co.product_title LIKE '%sales%' THEN 'Sales'
                        ELSE 'Other'
                    END as skill_category,
                    COUNT(DISTINCT co.id) as demand,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as supply,
                    COUNT(DISTINCT co.user_id) as interested_employees,
                    ROUND(
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                        COUNT(DISTINCT co.id), 1
                    ) as fulfillment_rate
                FROM course_orders co
                WHERE co.company_id = %s AND co.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY skill_category
                ORDER BY demand DESC
            """, (company['id'], period_days))
            skills_gap_analysis = cur.fetchall()
            
            cur.close()
            
            # Prepare chart data
            chart_data = {
                'learning_trends': {
                    'dates': [trend['date'].strftime('%Y-%m-%d') for trend in learning_trends],
                    'enrollments': [trend['enrollments'] for trend in learning_trends],
                    'completions': [trend['completions'] for trend in learning_trends]
                },
                'hourly_engagement': {
                    'hours': [f"{hour:02d}:00" for hour in range(24)],
                    'interactions': [0] * 24
                }
            }
            
            # Fill hourly engagement data
            for engagement in hourly_engagement:
                chart_data['hourly_engagement']['interactions'][engagement['hour_of_day']] = engagement['interactions']
            
            return render_template('hr_dashboard/learning_analytics.html',
                                 company=company,
                                 period=period,
                                 learning_trends=learning_trends,
                                 popular_courses=popular_courses,
                                 department_comparison=department_comparison,
                                 learning_path_effectiveness=learning_path_effectiveness,
                                 hourly_engagement=hourly_engagement,
                                 skills_gap_analysis=skills_gap_analysis,
                                 chart_data=chart_data)
            
        except Exception as e:
            current_app.logger.error(f"Error loading learning analytics: {e}")
            flash("Error loading learning analytics data.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/reports')
    def reports():
        """HR Reports and data export"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        return render_template('hr_dashboard/reports.html', company=company)

    @hr_dashboard_bp.route('/export/<report_type>')
    def export_report(report_type):
        """Export HR reports as CSV"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            return jsonify({'error': 'Company not found'}), 404
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            if report_type == 'employee_progress':
                cur.execute("""
                    SELECT 
                        u.username as 'Employee Name',
                        u.email as 'Email',
                        cu.department as 'Department',
                        cu.job_title as 'Job Title',
                        cu.employee_id as 'Employee ID',
                        cu.hire_date as 'Hire Date',
                        COUNT(DISTINCT co.id) as 'Courses Enrolled',
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as 'Courses Completed',
                        COALESCE(AVG(elp.progress_percentage), 0) as 'Average Progress %',
                        cu.total_chatbot_queries as 'Chatbot Interactions',
                        cu.last_login as 'Last Login'
                    FROM company_users cu
                    JOIN users u ON cu.user_id = u.id
                    LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                    LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                    WHERE cu.company_id = %s
                    GROUP BY cu.user_id, u.username, u.email, cu.department, cu.job_title, 
                             cu.employee_id, cu.hire_date, cu.total_chatbot_queries, cu.last_login
                    ORDER BY u.username
                """, (company['id'],))
                
            elif report_type == 'course_completions':
                cur.execute("""
                    SELECT 
                        u.username as 'Employee Name',
                        cu.department as 'Department',
                        co.product_title as 'Course Title',
                        co.created_at as 'Enrollment Date',
                        co.completion_status as 'Status',
                        co.completion_date as 'Completion Date',
                        co.price as 'Course Price',
                        co.variant_location as 'Location',
                        co.variant_date as 'Course Date'
                    FROM course_orders co
                    JOIN users u ON co.user_id = u.id
                    JOIN company_users cu ON co.user_id = cu.user_id AND co.company_id = cu.company_id
                    WHERE co.company_id = %s
                    ORDER BY co.created_at DESC
                """, (company['id'],))
                
            elif report_type == 'department_summary':
                cur.execute("""
                    SELECT 
                        cu.department as 'Department',
                        COUNT(DISTINCT cu.user_id) as 'Total Employees',
                        COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as 'Active Employees',
                        COUNT(DISTINCT co.id) as 'Total Enrollments',
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as 'Completed Courses',
                        ROUND(
                            COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                            NULLIF(COUNT(DISTINCT co.id), 0), 1
                        ) as 'Completion Rate %',
                        COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as 'Training Investment'
                    FROM company_users cu
                    LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                    WHERE cu.company_id = %s
                    GROUP BY cu.department
                    ORDER BY cu.department
                """, (company['id'],))
                
            else:
                return jsonify({'error': 'Invalid report type'}), 400
            
            data = cur.fetchall()
            cur.close()
            
            if not data:
                return jsonify({'error': 'No data found'}), 404
            
            # Create CSV
            output = io.StringIO()
            if data:
                writer = csv.DictWriter(output, fieldnames=data[0].keys())
                writer.writeheader()
                writer.writerows(data)
            
            # Create response
            response = current_app.response_class(
                output.getvalue(),
                mimetype='text/csv',
                headers={
                    'Content-Disposition': f'attachment; filename={report_type}_{company["company_slug"]}_{datetime.now().strftime("%Y%m%d")}.csv'
                }
            )
            
            return response
            
        except Exception as e:
            current_app.logger.error(f"Error exporting report: {e}")
            return jsonify({'error': 'Export failed'}), 500

    @hr_dashboard_bp.route('/employee/<int:user_id>/details')
    def employee_details(user_id):
        """Detailed view of individual employee progress"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get employee details
            cur.execute("""
                SELECT 
                    cu.*, u.username, u.email,
                    manager.username as manager_name,
                    COUNT(DISTINCT co.id) as total_enrollments,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'in_progress' THEN co.id END) as in_progress_courses,
                    COALESCE(SUM(elp.time_spent_minutes), 0) as total_learning_time
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN users manager ON cu.manager_user_id = manager.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                WHERE cu.company_id = %s AND cu.user_id = %s
                GROUP BY cu.id, u.username, u.email, manager.username
            """, (company['id'], user_id))
            
            employee = cur.fetchone()
            if not employee:
                flash("Employee not found.", "danger")
                return redirect(url_for('hr_dashboard.employee_progress'))
            
            # Get course history
            cur.execute("""
                SELECT 
                    co.product_title, co.product_handle, co.price,
                    co.created_at as enrollment_date,
                    co.completion_status, co.completion_date,
                    co.completion_deadline,
                    elp.progress_percentage, elp.time_spent_minutes,
                    elp.last_accessed
                FROM course_orders co
                LEFT JOIN employee_learning_progress elp ON co.user_id = elp.user_id 
                    AND co.product_handle = elp.course_handle AND co.company_id = elp.company_id
                WHERE co.company_id = %s AND co.user_id = %s
                ORDER BY co.created_at DESC
            """, (company['id'], user_id))
            
            course_history = cur.fetchall()
            
            # Get chatbot interaction summary
            cur.execute("""
                SELECT 
                    DATE(ci.created_at) as interaction_date,
                    COUNT(*) as daily_interactions,
                    COALESCE(AVG(ci.interaction_quality_score), 0) as avg_quality
                FROM chatbot_interactions ci
                JOIN users u ON ci.username = u.username
                WHERE u.id = %s
                    AND ci.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
                GROUP BY DATE(ci.created_at)
                ORDER BY interaction_date DESC
                LIMIT 30
            """, (user_id,))
            
            interaction_history = cur.fetchall()
            
            cur.close()
            
            return render_template('hr_dashboard/employee_details.html',
                                 company=company,
                                 employee=employee,
                                 course_history=course_history,
                                 interaction_history=interaction_history)
            
        except Exception as e:
            current_app.logger.error(f"Error loading employee details: {e}")
            flash("Error loading employee details.", "danger")
            return redirect(url_for('hr_dashboard.employee_progress'))

    @hr_dashboard_bp.route('/notifications')
    def notifications():
        """HR notifications and alerts"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get all notifications for HR
            cur.execute("""
                SELECT cn.*, u.username as sender_name
                FROM company_notifications cn
                LEFT JOIN users u ON cn.sender_user_id = u.id
                WHERE cn.company_id = %s 
                AND (cn.recipient_user_id = %s OR cn.recipient_user_id IS NULL)
                AND (cn.target_roles IS NULL OR JSON_CONTAINS(cn.target_roles, %s))
                ORDER BY cn.is_urgent DESC, cn.created_at DESC
                LIMIT 50
            """, (company['id'], session.get('user_id'), json.dumps(session.get('company_role'))))
            
            notifications = cur.fetchall()
            
            cur.close()
            
            return render_template('hr_dashboard/notifications.html',
                                 company=company,
                                 notifications=notifications)
            
        except Exception as e:
            current_app.logger.error(f"Error loading notifications: {e}")
            flash("Error loading notifications.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    # ── Billing Management (off-platform billing workflow) ──

    @hr_dashboard_bp.route('/billing')
    def billing_overview():
        """Billing overview — all orders with billing status"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))

        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Filter params
            billing_filter = request.args.get('billing_status', '')
            dept_filter = request.args.get('department', '')

            where = "co.company_id = %s AND co.status NOT IN ('cancelled', 'rejected')"
            params = [company['id']]

            if billing_filter:
                where += " AND COALESCE(co.billing_status, 'not_invoiced') = %s"
                params.append(billing_filter)
            if dept_filter:
                where += " AND co.department = %s"
                params.append(dept_filter)

            cur.execute(f"""
                SELECT co.order_id, co.product_title, co.username, co.department,
                       co.price, co.status, co.created_at,
                       COALESCE(co.billing_status, 'not_invoiced') as billing_status,
                       co.invoice_number, co.invoice_date, co.payment_date,
                       co.payment_method, co.payment_reference, co.billing_note
                FROM course_orders co
                WHERE {where}
                ORDER BY co.created_at DESC
                LIMIT 500
            """, tuple(params))
            orders = cur.fetchall()

            # Summary stats
            cur.execute("""
                SELECT
                    COUNT(*) as total_orders,
                    COALESCE(SUM(price), 0) as total_value,
                    COUNT(CASE WHEN COALESCE(billing_status, 'not_invoiced') = 'not_invoiced' THEN 1 END) as not_invoiced,
                    COALESCE(SUM(CASE WHEN COALESCE(billing_status, 'not_invoiced') = 'not_invoiced' THEN price ELSE 0 END), 0) as not_invoiced_value,
                    COUNT(CASE WHEN billing_status = 'invoiced' THEN 1 END) as invoiced,
                    COALESCE(SUM(CASE WHEN billing_status = 'invoiced' THEN price ELSE 0 END), 0) as invoiced_value,
                    COUNT(CASE WHEN billing_status = 'paid' THEN 1 END) as paid,
                    COALESCE(SUM(CASE WHEN billing_status = 'paid' THEN price ELSE 0 END), 0) as paid_value
                FROM course_orders
                WHERE company_id = %s AND status NOT IN ('cancelled', 'rejected')
            """, (company['id'],))
            summary = cur.fetchone()

            # Departments for filter
            cur.execute("""
                SELECT DISTINCT department FROM company_users
                WHERE company_id = %s AND department IS NOT NULL
                ORDER BY department
            """, (company['id'],))
            departments = [r['department'] for r in cur.fetchall()]

            cur.close()
            return render_template('hr_dashboard/billing.html',
                                   company=company, orders=orders, summary=summary,
                                   departments=departments,
                                   billing_filter=billing_filter, dept_filter=dept_filter)

        except Exception as e:
            current_app.logger.error(f"Error loading billing: {e}")
            flash("Error loading billing overview.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @hr_dashboard_bp.route('/order/<order_id>/billing', methods=['POST'])
    def update_billing(order_id):
        """Update billing info on an order"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404

        data = request.json or {}
        billing_status = data.get('billing_status', '')
        valid_billing = ['not_invoiced', 'invoiced', 'paid', 'credited']
        if billing_status and billing_status not in valid_billing:
            return jsonify({'success': False, 'message': 'Invalid billing status'}), 400

        try:
            cur = current_app.mysql.connection.cursor()

            # Verify order belongs to company
            cur.execute("SELECT id FROM course_orders WHERE order_id = %s AND company_id = %s",
                        (order_id, company['id']))
            if not cur.fetchone():
                cur.close()
                return jsonify({'success': False, 'message': 'Order not found'}), 404

            updates = []
            params = []

            if billing_status:
                updates.append("billing_status = %s")
                params.append(billing_status)
            if 'invoice_number' in data:
                updates.append("invoice_number = %s")
                params.append(data['invoice_number'] or None)
            if 'invoice_date' in data:
                updates.append("invoice_date = %s")
                params.append(data['invoice_date'] or None)
            if 'payment_date' in data:
                updates.append("payment_date = %s")
                params.append(data['payment_date'] or None)
            if 'payment_method' in data:
                updates.append("payment_method = %s")
                params.append(data['payment_method'] or None)
            if 'payment_reference' in data:
                updates.append("payment_reference = %s")
                params.append(data['payment_reference'] or None)
            if 'billing_note' in data:
                updates.append("billing_note = %s")
                params.append(data['billing_note'] or None)

            if not updates:
                cur.close()
                return jsonify({'success': False, 'message': 'No fields to update'}), 400

            params.extend([order_id, company['id']])
            cur.execute(f"""
                UPDATE course_orders SET {', '.join(updates)}
                WHERE order_id = %s AND company_id = %s
            """, tuple(params))

            current_app.mysql.connection.commit()
            cur.close()

            return jsonify({'success': True, 'message': f'Fakturering opdateret for {order_id}'})

        except Exception as e:
            current_app.logger.error(f"Error updating billing: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    @hr_dashboard_bp.route('/billing/bulk', methods=['POST'])
    def bulk_billing_update():
        """Bulk update billing status for multiple orders"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        company = get_company_context()
        if not company:
            return jsonify({'success': False, 'message': 'Company not found'}), 404

        data = request.json or {}
        order_ids = data.get('order_ids', [])
        billing_status = data.get('billing_status', '')
        invoice_number = data.get('invoice_number', '')

        if not order_ids or not billing_status:
            return jsonify({'success': False, 'message': 'Missing order_ids or billing_status'}), 400
        if billing_status not in ['not_invoiced', 'invoiced', 'paid', 'credited']:
            return jsonify({'success': False, 'message': 'Invalid billing status'}), 400
        if len(order_ids) > 100:
            return jsonify({'success': False, 'message': 'Max 100 orders per batch'}), 400

        try:
            cur = current_app.mysql.connection.cursor()
            placeholders = ','.join(['%s'] * len(order_ids))

            update_parts = ["billing_status = %s"]
            update_params = [billing_status]

            if billing_status == 'invoiced' and invoice_number:
                update_parts.append("invoice_number = %s")
                update_params.append(invoice_number)
                update_parts.append("invoice_date = CURDATE()")
            elif billing_status == 'paid':
                update_parts.append("payment_date = CURDATE()")

            update_params.extend(order_ids)
            update_params.append(company['id'])

            cur.execute(f"""
                UPDATE course_orders SET {', '.join(update_parts)}
                WHERE order_id IN ({placeholders}) AND company_id = %s
            """, tuple(update_params))

            updated = cur.rowcount
            current_app.mysql.connection.commit()
            cur.close()

            return jsonify({'success': True, 'message': f'{updated} ordrer opdateret', 'updated': updated})

        except Exception as e:
            current_app.logger.error(f"Error in bulk billing update: {e}")
            return jsonify({'success': False, 'message': str(e)}), 500

    # ── Phase 5: HR Chatbot ──

    @hr_dashboard_bp.route('/chatbot')
    def hr_chatbot():
        """HR AI Assistant chatbot page"""
        auth_check = require_hr_access()
        if auth_check:
            return auth_check
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        return render_template('hr_dashboard/chatbot.html', company=company)

    @hr_dashboard_bp.route('/chatbot/ask', methods=['POST'])
    def hr_chatbot_ask():
        """HR chatbot SSE endpoint"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({"error": "Unauthorized"}), 401
        company = get_company_context()
        if not company:
            return jsonify({"error": "No company"}), 400

        data = request.json or {}
        user_query = (data.get('query') or '').strip()
        if not user_query:
            return jsonify({"error": "Tom besked"}), 400
        if len(user_query) > 3000:
            user_query = user_query[:3000]

        # Ensure company context is in session for hr_tools
        session['company_name'] = company.get('company_name', '')

        from hr_agent import handle_hr_ask
        return handle_hr_ask(user_query, session)

    @hr_dashboard_bp.route('/chatbot/reset', methods=['POST'])
    def hr_chatbot_reset():
        """Reset HR chatbot session"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({"error": "Unauthorized"}), 401
        session.pop('hr_chat_session_id', None)
        return jsonify({"success": True})

    # ── Phase 5.3: Proactive Notifications ──

    @hr_dashboard_bp.route('/notifications/proactive')
    def proactive_notifications():
        """Get proactive notification alerts for HR"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({"error": "Unauthorized"}), 401
        company = get_company_context()
        if not company:
            return jsonify({"alerts": []})

        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            alerts = []

            # Pending approvals
            cur.execute("""
                SELECT COUNT(*) as cnt FROM order_approvals
                WHERE company_id = %s AND status = 'pending'
            """, (company['id'],))
            pending = cur.fetchone()['cnt']
            if pending > 0:
                alerts.append({
                    "type": "approval",
                    "icon": "fa-clipboard-check",
                    "color": "danger",
                    "title": f"{pending} ventende godkendelser",
                    "message": "Kursusbestillinger venter paa din godkendelse.",
                    "action_url": url_for('hr_dashboard.pending_approvals'),
                    "priority": 1
                })

            # Budget alerts (>80%)
            cur.execute("""
                SELECT department, ROUND(spent / NULLIF(annual_budget, 0) * 100, 1) as pct
                FROM department_budgets
                WHERE company_id = %s AND annual_budget > 0
                AND spent / annual_budget > 0.8
            """, (company['id'],))
            for b in cur.fetchall():
                alerts.append({
                    "type": "budget",
                    "icon": "fa-wallet",
                    "color": "warning",
                    "title": f"{b['department']}: {b['pct']}% budget brugt",
                    "message": f"Afdelingens uddannelsesbudget er naesten opbrugt.",
                    "action_url": url_for('hr_dashboard.department_budgets'),
                    "priority": 2
                })

            # Employees inactive 30+ days
            cur.execute("""
                SELECT COUNT(*) as cnt FROM company_users
                WHERE company_id = %s AND status = 'active'
                AND (last_login IS NULL OR last_login < DATE_SUB(NOW(), INTERVAL 30 DAY))
            """, (company['id'],))
            inactive = cur.fetchone()['cnt']
            if inactive > 0:
                alerts.append({
                    "type": "inactive",
                    "icon": "fa-user-clock",
                    "color": "info",
                    "title": f"{inactive} inaktive medarbejdere",
                    "message": "Medarbejdere der ikke har brugt platformen i 30+ dage.",
                    "action_url": url_for('hr_dashboard.employees'),
                    "priority": 3
                })

            # Upcoming course deadlines (courses starting within 7 days)
            cur.execute("""
                SELECT COUNT(*) as cnt FROM course_orders
                WHERE company_id = %s AND status IN ('confirmed', 'processing')
                AND start_date IS NOT NULL AND start_date BETWEEN NOW() AND DATE_ADD(NOW(), INTERVAL 7 DAY)
            """, (company['id'],))
            upcoming = cur.fetchone()['cnt']
            if upcoming > 0:
                alerts.append({
                    "type": "upcoming",
                    "icon": "fa-calendar-alt",
                    "color": "primary",
                    "title": f"{upcoming} kurser starter inden 7 dage",
                    "message": "Husk at informere medarbejderne.",
                    "action_url": url_for('hr_dashboard.dashboard'),
                    "priority": 2
                })

            # Unread company notifications
            user_id = session.get('user_id')
            if user_id:
                cur.execute("""
                    SELECT COUNT(*) as cnt FROM company_notifications
                    WHERE company_id = %s AND is_read = 0
                    AND (recipient_user_id = %s OR recipient_user_id IS NULL)
                """, (company['id'], user_id))
                unread = cur.fetchone()['cnt']
                if unread > 0:
                    alerts.append({
                        "type": "notification",
                        "icon": "fa-bell",
                        "color": "secondary",
                        "title": f"{unread} ulæste notifikationer",
                        "message": "Du har ulæste beskeder.",
                        "action_url": url_for('hr_dashboard.notifications'),
                        "priority": 4
                    })

            cur.close()
            alerts.sort(key=lambda a: a['priority'])
            return jsonify({"alerts": alerts})

        except Exception as e:
            current_app.logger.error(f"Error loading proactive notifications: {e}")
            return jsonify({"alerts": []})

    @hr_dashboard_bp.route('/notifications/dismiss', methods=['POST'])
    def dismiss_notification():
        """Dismiss/mark notification as read"""
        auth_check = require_hr_access()
        if auth_check:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json or {}
        notif_id = data.get('notification_id')
        if notif_id:
            try:
                cur = current_app.mysql.connection.cursor()
                cur.execute("UPDATE company_notifications SET is_read = 1 WHERE id = %s AND company_id = %s",
                            (notif_id, session.get('company_id')))
                current_app.mysql.connection.commit()
                cur.close()
            except Exception:
                pass
        return jsonify({"success": True})

    return hr_dashboard_bp

# Create the blueprint instance
hr_dashboard_bp = create_hr_dashboard_blueprint()