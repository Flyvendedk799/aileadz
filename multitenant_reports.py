"""
Multi-Tenant Reports System
Enterprise-grade analytics and reporting for company workspaces
Replaces the single-tenant reports.py with company-scoped analytics
"""

from flask import Blueprint, render_template, session, redirect, url_for, flash, current_app, request, jsonify
import MySQLdb.cursors
from collections import defaultdict
import datetime
import json
import re
from datetime import timedelta

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

    @multitenant_reports_bp.route('/')
    def reports():
        """
        Company-specific reports dashboard
        Shows analytics scoped to the user's company only
        """
        auth_check = require_company_access()
        if auth_check:
            return auth_check
        
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

            # 1) Fetch company-specific chatbot interactions
            cur.execute("""
                SELECT ci.*, cu.department, cu.job_title, cu.role
                FROM chatbot_interactions ci
                LEFT JOIN users u ON ci.username = u.username
                LEFT JOIN company_users cu ON u.id = cu.user_id AND cu.company_id = %s
                WHERE ci.company_id = %s
                ORDER BY ci.created_at DESC
                LIMIT 1000
            """, (company['id'], company['id']))
            interactions = cur.fetchall()
            
            current_app.logger.info(f"Processing {len(interactions)} chatbot interactions for company {company['company_name']}")
            
            sessions = defaultdict(list)
            question_responses = defaultdict(lambda: {'count': 0, 'responses': []})
            
            interactions_with_kursus = 0
            interactions_with_products = 0
            
            for interaction in interactions:
                total_chatbot_queries += 1
                dt = interaction['created_at']
                if isinstance(dt, datetime.datetime):
                    date_str = dt.strftime('%Y-%m-%d')
                    hour = dt.hour
                    daily_chatbot_usage[date_str] += 1
                    hourly_usage_pattern[hour] += 1
                
                response_text = interaction.get('response_text', '')
                query_text = interaction.get('query_text', '')
                
                # Enhanced categorization with company context
                department = interaction.get('department', 'Unknown')
                role = interaction.get('role', 'employee')
                
                # Simple categorization
                if any(word in query_text.lower() for word in ['pris', 'koster', 'price', 'cost']):
                    query_type_distribution['price_inquiry'] += 1
                    intent_distribution['prisInfo'] += 1
                elif any(word in query_text.lower() for word in ['dato', 'hvornaar', 'when', 'date']):
                    query_type_distribution['date_inquiry'] += 1
                    intent_distribution['datoInfo'] += 1
                elif any(word in query_text.lower() for word in ['sted', 'hvor', 'where', 'location']):
                    query_type_distribution['location_inquiry'] += 1
                    intent_distribution['stedInfo'] += 1
                elif any(word in query_text.lower() for word in ['anbefal', 'foreslaa', 'recommend']):
                    query_type_distribution['recommendation'] += 1
                    intent_distribution['anbefaling'] += 1
                elif any(word in query_text.lower() for word in ['bestil', 'koeb', 'ordre', 'tilmeld', 'order', 'buy']):
                    query_type_distribution['order_intent'] += 1
                    intent_distribution['bestilling'] += 1
                else:
                    query_type_distribution['general'] += 1
                    intent_distribution['generelInfo'] += 1
                
                # Categories with company-specific tracking
                categories = [
                    'ledelse', 'projekt', 'it', 'kommunikation', 'salg',
                    'personlig udvikling', 'jura', 'oekonomi', 'hr',
                    'leadership', 'project', 'communication', 'sales', 'legal'
                ]
                for cat in categories:
                    if cat in query_text.lower():
                        category_distribution[f"{cat}_{department}"] += 1
                        category_distribution[cat] += 1
                
                # Enhanced course tracking with company context
                if response_text and ('kursus' in response_text.lower() or 'course' in response_text.lower()):
                    interactions_with_kursus += 1
                    
                    # Enhanced patterns for course extraction
                    patterns = [
                        r'/products/([^"\s<>]+)',
                        r'products/([^"\s<>]+)',
                        r'href="[^"]*products/([^"/]+)',
                        r'course[_-]([a-zA-Z0-9\-_]+)',
                        r'kursus[_-]([a-zA-Z0-9\-_]+)',
                    ]
                    
                    handles_found = set()
                    for pattern in patterns:
                        matches = re.findall(pattern, response_text, re.IGNORECASE)
                        for match in matches:
                            clean_match = match.strip().rstrip('",;.')
                            if clean_match and len(clean_match) > 2:
                                handles_found.add(clean_match)
                    
                    if handles_found:
                        interactions_with_products += 1
                        for handle in handles_found:
                            course_views[handle] += 1
                            current_app.logger.debug(f"Company {company['id']}: Extracted course handle: {handle}")
                
                # Track location references
                locs = ['koebenhavn', 'aarhus', 'odense', 'aalborg', 'online', 'herlev', 'hoerkaer']
                for loc in locs:
                    if loc in query_text.lower():
                        user_locations[loc] += 1
                
                # Error tracking
                if 'fejl' in response_text.lower() or 'error' in response_text.lower():
                    api_errors += 1
                
                # No results tracking
                if 'ingen' in response_text.lower() and 'fundet' in response_text.lower():
                    no_results_queries += 1
                
                # Session grouping with company context
                session_id = f"company_{company['id']}_session_{date_str}_{hour}_{department}"
                sessions[session_id].append(interaction)
                
                # Track frequent questions
                if query_text:
                    question_responses[query_text]['count'] += 1
                    if response_text:
                        question_responses[query_text]['responses'].append(response_text)
            
            # Log company-specific course extraction statistics
            current_app.logger.info(f"Company {company['company_name']}: {interactions_with_kursus} interactions with 'kursus', {interactions_with_products} with product links, {len(course_views)} unique courses found")
            
            # Conversation metrics
            if sessions:
                total_conversations = len(sessions)
                conv_lens = [len(msgs) for msgs in sessions.values()]
                if conv_lens:
                    avg_conversation_length = sum(conv_lens) / len(conv_lens)
            
            avg_response_time = 250  # placeholder ms
            
            # Frequent questions (top 10)
            sorted_q = sorted(question_responses.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
            fd_qs = []
            for q_text, q_data in sorted_q:
                truncated_text = q_text[:100] + '...' if len(q_text) > 100 else q_text
                top_res = ''
                if q_data['responses']:
                    top_res = q_data['responses'][0]
                    if len(top_res) > 200:
                        top_res = top_res[:200] + '...'
                fd_qs.append({
                    'text': truncated_text,
                    'count': q_data['count'],
                    'top_response': top_res
                })
            frequent_questions = fd_qs
            
            # Build recent conversations by session (top 10)
            recent_session_ids = list(sessions.keys())[:10]
            for sid in recent_session_ids:
                si = sessions[sid]
                if si:
                    first_i = si[-1]
                    last_i = si[0]
                    topics = set()
                    for inter in si:
                        lower_txt = inter.get('query_text', '').lower()
                        for tcat in ['ledelse', 'projekt', 'it', 'kommunikation', 'salg']:
                            if tcat in lower_txt:
                                topics.add(tcat)
                    duration = 5
                    if (isinstance(first_i['created_at'], datetime.datetime) and
                       isinstance(last_i['created_at'], datetime.datetime)):
                        duration = int((last_i['created_at'] - first_i['created_at']).total_seconds() / 60)
                    
                    recent_conversations.append({
                        'session_id': sid[:12],
                        'start_time': (
                            first_i['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                            if isinstance(first_i['created_at'], datetime.datetime)
                            else 'N/A'
                        ),
                        'duration': duration,
                        'query_count': len(si),
                        'topics': list(topics)[:3],
                        'department': first_i.get('department', 'Unknown')
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
            
            # Get company-specific analytics
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_employees,
                    COUNT(DISTINCT cu.department) as total_departments,
                    COALESCE(AVG(ci.interaction_quality_score), 0) as avg_interaction_quality,
                    COUNT(DISTINCT CASE WHEN cu.last_chatbot_interaction >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN cu.user_id END) as active_users_30d
                FROM company_users cu
                LEFT JOIN chatbot_interactions ci ON ci.company_id = cu.company_id
                WHERE cu.company_id = %s
            """, (company['id'],))
            company_stats = cur.fetchone()
            
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
        
        return render_template(
            'multitenant_reports/dashboard.html',
            company=company,
            company_stats=company_stats,
            
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
    def order_detail(order_id):
        """
        Company-scoped order details
        """
        auth_check = require_company_access()
        if auth_check:
            return auth_check
        
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
            
            return render_template('multitenant_reports/order_detail.html', 
                                 company=company, order=order)
        except Exception as e:
            current_app.logger.error(f"Error fetching order details: {e}")
            flash("Error loading order details.", "danger")
            return redirect(url_for('multitenant_reports.reports'))

    @multitenant_reports_bp.route('/order/<order_id>/update', methods=['POST'])
    def update_order_status(order_id):
        """
        Update order status (company-scoped)
        Only HR managers and company admins can update orders
        """
        auth_check = require_company_access()
        if auth_check:
            return auth_check
        
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
    def export_analytics():
        """
        Export company-specific analytics data as JSON
        """
        auth_check = require_company_access()
        if auth_check:
            return auth_check
        
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
            'exported_by': session.get('user'),
            'metrics': {},
            'time_series': {},
            'distributions': {}
        }
        
        response = jsonify(analytics_data)
        response.headers['Content-Disposition'] = (
            f"attachment; filename=company_analytics_{company['company_slug']}_{datetime.datetime.now().strftime('%Y%m%d')}.json"
        )
        return response

    @multitenant_reports_bp.route('/department/<department_name>')
    def department_analytics(department_name):
        """
        Department-specific analytics within the company
        """
        auth_check = require_company_access()
        if auth_check:
            return auth_check
        
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
            
            return render_template('multitenant_reports/department_analytics.html',
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
