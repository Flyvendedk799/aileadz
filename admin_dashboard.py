from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
import logging
from datetime import datetime, timedelta

admin_dashboard_bp = Blueprint('admin_dashboard', __name__, template_folder='templates')


def require_admin():
    """Check admin access. Returns redirect response or None."""
    if 'user' not in session or session.get('role') != 'admin':
        flash("Adgang naegtet.", "danger")
        return redirect(url_for('auth.login'))
    return None


@admin_dashboard_bp.route('/')
def admin_home():
    check = require_admin()
    if check:
        return check
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    # Platform metrics
    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    total_users = cur.fetchone()['cnt']

    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'")
    total_admins = cur.fetchone()['cnt']

    # Active users (logged in last 7 days) — use last_login if available, else fallback
    active_users_7d = 0
    try:
        cur.execute("""
            SELECT COUNT(DISTINCT user_id) AS cnt FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        """)
        active_users_7d = cur.fetchone()['cnt']
    except Exception:
        pass

    # New users this month
    new_users_month = 0
    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= DATE_FORMAT(NOW(), '%%Y-%%m-01')")
        new_users_month = cur.fetchone()['cnt']
    except Exception:
        pass

    total_companies = 0
    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM companies")
        total_companies = cur.fetchone()['cnt']
    except Exception:
        pass

    total_orders = 0
    total_revenue = 0
    try:
        cur.execute("SELECT COUNT(*) AS cnt, COALESCE(SUM(price), 0) AS rev FROM course_orders")
        row = cur.fetchone()
        total_orders = row['cnt']
        total_revenue = float(row['rev'])
    except Exception:
        pass

    # Orders this month
    orders_this_month = 0
    revenue_this_month = 0
    try:
        cur.execute("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(price), 0) AS rev
            FROM course_orders WHERE created_at >= DATE_FORMAT(NOW(), '%%Y-%%m-01')
        """)
        row = cur.fetchone()
        orders_this_month = row['cnt']
        revenue_this_month = float(row['rev'])
    except Exception:
        pass

    # Total chatbot interactions
    total_chatbot_queries = 0
    queries_this_week = 0
    try:
        cur.execute("SELECT COUNT(*) AS cnt FROM chatbot_interactions")
        total_chatbot_queries = cur.fetchone()['cnt']
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        """)
        queries_this_week = cur.fetchone()['cnt']
    except Exception:
        pass

    # Recent users
    cur.execute("""
        SELECT id, username, email, role, credits, created_at
        FROM users ORDER BY id DESC LIMIT 15
    """)
    recent_users = cur.fetchall()

    # Companies list with more details
    companies = []
    try:
        cur.execute("""
            SELECT c.id, c.company_name, c.industry, c.company_size, c.subscription_status,
                   c.trial_end_date, c.created_at,
                   (SELECT COUNT(*) FROM company_users cu WHERE cu.company_id = c.id) AS employee_count,
                   (SELECT COUNT(*) FROM course_orders co WHERE co.company_id = c.id) AS order_count,
                   (SELECT COALESCE(SUM(price), 0) FROM course_orders co WHERE co.company_id = c.id) AS revenue
            FROM companies c ORDER BY c.created_at DESC LIMIT 20
        """)
        companies = cur.fetchall()
    except Exception:
        pass

    # Recent orders (platform-wide)
    recent_orders = []
    try:
        cur.execute("""
            SELECT co.order_id, co.username, co.product_title, co.price, co.status,
                   co.created_at, c.company_name
            FROM course_orders co
            LEFT JOIN companies c ON co.company_id = c.id
            ORDER BY co.created_at DESC LIMIT 10
        """)
        recent_orders = cur.fetchall()
    except Exception:
        pass

    cur.close()

    return render_template('admin_dashboard.html',
                           total_users=total_users,
                           total_admins=total_admins,
                           active_users_7d=active_users_7d,
                           new_users_month=new_users_month,
                           total_companies=total_companies,
                           total_orders=total_orders,
                           total_revenue=total_revenue,
                           orders_this_month=orders_this_month,
                           revenue_this_month=revenue_this_month,
                           total_chatbot_queries=total_chatbot_queries,
                           queries_this_week=queries_this_week,
                           recent_users=recent_users,
                           companies=companies,
                           recent_orders=recent_orders)


