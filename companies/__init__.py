"""
Enterprise Company Management System
Multi-tenant B2B solution for corporate learning management
"""

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
from datetime import datetime, timedelta
import json
import secrets
from werkzeug.security import generate_password_hash
from auth_decorators import can, require_capability, require_role
from branding_service import (
    get_branding,
    has_custom_branding_feature,
    is_whitelabel_active,
    log_branding_change,
    save_branding_settings,
    set_custom_branding_feature,
)

# Company roles HR may hand out when adding an employee. Granting company_admin
# additionally needs the ``company.admins`` capability (company admins only).
ASSIGNABLE_ROLES = ('employee', 'team_lead', 'department_head', 'hr_manager', 'company_admin')


def create_companies_blueprint():
    companies_bp = Blueprint('companies', __name__, template_folder='templates')

    def require_company_admin(capability='company.employees'):
        """Guard for the company-administration pages. ``capability`` is asked of
        the role matrix in auth_decorators (the same answer the nav links get), so
        link visibility and route access always agree."""
        if 'user' not in session:
            flash("Log ind for at bruge denne funktion.", "danger")
            return redirect(url_for('auth.login'))

        if not session.get('company_id') or not can(capability):
            flash("Du har ikke tilladelse til at bruge denne funktion.", "danger")
            return redirect(url_for('dashboard.dashboard'))
        return None

    def require_platform_admin():
        if 'user' not in session or session.get('role') != 'admin':
            flash("Kun platform-administratorer har adgang.", "danger")
            return redirect(url_for('auth.login'))
        return None

    def get_company_by_id(company_id):
        """Load company row for platform admin (no membership required)."""
        if not company_id:
            return None
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("""
                SELECT c.*,
                       cs.enable_white_label, cs.hide_platform_branding, cs.branding_status
                FROM companies c
                LEFT JOIN company_settings cs ON cs.company_id = c.id
                WHERE c.id = %s
            """, (company_id,))
            row = cur.fetchone()
            cur.close()
            if row and row.get('features') and isinstance(row['features'], str):
                try:
                    row['features_parsed'] = json.loads(row['features'])
                except (TypeError, ValueError):
                    row['features_parsed'] = {}
            elif row:
                row['features_parsed'] = row.get('features') if isinstance(row.get('features'), dict) else {}
            return row
        except Exception as e:
            current_app.logger.error(f"get_company_by_id: {e}")
            return None

    def _resolve_branding_company_id(explicit_id=None):
        # S-2.5: resolving a company for one request must NOT change which
        # company the admin is "acting as" (that is an explicit POST, audited).
        if explicit_id is not None and session.get('role') == 'admin':
            return explicit_id
        if session.get('role') == 'admin':
            acting = request.args.get('company_id', type=int) or session.get('admin_acting_company_id')
            if acting:
                return acting
        company = get_company_context()
        if company:
            return company['id']
        return session.get('company_id')

    def get_company_context():
        """Get current user's company context.

        Honors admin impersonation (session['admin_acting_company_id']) the same
        way the HR dashboard does, so a platform admin acting as a tenant sees
        that tenant's company BI / benchmark / reports too.
        """
        if 'user' not in session:
            return None

        acting = session.get('admin_acting_company_id')
        if session.get('role') == 'admin' and acting:
            try:
                cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                cur.execute("SELECT c.* FROM companies c WHERE c.id = %s", (acting,))
                row = cur.fetchone()
                cur.close()
                if row:
                    if session.get('company_id') != row['id']:
                        session['company_id'] = row['id']
                    row['user_role'] = 'company_admin'
                    row['department'] = None
                    row['permissions'] = None
                    return row
            except Exception as e:
                current_app.logger.error(f"Error resolving acting company: {e}")

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
    @require_role('admin')
    def register_company():
        """Company registration — super admin only. Creates company + assigns HR manager."""
        if request.method == 'POST':
            # Company details
            company_name = request.form.get('company_name', '').strip()
            company_domain = request.form.get('company_domain', '').strip()
            industry = request.form.get('industry', '').strip()
            if industry == 'other':
                industry = request.form.get('industry_custom', '').strip() or 'Andet'
            company_size = request.form.get('company_size', '11-50')
            country = request.form.get('country', 'Denmark')
            city = request.form.get('city', '').strip()

            # HR manager mode: 'new' or 'existing'
            hr_mode = request.form.get('hr_mode', 'new')
            existing_user_id = request.form.get('existing_user_id', '').strip()

            # New HR manager details
            hr_name = request.form.get('hr_name', '').strip()
            hr_username = request.form.get('hr_username', '').strip()
            hr_email = request.form.get('hr_email', '').strip()
            hr_password = request.form.get('hr_password', '').strip()
            hr_phone = request.form.get('hr_phone', '').strip()
            hr_job_title = request.form.get('job_title', '').strip() or 'HR Manager'

            # Validation
            if not company_name:
                flash("Virksomhedsnavn er påkrævet.", "danger")
                return render_template('fm/company_register.html')

            if hr_mode == 'new' and not all([hr_name, hr_username, hr_email]):
                flash("Udfyld venligst navn, brugernavn og e-mail for kontaktpersonen.", "danger")
                return render_template('fm/company_register.html')

            # S-2.4: the password is optional. Without one, the HR manager gets a
            # one-time set-password link by e-mail (no password is handled by a human).
            if hr_mode == 'new' and hr_password:
                from password_policy import validate_password
                _pw_errors = validate_password(hr_password, hr_username, hr_email)
                if _pw_errors:
                    for _e in _pw_errors:
                        flash(_e, "danger")
                    return render_template('fm/company_register.html')

            if hr_mode == 'existing' and not existing_user_id:
                flash("Vælg venligst en eksisterende bruger.", "danger")
                return render_template('fm/company_register.html')

            # Generate company slug
            company_slug = ''.join(c.lower() if c.isalnum() else '-' for c in company_name)
            company_slug = company_slug.strip('-')

            generated_password = None
            invite_needed = False

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
                        return render_template('fm/company_register.html')
                    user_id = user_row['id']
                    hr_name = user_row['username']
                    hr_email = user_row['email']
                else:
                    # Create new user as HR manager
                    cur.execute("SELECT id FROM users WHERE email = %s OR username = %s", (hr_email, hr_username))
                    if cur.fetchone():
                        flash("En bruger med dette brugernavn eller e-mail eksisterer allerede. Brug 'Eksisterende bruger' i stedet.", "danger")
                        cur.close()
                        return render_template('fm/company_register.html')

                    # Password provided by the admin (policy-checked above), or an
                    # unusable random one until the invite link is used (Part A token flow).
                    invite_needed = not hr_password
                    generated_password = hr_password or None
                    hashed_password = generate_password_hash(hr_password or secrets.token_urlsafe(48))

                    cur.execute("""
                        INSERT INTO users (username, email, password, credits, role)
                        VALUES (%s, %s, %s, 1000, 'user')
                    """, (hr_username, hr_email, hashed_password))
                    user_id = cur.lastrowid

                # The first user of a new company is its company_admin (superset of
                # hr_manager). Before, only hr_manager existed, so the admin-only
                # settings (SSO, API keys, billing policy) were unreachable.
                hr_permissions = json.dumps({
                    "manage_users": True,
                    "view_analytics": True,
                    "export_data": True,
                    "manage_billing": True,
                    "manage_integrations": True,
                    "manage_branding": True,
                })
                cur.execute("""
                    INSERT INTO company_users (
                        company_id, user_id, username, full_name, email, role, department, job_title,
                        status, phone, permissions, added_by
                    ) VALUES (%s, %s, %s, %s, %s, 'company_admin', 'Human Resources', %s, 'active', %s, %s, %s)
                """, (
                    company_id, user_id, hr_username or hr_name, hr_name, hr_email,
                    hr_job_title, hr_phone, hr_permissions, session.get('user_id')
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

                invite_sent = False
                if invite_needed:
                    try:
                        from account_flows import send_invite
                        invite_sent = send_invite(user_id, email=hr_email, name=hr_name,
                                                  username=hr_username,
                                                  company={'id': company_id, 'company_name': company_name})
                    except Exception as _mail_err:
                        current_app.logger.warning("HR invite e-mail failed: %s", _mail_err)

                # Never echo a password back (S-2.4); say how the HR manager gets in.
                if hr_mode == 'new':
                    return render_template('fm/register_success.html',
                        company_name=company_name,
                        company_slug=company_slug,
                        hr_name=hr_name,
                        hr_email=hr_email,
                        is_new_user=True,
                        invite_needed=invite_needed,
                        invite_sent=invite_sent
                    )
                else:
                    return render_template('fm/register_success.html',
                        company_name=company_name,
                        company_slug=company_slug,
                        hr_name=hr_name,
                        hr_email=hr_email,
                        is_new_user=False
                    )

            except Exception as e:
                current_app.logger.error(f"Error registering company: {e}")
                flash("Der opstod en fejl under oprettelsen. Prøv venligst igen.", "danger")
                if 'cur' in locals():
                    cur.close()
                return render_template('fm/company_register.html')

        return render_template('fm/company_register.html')

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
            flash("Virksomhedsoplysningerne blev ikke fundet.", "danger")
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
                WHERE company_id = %s AND department IS NOT NULL AND department != ''
                ORDER BY department
            """, (company['id'],))
            departments = [row['department'] for row in cur.fetchall()]
            
            cur.close()
            
            return render_template('fm/employees.html',
                                 company=company,
                                 employees=employees,
                                 departments=departments,
                                 active_hr_page='employees')

        except Exception as e:
            current_app.logger.error(f"Error loading employees: {e}")
            flash("Medarbejderne kunne ikke indlæses.", "danger")
            return redirect(url_for('hr_dashboard.dashboard'))

    @companies_bp.route('/employees/add', methods=['GET', 'POST'])
    def add_employee():
        """Add new employee to company"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Virksomhedsoplysningerne blev ikke fundet.", "danger")
            return redirect(url_for('auth.login'))
        
        # Helper: load departments for re-renders / GET dropdown.
        def _load_departments():
            try:
                dcur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                dcur.execute("SELECT department_name FROM company_departments WHERE company_id = %s", (company['id'],))
                rows = [r['department_name'] for r in dcur.fetchall()]
                dcur.close()
                return rows
            except Exception as e:
                current_app.logger.error(f"Error loading departments: {e}")
                return []

        # Possible managers (N-2.3): active members of THIS company.
        def _load_managers():
            try:
                mcur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                mcur.execute("""SELECT cu.user_id, COALESCE(cu.full_name, u.username) AS name
                                FROM company_users cu JOIN users u ON u.id = cu.user_id
                                WHERE cu.company_id = %s AND cu.status = 'active' ORDER BY name""", (company['id'],))
                rows = mcur.fetchall() or []
                mcur.close()
                return rows
            except Exception as e:
                current_app.logger.error(f"Error loading managers: {e}")
                return []

        # Seat governance snapshot for the template (informational banner).
        try:
            import seat_governance
            seat_status = seat_governance.trial_status(company['id'])
        except Exception:
            seat_status = None

        if request.method == 'POST':
            # Seat governance gate — enforce the same limit the REST/SCIM paths
            # do. Company-scoped; fails OPEN inside seat_governance on any glitch.
            try:
                import seat_governance
                seat_ok, seat_reason = seat_governance.can_add_employee(company['id'])
            except Exception:
                seat_ok, seat_reason = True, None
            if not seat_ok:
                flash(seat_reason or "Du kan ikke tilføje flere medarbejdere lige nu.", "danger")
                return render_template('fm/add_employee.html', company=company,
                                       departments=_load_departments(), managers=_load_managers(),
                                       seat_status=seat_status,
                                       active_hr_page='employees')

            # Employee details
            full_name = request.form.get('full_name', '').strip()
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()
            # No password typed = invite: the employee gets a set-password link and
            # picks their own (HR never has to know or pass on a password).
            role = request.form.get('role', 'employee')
            if role not in ASSIGNABLE_ROLES or (role == 'company_admin' and not can('company.admins')):
                role = 'employee'
            department = request.form.get('department', '').strip()
            job_title = request.form.get('job_title', '').strip()
            employee_id = request.form.get('employee_id', '').strip()
            hire_date = request.form.get('hire_date', '')
            employment_type = request.form.get('employment_type', 'full_time')
            try:
                manager_user_id = int(request.form.get('manager_user_id') or 0) or None
            except (TypeError, ValueError):
                manager_user_id = None

            # Validation
            if not all([full_name, username, email, department, job_title]):
                flash("Udfyld venligst alle påkrævede felter.", "danger")
                return render_template('fm/add_employee.html', company=company,
                                       departments=_load_departments(), managers=_load_managers(),
                                       seat_status=seat_status,
                                       active_hr_page='employees')

            # S-2.4: a typed password must satisfy the policy; a blank one means
            # "invite": the employee gets a set-password link instead.
            if password:
                from password_policy import validate_password
                _pw_errors = validate_password(password, username, email)
                if _pw_errors:
                    for _e in _pw_errors:
                        flash(_e, "danger")
                    return render_template('fm/add_employee.html', company=company,
                                           departments=_load_departments(), managers=_load_managers(),
                                           seat_status=seat_status,
                                           active_hr_page='employees')
            invite_new_user = False
            
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
                        flash("Brugeren er allerede medlem af din virksomhed.", "warning")
                        cur.close()
                        return render_template('fm/add_employee.html', company=company,
                                               departments=_load_departments(), managers=_load_managers(),
                                               seat_status=seat_status,
                                               active_hr_page='employees')
                else:
                    # Create new user (unusable random password until the invite is used)
                    invite_new_user = not password
                    hashed_password = generate_password_hash(password or secrets.token_urlsafe(48))
                    cur.execute("""
                        INSERT INTO users (username, email, password, credits, role)
                        VALUES (%s, %s, %s, 100, 'user')
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
                
                # The manager must be an active member of THIS company (never another tenant).
                if manager_user_id:
                    cur.execute("SELECT 1 FROM company_users WHERE company_id = %s AND user_id = %s AND status = 'active'",
                                (company['id'], manager_user_id))
                    if not cur.fetchone():
                        manager_user_id = None

                # Add to company
                cur.execute("""
                    INSERT INTO company_users (
                        company_id, user_id, username, full_name, email, role, department, job_title, employee_id,
                        hire_date, employment_type, status, permissions, added_by, manager_user_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                """, (
                    company['id'], user_id, username, full_name, email, role, department, job_title, employee_id,
                    hire_date if hire_date else None, employment_type,
                    json.dumps(permissions.get(role, permissions['employee'])),
                    session.get('user_id'), manager_user_id
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

                # Integration event (webhook subscribers) - best-effort, post-commit.
                try:
                    from event_bus import emit_event
                    emit_event(company['id'], 'employee.added', {
                        'user_id': user_id, 'username': username, 'email': email,
                        'name': full_name, 'role': role, 'department': department,
                        'job_title': job_title, 'source': 'hr_screen'})
                except Exception as ev_err:
                    current_app.logger.debug(f"employee.added event skipped: {ev_err}")

                # Best-effort branded welcome/invite email to the new employee.
                # Guarded so a mail failure (or no backend at all) NEVER blocks
                # the add — no-ops cleanly when MAIL_SERVER/MAIL_DEFAULT_SENDER
                # are not configured (ops-gated).
                try:
                    if invite_new_user:
                        from account_flows import send_invite
                        send_invite(user_id, email=email, name=full_name, username=username,
                                    company=company)
                    else:
                        from email_service import send_employee_welcome
                        try:
                            login_url = url_for('auth.login', _external=True)
                        except Exception:
                            login_url = ''
                        send_employee_welcome(
                            company,
                            {
                                'email': email,
                                'name': full_name,
                                'username': username,
                            },
                            login_url=login_url,
                        )
                except Exception as mail_err:
                    current_app.logger.debug(
                        f"Employee welcome email skipped: {mail_err}"
                    )

                flash(f"Medarbejderen '{username}' er tilføjet.", "success")
                return redirect(url_for('companies.employees'))
                
            except Exception as e:
                current_app.logger.error(f"Error adding employee: {e}")
                flash("Medarbejderen kunne ikke tilføjes. Prøv igen om lidt.", "danger")
                if 'cur' in locals():
                    cur.close()
        
        # Get departments for dropdown
        departments = _load_departments()

        return render_template('fm/add_employee.html', company=company,
                               departments=departments, managers=_load_managers(),
                               seat_status=seat_status, active_hr_page='employees')

    @companies_bp.route('/employees/<int:user_id>/edit', methods=['GET', 'POST'])
    def edit_employee(user_id):
        """Edit employee details"""
        auth_check = require_company_admin()
        if auth_check:
            return auth_check
        
        company = get_company_context()
        if not company:
            flash("Virksomhedsoplysningerne blev ikke fundet.", "danger")
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
                flash("Medarbejderen blev ikke fundet.", "danger")
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
                # Only a company admin may grant or take away the admin role.
                if (not can('company.admins')
                        and role != employee['role'] and 'company_admin' in (role, employee['role'])):
                    role = employee['role']

                # Manager-of-record: must be another active user in THIS company
                # (company-scoped to prevent cross-tenant assignment), and never
                # the employee themselves. Empty -> NULL (no manager).
                manager_raw = request.form.get('manager_user_id', '').strip()
                manager_user_id = None
                if manager_raw:
                    try:
                        candidate = int(manager_raw)
                    except (TypeError, ValueError):
                        candidate = None
                    if candidate and candidate != user_id:
                        mcur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
                        mcur.execute("""
                            SELECT user_id FROM company_users
                            WHERE company_id = %s AND user_id = %s AND status = 'active'
                        """, (company['id'], candidate))
                        if mcur.fetchone():
                            manager_user_id = candidate
                        mcur.close()

                cur.execute("""
                    UPDATE company_users
                    SET role = %s, department = %s, job_title = %s, employee_id = %s,
                        employment_type = %s, status = %s, manager_user_id = %s, updated_at = NOW()
                    WHERE company_id = %s AND user_id = %s
                """, (role, department, job_title, employee_id, employment_type, status,
                      manager_user_id, company['id'], user_id))
                
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
                try:
                    from event_bus import emit_event
                    emit_event(company['id'], 'employee.updated', {
                        'user_id': user_id, 'role': role, 'department': department,
                        'job_title': job_title, 'status': status, 'source': 'hr_screen'})
                except Exception as ev_err:
                    current_app.logger.debug(f"employee.updated event skipped: {ev_err}")
                flash("Medarbejderens oplysninger er opdateret.", "success")
                return redirect(url_for('companies.employees'))
            
            # Get departments for dropdown
            cur.execute("SELECT department_name FROM company_departments WHERE company_id = %s", (company['id'],))
            departments = [row['department_name'] for row in cur.fetchall()]

            # Same-company users for the manager-of-record dropdown (exclude the
            # employee themselves). Company-scoped.
            cur.execute("""
                SELECT cu.user_id,
                       COALESCE(cu.full_name, u.username) AS name,
                       cu.department, cu.job_title
                FROM company_users cu
                JOIN users u ON cu.user_id = u.id
                WHERE cu.company_id = %s AND cu.status = 'active' AND cu.user_id <> %s
                ORDER BY name
            """, (company['id'], user_id))
            managers = cur.fetchall() or []

            cur.close()

            return render_template('fm/edit_employee.html',
                                 company=company, employee=employee, departments=departments,
                                 managers=managers,
                                 active_hr_page='employees')
            
        except Exception as e:
            current_app.logger.error(f"Error editing employee: {e}")
            flash("Medarbejderens oplysninger kunne ikke indlæses.", "danger")
            return redirect(url_for('companies.employees'))

    @companies_bp.route('/analytics')
    def analytics():
        """Virksomheds-BI is merged into Læringsanalyse (N-4.1)."""
        return redirect(url_for('hr_dashboard.learning_analytics', period=request.args.get('period', '30d')))

    @companies_bp.route('/benchmarking')
    def benchmarking_page():
        """The canonical benchmarking page lives in the HR workspace (N-4.1)."""
        return redirect(url_for('hr_dashboard.benchmarking_view'))

    @companies_bp.route('/settings', methods=['GET', 'POST'])
    def settings():
        """Company settings management (GET lives in the settings hub, N-4.6)"""
        auth_check = require_company_admin('company.settings')
        if auth_check:
            return auth_check
        from settings_hub import hub_redirect
        _hub = hub_redirect('virksomhed')
        if _hub is not None:
            return _hub
        
        company = get_company_context()
        if not company:
            flash("Virksomhedsoplysningerne blev ikke fundet.", "danger")
            return redirect(url_for('auth.login'))
        
        if request.method == 'POST':
            try:
                cur = current_app.mysql.connection.cursor()
                cur.execute("""
                    UPDATE companies SET company_name = %s, industry = %s, updated_at = NOW()
                    WHERE id = %s
                """, (
                    request.form.get('company_name', company.get('company_name', '')),
                    request.form.get('industry', ''),
                    company['id']
                ))
                # Also upsert company_settings for branding/preferences
                branding_data = {
                    'language': request.form.get('language', 'da'),
                    'timezone': request.form.get('timezone', 'Europe/Copenhagen'),
                    # The support-email field used to be ignored because contact_email
                    # (always posted) took precedence. The dedicated field wins now.
                    'support_email': (request.form.get('support_email') or request.form.get('contact_email') or '').strip(),
                    'contact_email': (request.form.get('contact_email') or '').strip(),
                    'support_phone': request.form.get('support_phone', ''),
                    'company_website': request.form.get('website', ''),
                }
                save_branding_settings(company['id'], branding_data, as_draft=False)
                current_app.mysql.connection.commit()
                cur.close()
                # Update session
                if request.form.get('company_name'):
                    session['company_name'] = request.form['company_name']
                flash("Indstillinger gemt!", "success")
            except Exception as e:
                current_app.logger.error(f"Settings update error: {e}")
                flash("Indstillingerne kunne ikke gemmes.", "danger")
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

        return render_template('fm/company_settings.html', company=company, settings=settings)

    @companies_bp.route('/admin')
    @require_role('admin')
    def admin_companies_list():
        """Platform admin: list all tenants with quick controls."""
        companies = []
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            # Pre-aggregate employees + orders in derived tables (one grouped scan
            # each, covered by idx_cu_company_status / idx_co_company_created)
            # instead of 4 correlated subqueries per company row.
            cur.execute("""
                SELECT c.id, c.company_name, c.company_slug, c.industry, c.status,
                       c.subscription_plan, c.created_at, c.features,
                       cs.enable_white_label,
                       COALESCE(emp.employee_count, 0) AS employee_count,
                       COALESCE(ord.orders_total, 0) AS orders_total,
                       COALESCE(ord.orders_30d, 0) AS orders_30d,
                       ord.last_order_at AS last_order_at
                FROM companies c
                LEFT JOIN company_settings cs ON cs.company_id = c.id
                LEFT JOIN (
                    SELECT company_id, COUNT(*) AS employee_count
                    FROM company_users WHERE status = 'active' GROUP BY company_id
                ) emp ON emp.company_id = c.id
                LEFT JOIN (
                    SELECT company_id,
                           COUNT(*) AS orders_total,
                           COUNT(CASE WHEN created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN 1 END) AS orders_30d,
                           MAX(created_at) AS last_order_at
                    FROM course_orders GROUP BY company_id
                ) ord ON ord.company_id = c.id
                ORDER BY c.company_name ASC
            """)
            companies = cur.fetchall()
            cur.close()
            for co in companies:
                feats = co.get('features')
                if isinstance(feats, str):
                    try:
                        feats = json.loads(feats)
                    except (TypeError, ValueError):
                        feats = {}
                co['custom_branding'] = bool((feats or {}).get('custom_branding'))
                # B11: composite health signal (derived, not stored).
                emp = co.get('employee_count') or 0
                o30 = co.get('orders_30d') or 0
                ot = co.get('orders_total') or 0
                if emp == 0 or ot == 0:
                    co['health'], co['health_label'] = 'red', 'Inaktiv'
                elif o30 == 0:
                    co['health'], co['health_label'] = 'amber', 'Lav aktivitet'
                else:
                    co['health'], co['health_label'] = 'green', 'Sund'
        except Exception as e:
            current_app.logger.error(f"admin_companies_list: {e}")
        from admin_lists import filter_rows, list_args, paginate
        _page, _per, q = list_args(request)
        companies = filter_rows(companies, q, ['company_name', 'company_slug', 'industry', 'subscription_plan'])
        pg = paginate(companies, _page, _per)
        return render_template(
            'fm/admin_companies.html',
            companies=pg['items'],
            pg=pg,
            q=q,
            acting_company_id=session.get('admin_acting_company_id'),
        )

    @companies_bp.route('/admin/<int:company_id>', methods=['GET', 'POST'])
    @require_role('admin')
    def admin_company_detail(company_id):
        """Platform admin: tenant settings and feature toggles."""
        company = get_company_by_id(company_id)
        if not company:
            flash("Virksomhed ikke fundet.", "danger")
            return redirect(url_for('companies.admin_companies_list'))

        # Viewing a tenant's detail page is read context only; it must not
        # silently switch the admin into that company (S-2.5). Use the explicit
        # "act as" button (POST /admin/impersonate/<id>) for that.

        if request.method == 'POST':
            action = request.form.get('action', 'save')
            try:
                if action == 'toggle_branding':
                    enabled = request.form.get('enabled') == '1'
                    set_custom_branding_feature(company_id, enabled)
                    flash(
                        f"Custom branding {'aktiveret' if enabled else 'deaktiveret'} for {company['company_name']}.",
                        "success",
                    )
                elif action == 'toggle_whitelabel':
                    enabled = request.form.get('enabled') == '1'
                    save_branding_settings(company_id, {'enable_white_label': 1 if enabled else 0}, as_draft=False)
                    flash(
                        f"White-label {'aktiveret' if enabled else 'deaktiveret'} for {company['company_name']}.",
                        "success",
                    )
                else:
                    cur = current_app.mysql.connection.cursor()
                    cur.execute("""
                        UPDATE companies
                        SET company_name = %s, industry = %s, subscription_plan = %s,
                            status = %s, max_employees = %s, updated_at = NOW()
                        WHERE id = %s
                    """, (
                        request.form.get('company_name', company['company_name']),
                        request.form.get('industry', company.get('industry') or ''),
                        request.form.get('subscription_plan', company.get('subscription_plan') or 'trial'),
                        request.form.get('status', company.get('status') or 'active'),
                        request.form.get('max_employees', company.get('max_employees') or 50),
                        company_id,
                    ))
                    current_app.mysql.connection.commit()
                    cur.close()
                    flash("Virksomhedsindstillinger gemt.", "success")
            except Exception as e:
                current_app.logger.error(f"admin_company_detail save: {e}")
                flash("Kunne ikke gemme.", "danger")
            return redirect(url_for('companies.admin_company_detail', company_id=company_id))

        features = company.get('features_parsed') or {}
        employee_count = 0
        hr_contacts = []
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM company_users WHERE company_id = %s AND status = 'active'",
                (company_id,),
            )
            employee_count = cur.fetchone()['cnt']
            cur.execute("""
                SELECT full_name, email, role, username
                FROM company_users
                WHERE company_id = %s AND role IN ('hr_manager', 'company_admin') AND status = 'active'
                ORDER BY role, full_name LIMIT 5
            """, (company_id,))
            hr_contacts = cur.fetchall()
            cur.close()
        except Exception:
            pass

        return render_template(
            'fm/admin_company_detail.html',
            company=company,
            features=features,
            custom_branding=bool(features.get('custom_branding')),
            whitelabel_active=bool(company.get('enable_white_label')),
            employee_count=employee_count,
            hr_contacts=hr_contacts,
        )

    @companies_bp.route('/branding', methods=['GET', 'POST'])
    @companies_bp.route('/branding/<int:company_id>', methods=['GET', 'POST'])
    @require_capability('company.branding')
    def branding(company_id=None):
        """Unified branding hub for HR admins (opened through the settings hub)."""
        if company_id is None:
            from settings_hub import hub_redirect
            _hub = hub_redirect('branding')
            if _hub is not None:
                return _hub
        resolved_id = _resolve_branding_company_id(company_id)
        company = get_company_by_id(resolved_id) if session.get('role') == 'admin' and resolved_id else get_company_context()
        if not company and resolved_id:
            company = get_company_by_id(resolved_id)

        if not company and session.get('role') != 'admin':
            flash("Virksomhedsoplysningerne blev ikke fundet.", "danger")
            return redirect(url_for('auth.login'))

        company_id = resolved_id or (company['id'] if company else None)
        if not company_id:
            flash("Vælg en virksomhed for at administrere branding.", "warning")
            return redirect(url_for('companies.admin_companies_list'))

        features = (company.get('features_parsed') if company else None) or {}
        if not features and company:
            raw = company.get('features')
            if isinstance(raw, str):
                try:
                    features = json.loads(raw)
                except (TypeError, ValueError):
                    features = {}
            elif isinstance(raw, dict):
                features = raw
        branding_enabled = has_custom_branding_feature(company_id) or session.get('role') == 'admin'

        if request.method == 'POST':
            action = request.form.get('action', 'save_draft')
            data = {
                'company_display_name': request.form.get('company_display_name', '').strip(),
                'company_tagline': request.form.get('company_tagline', '').strip(),
                'company_description': request.form.get('company_description', '').strip(),
                'company_website': request.form.get('company_website', '').strip(),
                'support_email': request.form.get('support_email', '').strip(),
                'support_phone': request.form.get('support_phone', '').strip(),
                'primary_color': request.form.get('primary_color', '#0b6b63'),
                'secondary_color': request.form.get('secondary_color', '#2563eb'),
                'accent_color': request.form.get('accent_color', '#f59e0b'),
                'background_color': request.form.get('background_color', '#f8fafc'),
                'text_color': request.form.get('text_color', '#1f2937'),
                'font_family': request.form.get('font_family', 'Inter, sans-serif'),
                'font_size_base': request.form.get('font_size_base', '14px'),
                'border_radius': request.form.get('border_radius', '8px'),
                'logo_url': request.form.get('logo_url', '').strip(),
                'favicon_url': request.form.get('favicon_url', '').strip(),
                'custom_css': request.form.get('custom_css', ''),
                'custom_js': request.form.get('custom_js', ''),
                'language': request.form.get('language', 'da'),
                'timezone': request.form.get('timezone', 'Europe/Copenhagen'),
                'enable_white_label': 1 if request.form.get('enable_white_label') else 0,
                'hide_platform_branding': 1 if request.form.get('hide_platform_branding') else 0,
            }
            if not branding_enabled:
                data.pop('hide_platform_branding', None)
                data.pop('custom_css', None)
                data.pop('custom_js', None)

            as_draft = action == 'save_draft'
            ok = save_branding_settings(company_id, data, as_draft=as_draft, user_id=session.get('user_id'))
            if ok:
                log_branding_change(
                    company_id, action, '', 'updated', session.get('user_id'),
                    f'Branding {action} via hub',
                )
                flash("Branding gemt!" if as_draft else "Branding publiceret!", "success")
            else:
                flash("Branding kunne ikke gemmes.", "danger")
            return redirect(url_for('companies.branding', company_id=company_id))

        settings = {}
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            cur.execute("SELECT * FROM company_settings WHERE company_id = %s", (company_id,))
            settings = cur.fetchone() or {}
            cur.execute("""
                SELECT csh.*, u.username AS changed_by_username
                FROM company_settings_history csh
                LEFT JOIN users u ON csh.changed_by = u.id
                WHERE csh.company_id = %s
                ORDER BY csh.created_at DESC LIMIT 30
            """, (company_id,))
            settings_history = cur.fetchall()
            cur.execute(
                "SELECT id, template_name, template_category, primary_color, secondary_color, accent_color "
                "FROM company_theme_templates WHERE is_active = 1 ORDER BY template_category, template_name"
            )
            theme_templates = cur.fetchall()
            cur.close()
        except Exception as e:
            current_app.logger.error(f"Branding hub load error: {e}")
            settings_history = []
            theme_templates = []

        live_branding = get_branding(company_id, preview=True)
        if settings.get('branding_status') == 'draft' and settings.get('branding_draft'):
            draft = settings['branding_draft']
            if isinstance(draft,str):
                try: draft=json.loads(draft)
                except ValueError: draft={}
            settings={**settings,**draft}
        return render_template(
            'fm/branding.html',
            company=company,
            settings=settings,
            branding=live_branding,
            branding_enabled=branding_enabled,
            whitelabel_active=is_whitelabel_active(company_id),
            settings_history=settings_history,
            theme_templates=theme_templates,
            features=features,
        )

    @companies_bp.route('/branding/toggle-feature', methods=['POST'])
    @require_role('admin')
    def toggle_branding_feature():
        """Platform admin: enable/disable custom_branding for a tenant."""
        company_id = request.form.get('company_id', type=int)
        enabled = request.form.get('enabled') == '1'
        if company_id and set_custom_branding_feature(company_id, enabled):
            flash("Branding-adgang opdateret.", "success")
        else:
            flash("Kunne ikke opdatere branding-adgang.", "danger")
        if company_id and request.form.get('back') == 'branding':
            # The "Aktivér" button on the branding hub returns there.
            return redirect(url_for('companies.branding', company_id=company_id))
        if company_id:
            return redirect(url_for('companies.admin_company_detail', company_id=company_id))
        return redirect(request.referrer or url_for('companies.admin_companies_list'))

    return companies_bp

# Create the blueprint instance
companies_bp = create_companies_blueprint()
