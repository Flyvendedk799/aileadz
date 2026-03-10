"""
Enterprise Company Management System
Multi-tenant B2B solution for corporate learning management
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
from datetime import datetime, timedelta
import json
import secrets
import string
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
import os

def create_companies_blueprint():
    companies_bp = Blueprint('companies', __name__, template_folder='templates')

    def require_company_admin():
        """Decorator to ensure user is a company admin"""
        if 'user' not in session:
            flash("Please log in to access this feature.", "danger")
            return redirect(url_for('auth.login'))
        
        if not session.get('company_id') or session.get('company_role') not in ['company_admin', 'hr_manager']:
            flash("You don't have permission to access this feature.", "danger")
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

    @companies_bp.route('/search-users')
    def search_users():
        """AJAX endpoint for admin to search existing users (not already in a company)"""
        if 'user' not in session or session.get('role') != 'admin':
            return jsonify([])
        q = request.args.get('q', '').strip()
        if len(q) < 2:
            return jsonify([])
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT u.id, u.username, u.email
                FROM users u
                WHERE (u.username LIKE %s OR u.email LIKE %s)
                  AND u.id NOT IN (SELECT user_id FROM company_users WHERE status = 'active')
                LIMIT 10
            """, (f'%{q}%', f'%{q}%'))
            results = cur.fetchall()
            cur.close()
            return jsonify([{'id': r['id'], 'username': r['username'], 'email': r['email']} for r in results])
        except Exception:
            return jsonify([])

    @companies_bp.route('/register', methods=['GET', 'POST'])
    def register_company():
        """Company registration — super admin only. Creates company + assigns HR manager."""
        if 'user' not in session or session.get('role') != 'admin':
            flash("Kun administratorer kan oprette virksomheder.", "danger")
            return redirect(url_for('auth.login'))
        if request.method == 'POST':
            # Company details
            company_name = request.form.get('company_name', '').strip()
            company_domain = request.form.get('company_domain', '').strip()
            industry = request.form.get('industry', '').strip()
            company_size = request.form.get('company_size', '11-50')
            country = request.form.get('country', 'Denmark')
            city = request.form.get('city', '').strip()

            # HR manager mode: 'new' or 'existing'
            hr_mode = request.form.get('hr_mode', 'new')
            existing_user_id = request.form.get('existing_user_id', '').strip()

            # New HR manager details
            hr_name = request.form.get('hr_name', '').strip()
            hr_email = request.form.get('hr_email', '').strip()
            hr_phone = request.form.get('hr_phone', '').strip()
            hr_job_title = request.form.get('job_title', '').strip() or 'HR Manager'

            # Validation
            if not company_name:
                flash("Virksomhedsnavn er påkrævet.", "danger")
                return render_template('companies/register.html')

            if hr_mode == 'new' and not all([hr_name, hr_email]):
                flash("Udfyld venligst navn og e-mail for HR-manageren.", "danger")
                return render_template('companies/register.html')

            if hr_mode == 'existing' and not existing_user_id:
                flash("Vælg venligst en eksisterende bruger.", "danger")
                return render_template('companies/register.html')

            # Generate company slug
            company_slug = ''.join(c.lower() if c.isalnum() else '-' for c in company_name)
            company_slug = company_slug.strip('-')

            generated_password = None

            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

                # Check if company slug already exists
                cur.execute("SELECT id FROM companies WHERE company_slug = %s", (company_slug,))
                if cur.fetchone():
                    company_slug += f"-{secrets.token_hex(4)}"

                # Create company
                cur.execute("""
                    INSERT INTO companies (
                        company_name, company_slug, company_domain, industry,
                        company_size, country, city, subscription_plan,
                        trial_ends_at, max_employees, features, settings
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'trial', %s, %s, %s, %s)
                """, (
                    company_name, company_slug, company_domain, industry,
                    company_size, country, city,
                    datetime.now() + timedelta(days=30),
                    50,
                    json.dumps({
                        "advanced_analytics": True,
                        "custom_branding": False,
                        "api_access": False,
                        "sso": False
                    }),
                    json.dumps({
                        "timezone": "Europe/Copenhagen",
                        "language": "da",
                        "currency": "DKK"
                    })
                ))

                company_id = cur.lastrowid

                if hr_mode == 'existing':
                    # Assign existing user as HR manager
                    cur.execute("SELECT id, username, email FROM users WHERE id = %s", (existing_user_id,))
                    user_row = cur.fetchone()
                    if not user_row:
                        flash("Brugeren blev ikke fundet.", "danger")
                        cur.close()
                        return render_template('companies/register.html')
                    user_id = user_row['id']
                    hr_name = user_row['username']
                    hr_email = user_row['email']
                else:
                    # Create new user as HR manager
                    cur.execute("SELECT id FROM users WHERE email = %s", (hr_email,))
                    if cur.fetchone():
                        flash("En bruger med denne e-mail eksisterer allerede. Brug 'Eksisterende bruger' i stedet.", "danger")
                        cur.close()
                        return render_template('companies/register.html')

                    # Generate a random password
                    generated_password = ''.join(secrets.choice(
                        string.ascii_letters + string.digits + '!@#$%'
                    ) for _ in range(12))
                    hashed_password = generate_password_hash(generated_password)

                    cur.execute("""
                        INSERT INTO users (username, email, password, credits, role)
                        VALUES (%s, %s, %s, 1000, 'user')
                    """, (hr_name, hr_email, hashed_password))
                    user_id = cur.lastrowid

                # Add user to company as hr_manager
                hr_permissions = json.dumps({
                    "manage_users": True,
                    "view_analytics": True,
                    "export_data": True,
                    "manage_billing": False,
                    "manage_integrations": False
                })
                cur.execute("""
                    INSERT INTO company_users (
                        company_id, user_id, username, email, role, department, job_title,
                        status, phone, permissions, added_by
                    ) VALUES (%s, %s, %s, %s, 'hr_manager', 'Human Resources', %s, 'active', %s, %s, %s)
                """, (
                    company_id, user_id, hr_name, hr_email, hr_job_title, hr_phone,
                    hr_permissions, session.get('user_id')
                ))

                # Create default departments (IGNORE handles duplicates from previous attempts)
                default_departments = [
                    ('Human Resources', 'HR', 'Employee management and development'),
                    ('Administration', 'ADMIN', 'Company administration and operations'),
                    ('General', 'GEN', 'General employees and contractors')
                ]
                for dept_name, dept_code, dept_desc in default_departments:
                    try:
                        cur.execute("""
                            INSERT IGNORE INTO company_departments (company_id, department_name, department_code, description)
                            VALUES (%s, %s, %s, %s)
                        """, (company_id, dept_name, dept_code, dept_desc))
                    except Exception:
                        pass  # skip if duplicate

                # Audit log
                cur.execute("""
                    INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                    VALUES (%s, %s, 'company_created', 'company', %s, %s)
                """, (
                    company_id, session.get('user_id'), str(company_id),
                    json.dumps({
                        "company_name": company_name,
                        "hr_manager_email": hr_email,
                        "hr_mode": hr_mode,
                        "subscription_plan": "trial"
                    })
                ))

                current_app.mysql.connection.commit()
                cur.close()

                # Show success with credentials if new user was created
                if generated_password:
                    return render_template('companies/register_success.html',
                        company_name=company_name,
                        hr_name=hr_name,
                        hr_email=hr_email,
                        hr_password=generated_password,
                        is_new_user=True
                    )
                else:
                    return render_template('companies/register_success.html',
                        company_name=company_name,
                        hr_name=hr_name,
                        hr_email=hr_email,
                        is_new_user=False
                    )

            except Exception as e:
                current_app.logger.error(f"Error registering company: {e}")
                flash("Der opstod en fejl under oprettelsen. Prøv venligst igen.", "danger")
                if 'cur' in locals():
                    cur.close()
                return render_template('companies/register.html')

        return render_template('companies/register.html')

    @companies_bp.route('/dashboard')
    def dashboard():
        """Redirect to HR dashboard (merged)"""
        return redirect(url_for('hr_dashboard.dashboard'))

    @companies_bp.route('/employees')
    def employees():
        """Employee management page for HR managers"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get all employees with their progress
            cur.execute("""
                SELECT 
                    cu.id as company_user_id,
                    u.id as user_id, u.username, u.email,
                    cu.role, cu.department, cu.job_title, cu.employee_id,
                    cu.hire_date, cu.employment_type, cu.status,
                    cu.last_login, cu.last_chatbot_interaction,
                    cu.total_chatbot_queries, cu.total_courses_completed,
                    manager.username as manager_name,
                    COUNT(DISTINCT co.id) as courses_enrolled,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as courses_completed,
                    COALESCE(AVG(elp.progress_percentage), 0) as avg_progress
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN users manager ON cu.manager_user_id = manager.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                LEFT JOIN employee_learning_progress elp ON cu.user_id = elp.user_id AND cu.company_id = elp.company_id
                WHERE cu.company_id = %s
                GROUP BY cu.id, u.id, u.username, u.email, cu.role, cu.department, cu.job_title, 
                         cu.employee_id, cu.hire_date, cu.employment_type, cu.status,
                         cu.last_login, cu.last_chatbot_interaction, cu.total_chatbot_queries, 
                         cu.total_courses_completed, manager.username
                ORDER BY cu.department, u.username
            """, (company['id'],))
            employees = cur.fetchall()
            
            # Get departments for filtering
            cur.execute("""
                SELECT DISTINCT department 
                FROM company_users 
                WHERE company_id = %s AND status = 'active'
                ORDER BY department
            """, (company['id'],))
            departments = [row['department'] for row in cur.fetchall()]
            
            cur.close()
            
            return render_template('companies/employees.html',
                                 company=company,
                                 employees=employees,
                                 departments=departments)
            
        except Exception as e:
            current_app.logger.error(f"Error loading employees: {e}")
            flash("Error loading employee data.", "danger")
            return redirect(url_for('companies.dashboard'))

    @companies_bp.route('/employees/add', methods=['GET', 'POST'])
    def add_employee():
        """Add new employee to company"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        if request.method == 'POST':
            # Employee details
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()
            role = request.form.get('role', 'employee')
            department = request.form.get('department', '').strip()
            job_title = request.form.get('job_title', '').strip()
            employee_id = request.form.get('employee_id', '').strip()
            hire_date = request.form.get('hire_date', '')
            employment_type = request.form.get('employment_type', 'full_time')
            
            # Validation
            if not all([username, email, password, department, job_title]):
                flash("Please fill in all required fields.", "danger")
                return render_template('companies/add_employee.html', company=company)
            
            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                
                # Check if user already exists
                cur.execute("SELECT id FROM users WHERE email = %s OR username = %s", (email, username))
                existing_user = cur.fetchone()
                
                if existing_user:
                    # User exists, just add to company
                    user_id = existing_user['id']
                    
                    # Check if already in company
                    cur.execute("SELECT id FROM company_users WHERE company_id = %s AND user_id = %s", 
                              (company['id'], user_id))
                    if cur.fetchone():
                        flash("This user is already part of your company.", "warning")
                        cur.close()
                        return render_template('companies/add_employee.html', company=company)
                else:
                    # Create new user
                    hashed_password = generate_password_hash(password)
                    cur.execute("""
                        INSERT INTO users (username, email, password, credits, role)
                        VALUES (%s, %s, %s, 100, 'employee')
                    """, (username, email, hashed_password))
                    user_id = cur.lastrowid
                
                # Set permissions based on role
                permissions = {
                    'company_admin': {
                        "manage_users": True, "view_analytics": True, "export_data": True,
                        "manage_billing": True, "manage_integrations": True
                    },
                    'hr_manager': {
                        "manage_users": True, "view_analytics": True, "export_data": True,
                        "manage_billing": False, "manage_integrations": False
                    },
                    'department_head': {
                        "manage_users": False, "view_analytics": True, "export_data": True,
                        "manage_billing": False, "manage_integrations": False
                    },
                    'team_lead': {
                        "manage_users": False, "view_analytics": True, "export_data": False,
                        "manage_billing": False, "manage_integrations": False
                    },
                    'employee': {
                        "manage_users": False, "view_analytics": False, "export_data": False,
                        "manage_billing": False, "manage_integrations": False
                    }
                }
                
                # Add to company
                cur.execute("""
                    INSERT INTO company_users (
                        company_id, user_id, username, email, role, department, job_title, employee_id,
                        hire_date, employment_type, status, permissions, added_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                """, (
                    company['id'], user_id, username, email, role, department, job_title, employee_id,
                    hire_date if hire_date else None, employment_type,
                    json.dumps(permissions.get(role, permissions['employee'])),
                    session.get('user_id')
                ))
                
                # Log the action
                cur.execute("""
                    INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                    VALUES (%s, %s, 'user_added', 'user', %s, %s)
                """, (
                    company['id'], session.get('user_id'), str(user_id),
                    json.dumps({
                        "username": username, "email": email, "role": role,
                        "department": department, "job_title": job_title
                    })
                ))
                
                current_app.mysql.connection.commit()
                cur.close()

                flash(f"Employee '{username}' has been added successfully!", "success")
                return redirect(url_for('companies.employees'))
                
            except Exception as e:
                current_app.logger.error(f"Error adding employee: {e}")
                flash("An error occurred while adding the employee.", "danger")
                if 'cur' in locals():
                    cur.close()
        
        # Get departments for dropdown
        departments = []
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT department_name FROM company_departments WHERE company_id = %s", (company['id'],))
            departments = [row['department_name'] for row in cur.fetchall()]
            cur.close()
        except Exception as e:
            current_app.logger.error(f"Error loading departments: {e}")
        
        return render_template('companies/add_employee.html', company=company, departments=departments)

    @companies_bp.route('/employees/<int:user_id>/edit', methods=['GET', 'POST'])
    def edit_employee(user_id):
        """Edit employee details"""
        auth_check = require_company_admin()
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
                SELECT cu.*, u.username, u.email
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                WHERE cu.company_id = %s AND cu.user_id = %s
            """, (company['id'], user_id))
            employee = cur.fetchone()
            
            if not employee:
                flash("Employee not found.", "danger")
                cur.close()
                return redirect(url_for('companies.employees'))
            
            if request.method == 'POST':
                # Update employee details
                role = request.form.get('role', employee['role'])
                department = request.form.get('department', employee['department'])
                job_title = request.form.get('job_title', employee['job_title'])
                employee_id = request.form.get('employee_id', employee['employee_id'])
                employment_type = request.form.get('employment_type', employee['employment_type'])
                status = request.form.get('status', employee['status'])
                
                cur.execute("""
                    UPDATE company_users 
                    SET role = %s, department = %s, job_title = %s, employee_id = %s,
                        employment_type = %s, status = %s, updated_at = NOW()
                    WHERE company_id = %s AND user_id = %s
                """, (role, department, job_title, employee_id, employment_type, status, company['id'], user_id))
                
                # Log the action
                cur.execute("""
                    INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                    VALUES (%s, %s, 'user_updated', 'user', %s, %s)
                """, (
                    company['id'], session.get('user_id'), str(user_id),
                    json.dumps({
                        "old_role": employee['role'], "new_role": role,
                        "old_department": employee['department'], "new_department": department,
                        "old_status": employee['status'], "new_status": status
                    })
                ))
                
                current_app.mysql.connection.commit()
                flash("Employee details updated successfully!", "success")
                return redirect(url_for('companies.employees'))
            
            # Get departments for dropdown
            cur.execute("SELECT department_name FROM company_departments WHERE company_id = %s", (company['id'],))
            departments = [row['department_name'] for row in cur.fetchall()]
            
            cur.close()
            
            return render_template('companies/edit_employee.html', 
                                 company=company, employee=employee, departments=departments)
            
        except Exception as e:
            current_app.logger.error(f"Error editing employee: {e}")
            flash("Error loading employee details.", "danger")
            return redirect(url_for('companies.employees'))

    @companies_bp.route('/analytics')
    def analytics():
        """Advanced company analytics dashboard"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check

        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))

        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cid = company['id']
            period = request.args.get('period', '30d')
            days = int(period.replace('d', '')) if period.endswith('d') else 30

            # Employee stats
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN status = 'active' THEN 1 END) as active,
                       COUNT(CASE WHEN added_at >= DATE_SUB(NOW(), INTERVAL %s DAY) THEN 1 END) as new_hires
                FROM company_users WHERE company_id = %s
            """, (days, cid))
            emp_stats = cur.fetchone() or {}

            # Course order stats
            cur.execute("""
                SELECT COUNT(*) as total_orders,
                       COUNT(CASE WHEN completion_status = 'completed' THEN 1 END) as completed,
                       COALESCE(SUM(price), 0) as total_spent
                FROM course_orders WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (cid, days))
            order_stats = cur.fetchone() or {}
            total_orders = order_stats.get('total_orders', 0) or 0
            completed = order_stats.get('completed', 0) or 0
            completion_rate = round((completed / total_orders * 100) if total_orders else 0, 1)

            # Chatbot engagement
            cur.execute("""
                SELECT COUNT(*) as total_interactions,
                       COUNT(DISTINCT username) as unique_users,
                       COALESCE(AVG(response_time_ms), 0) as avg_response_time,
                       COALESCE(AVG(interaction_quality_score), 0) as avg_quality
                FROM chatbot_interactions WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (cid, days))
            chat_stats = cur.fetchone() or {}

            # Daily activity (for chart)
            cur.execute("""
                SELECT DATE(created_at) as day, COUNT(*) as interactions
                FROM chatbot_interactions WHERE company_id = %s AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY DATE(created_at) ORDER BY day
            """, (cid, days))
            daily_activity = cur.fetchall()

            # Department breakdown
            cur.execute("""
                SELECT COALESCE(cu.department, 'Ukendt') as department,
                       COUNT(DISTINCT cu.user_id) as employees,
                       COUNT(DISTINCT co.id) as orders,
                       COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_orders
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND co.company_id = cu.company_id
                WHERE cu.company_id = %s
                GROUP BY cu.department ORDER BY employees DESC
            """, (cid,))
            departments = cur.fetchall()

            # Top courses
            cur.execute("""
                SELECT product_title, COUNT(*) as order_count, COALESCE(AVG(price), 0) as avg_price
                FROM course_orders WHERE company_id = %s
                GROUP BY product_title ORDER BY order_count DESC LIMIT 5
            """, (cid,))
            top_courses = cur.fetchall()

            cur.close()

            return render_template('companies/analytics.html',
                company=company, period=period, days=days,
                emp_stats=emp_stats, order_stats=order_stats,
                completion_rate=completion_rate, chat_stats=chat_stats,
                daily_activity=daily_activity, departments=departments,
                top_courses=top_courses)
            
        except Exception as e:
            current_app.logger.error(f"Error loading analytics: {e}")
            flash("Error loading analytics data.", "danger")
            return redirect(url_for('companies.dashboard'))

    @companies_bp.route('/settings', methods=['GET', 'POST'])
    def settings():
        """Company settings management"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        if request.method == 'POST':
            try:
                cur = current_app.mysql.connection.cursor()
                cur.execute("""
                    UPDATE companies SET company_name = %s, contact_email = %s,
                        website = %s, industry = %s, updated_at = NOW()
                    WHERE id = %s
                """, (
                    request.form.get('company_name', company.get('company_name', '')),
                    request.form.get('contact_email', ''),
                    request.form.get('website', ''),
                    request.form.get('industry', ''),
                    company['id']
                ))
                current_app.mysql.connection.commit()
                cur.close()
                # Update session
                if request.form.get('company_name'):
                    session['company_name'] = request.form['company_name']
                flash("Indstillinger gemt!", "success")
            except Exception as e:
                current_app.logger.error(f"Settings update error: {e}")
                flash("Fejl ved opdatering.", "danger")
            return redirect(url_for('companies.settings'))

        # Load extra settings from company_settings table if exists
        settings = {}
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT * FROM company_settings WHERE company_id = %s", (company['id'],))
            settings = cur.fetchone() or {}
            cur.close()
        except Exception:
            pass

        return render_template('companies/settings.html', company=company, settings=settings)

    return companies_bp

# Create the blueprint instance
companies_bp = create_companies_blueprint()