@admin_dashboard_bp.route('/credits', methods=['GET', 'POST'])
def credits():
    check = require_admin()
    if check:
        return check

    if request.method == 'POST':
        target_user = request.form.get('target_user')
        credit_amount = request.form.get('credit_amount')
        try:
            credit_amount = int(credit_amount)
        except ValueError:
            flash("Indtast et gyldigt antal kreditter.", "danger")
            return redirect(url_for('admin_dashboard.credits'))

        try:
            cur = current_app.mysql.connection.cursor()
            cur.execute("UPDATE users SET credits = credits + %s WHERE username = %s", (credit_amount, target_user))
            current_app.mysql.connection.commit()
            cur.close()
            flash(f"Kreditter tilfojet til {target_user}!", "success")
        except Exception as e:
            logging.error("Error updating credits: %s", e)
            flash("Fejl ved tildeling af kreditter.", "danger")
        return redirect(url_for('admin_dashboard.credits'))

    return render_template('admin_credits.html')


@admin_dashboard_bp.route('/users')
def user_list():
    check = require_admin()
    if check:
        return check
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    cur.execute("""
        SELECT u.id, u.username, u.email, u.role, u.credits, u.created_at,
               cu.company_id, c.company_name, cu.role AS company_role
        FROM users u
        LEFT JOIN company_users cu ON cu.user_id = u.id AND cu.status = 'active'
        LEFT JOIN companies c ON c.id = cu.company_id
        ORDER BY u.id DESC
    """)
    users = cur.fetchall()
    cur.close()
    return render_template('admin_users.html', users=users)


@admin_dashboard_bp.route('/users/<int:user_id>/role', methods=['POST'])
def update_user_role(user_id):
    check = require_admin()
    if check:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    new_role = request.json.get('role')
    if new_role not in ('user', 'admin'):
        return jsonify({'success': False, 'message': 'Invalid role'}), 400
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, user_id))
        current_app.mysql.connection.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'Rolle opdateret til {new_role}'})
    except Exception as e:
        logging.error("Error updating role: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500


@admin_dashboard_bp.route('/users/<int:user_id>/credits', methods=['POST'])
def update_user_credits(user_id):
    check = require_admin()
    if check:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    amount = request.json.get('amount', 0)
    try:
        amount = int(amount)
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Ugyldigt antal'}), 400
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("UPDATE users SET credits = credits + %s WHERE id = %s", (amount, user_id))
        current_app.mysql.connection.commit()
        cur.close()
        return jsonify({'success': True, 'message': f'{amount} kreditter tilfojet'})
    except Exception as e:
        logging.error("Error updating credits: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 500


@admin_dashboard_bp.route('/make-superadmin/<username>')
def make_superadmin(username):
    """One-time route to promote a user to admin."""
    if 'user' not in session:
        flash("Log ind foerst.", "danger")
        return redirect(url_for('auth.login'))
    if session.get('role') != 'admin' and session.get('user') != 'Mastek123':
        flash("Adgang naegtet.", "danger")
        return redirect(url_for('auth.login'))
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("UPDATE users SET role = 'admin' WHERE username = %s", (username,))
        current_app.mysql.connection.commit()
        affected = cur.rowcount
        cur.close()
        if affected:
            flash(f"{username} er nu superadmin!", "success")
        else:
            flash(f"Bruger '{username}' ikke fundet.", "warning")
    except Exception as e:
        logging.error("Error making superadmin: %s", e)
        flash("Fejl.", "danger")
    return redirect(url_for('admin_dashboard.admin_home'))
