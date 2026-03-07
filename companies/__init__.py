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

    @companies_bp.route('/register', methods=['GET', 'POST'])
    def register_company():
        """Company registration for new enterprise clients"""
        if request.method == 'POST':
            # Company details
            company_name = request.form.get('company_name', '').strip()
            company_domain = request.form.get('company_domain', '').strip()
            industry = request.form.get('industry', '').strip()
            company_size = request.form.get('company_size', '11-50')
            country = request.form.get('country', 'Denmark')
            city = request.form.get('city', '').strip()
            
            # Admin user details
            admin_name = request.form.get('admin_name', '').strip()
            admin_email = request.form.get('admin_email', '').strip()
            admin_phone = request.form.get('admin_phone', '').strip()
            admin_password = request.form.get('admin_password', '').strip()
            
            # Validation
            if not all([company_name, admin_name, admin_email, admin_password]):
                flash("Please fill in all required fields.", "danger")
                return render_template('companies/register.html')
            
            # Generate company slug
            company_slug = ''.join(c.lower() if c.isalnum() else '-' for c in company_name)
            company_slug = company_slug.strip('-')
            
            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                
                # Check if company slug already exists
                cur.execute("SELECT id FROM companies WHERE company_slug = %s", (company_slug,))
                if cur.fetchone():
                    company_slug += f"-{secrets.token_hex(4)}"
                
                # Check if admin email already exists
                cur.execute("SELECT id FROM users WHERE email = %s", (admin_email,))
                existing_user = cur.fetchone()
                
                if existing_user:
                    flash("A user with this email already exists. Please use a different email or log in.", "danger")
                    cur.close()
                    return render_template('companies/register.html')
                
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
                    datetime.now() + timedelta(days=30),  # 30-day trial
                    50,  # Default max employees for trial
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
                
                # Create admin user
                cur.execute("""
                    INSERT INTO users (username, email, password, credits, role)
                    VALUES (%s, %s, %s, 1000, 'company_admin')
                """, (admin_name, admin_email, admin_password))
                
                user_id = cur.lastrowid
                
                # Add admin to company
                cur.execute("""
                    INSERT INTO company_users (
                        company_id, user_id, role, department, job_title, 
                        status, permissions, added_by
                    ) VALUES (%s, %s, 'company_admin', 'Administration', 'Company Administrator', 'active', %s, %s)
                """, (
                    company_id, user_id,
                    json.dumps({
                        "manage_users": True,
                        "view_analytics": True,
                        "export_data": True,
                        "manage_billing": True,
                        "manage_integrations": True
                    }),
                    user_id
                ))
                
                # Create default departments
                default_departments = [
                    ('Human Resources', 'HR', 'Employee management and development'),
                    ('Administration', 'ADMIN', 'Company administration and operations'),
                    ('General', 'GEN', 'General employees and contractors')
                ]
                
                for dept_name, dept_code, dept_desc in default_departments:
                    cur.execute("""
                        INSERT INTO company_departments (company_id, department_name, department_code, description)
                        VALUES (%s, %s, %s, %s)
                    """, (company_id, dept_name, dept_code, dept_desc))
                
                # Log the company creation
                cur.execute("""
                    INSERT INTO audit_log (company_id, user_id, action_type, resource_type, resource_id, details)
                    VALUES (%s, %s, 'company_created', 'company', %s, %s)
                """, (
                    company_id, user_id, str(company_id),
                    json.dumps({
                        "company_name": company_name,
                        "admin_email": admin_email,
                        "subscription_plan": "trial"
                    })
                ))
                
                current_app.mysql.connection.commit()
                cur.close()

                # Auto-login the admin user
                session['user'] = admin_name
                session['user_id'] = user_id
                session['company_id'] = company_id
                session['company_role'] = 'company_admin'
                session['company_name'] = company_name
                
                flash(f"Welcome to AiLead Enterprise! Your company '{company_name}' has been registered successfully. You have a 30-day free trial.", "success")
                return redirect(url_for('companies.dashboard'))
                
            except Exception as e:
                current_app.logger.error(f"Error registering company: {e}")
                flash("An error occurred during registration. Please try again.", "danger")
                if 'cur' in locals():
                    cur.close()
                return render_template('companies/register.html')
        
        return render_template('companies/register.html')

    @companies_bp.route('/dashboard')
    def dashboard():
        """Main company dashboard for admins and HR managers"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Company information not found.", "danger")
            return redirect(url_for('auth.login'))
        
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get company statistics
            cur.execute("""
                SELECT 
                    COUNT(DISTINCT cu.user_id) as total_employees,
                    COUNT(DISTINCT CASE WHEN cu.status = 'active' THEN cu.user_id END) as active_employees,
                    COUNT(DISTINCT cu.department) as total_departments,
                    COUNT(DISTINCT co.id) as total_course_orders,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    COALESCE(SUM(CASE WHEN co.completion_status = 'completed' THEN co.price END), 0) as total_investment,
                    COUNT(DISTINCT ci.id) as total_chatbot_interactions
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.company_id = co.company_id
                LEFT JOIN chatbot_interactions ci ON cu.company_id = ci.company_id
                WHERE cu.company_id = %s
            """, (company['id'],))
            stats = cur.fetchone()
            
            # Get recent activity
            cur.execute("""
                SELECT 
                    al.action_type, al.details, al.created_at,
                    u.username as user_name
                FROM audit_log al
                LEFT JOIN users u ON al.user_id = u.id
                WHERE al.company_id = %s
                ORDER BY al.created_at DESC
                LIMIT 10
            """, (company['id'],))
            recent_activity = cur.fetchall()
            
            # Get department breakdown
            cur.execute("""
                SELECT 
                    cu.department,
                    COUNT(DISTINCT cu.user_id) as employee_count,
                    COUNT(DISTINCT co.id) as course_orders,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as completed_courses,
                    ROUND(
                        COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) * 100.0 / 
                        NULLIF(COUNT(DISTINCT co.id), 0), 1
                    ) as completion_rate
                FROM company_users cu
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                WHERE cu.company_id = %s AND cu.status = 'active'
                GROUP BY cu.department
                ORDER BY employee_count DESC
            """, (company['id'],))
            department_stats = cur.fetchall()
            
            # Get top performers
            cur.execute("""
                SELECT 
                    u.username, cu.department, cu.job_title,
                    COUNT(DISTINCT co.id) as courses_enrolled,
                    COUNT(DISTINCT CASE WHEN co.completion_status = 'completed' THEN co.id END) as courses_completed,
                    cu.total_chatbot_queries,
                    cu.last_chatbot_interaction
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                LEFT JOIN course_orders co ON cu.user_id = co.user_id AND cu.company_id = co.company_id
                WHERE cu.company_id = %s AND cu.status = 'active'
                GROUP BY cu.user_id, u.username, cu.department, cu.job_title, cu.total_chatbot_queries, cu.last_chatbot_interaction
                ORDER BY courses_completed DESC, cu.total_chatbot_queries DESC
                LIMIT 10
            """, (company['id'],))
            top_performers = cur.fetchall()
            
            cur.close()
            
            return render_template('companies/dashboard.html',
                                 company=company,
                                 stats=stats,
                                 recent_activity=recent_activity,
                                 department_stats=department_stats,
                                 top_performers=top_performers)
            
        except Exception as e:
            current_app.logger.error(f"Error loading company dashboard: {e}")
            flash("Error loading dashboard data.", "danger")
            return redirect(url_for('auth.login'))

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
                    cur.execute("""
                        INSERT INTO users (username, email, password, credits, role)
                        VALUES (%s, %s, %s, 100, 'employee')
                    """, (username, email, password))
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
                        company_id, user_id, role, department, job_title, employee_id,
                        hire_date, employment_type, status, permissions, added_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                """, (
                    company['id'], user_id, role, department, job_title, employee_id,
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

            # Get comprehensive analytics data
            # This will be implemented with detailed charts and metrics
            # For now, basic structure
            
            analytics_data = {
                'company': company,
                'period': request.args.get('period', '30d')
            }
            
            cur.close()
            
            return render_template('companies/analytics.html', **analytics_data)
            
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
            # Handle settings update
            # This will be implemented with comprehensive settings
            flash("Settings updated successfully!", "success")
            return redirect(url_for('companies.settings'))
        
        return render_template('companies/settings.html', company=company)

    return companies_bp

# Create the blueprint instance
companies_bp = create_companies_blueprint()
