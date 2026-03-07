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
                                 conversion_rate=conversion_rate)
            
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
        
        valid_statuses = ['pending', 'processing', 'confirmed', 'cancelled', 'completed']
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

    return hr_dashboard_bp

# Create the blueprint instance
hr_dashboard_bp = create_hr_dashboard_blueprint()