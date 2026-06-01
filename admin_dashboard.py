from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
import logging
import json
from datetime import datetime, timedelta
import catalog_service as catalog

admin_dashboard_bp = Blueprint('admin_dashboard', __name__, template_folder='templates')


def _column_exists(cur, table_name, column_name):
    try:
        cur.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,))
        return cur.fetchone() is not None
    except Exception as e:
        logging.warning("Could not inspect column %s.%s: %s", table_name, column_name, e)
        return False


def require_admin():
    """Check admin access. Returns redirect response or None."""
    if 'user' not in session or session.get('role') != 'admin':
        flash("Adgang naegtet.", "danger")
        return redirect(url_for('auth.login'))
    return None


@admin_dashboard_bp.route('')
@admin_dashboard_bp.route('/')
def admin_home():
    check = require_admin()
    if check:
        return check
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    users_has_created_at = _column_exists(cur, 'users', 'created_at')

    # Platform metrics
    def _cnt(default=0):
        row = cur.fetchone()
        return row['cnt'] if row and 'cnt' in row else default

    cur.execute("SELECT COUNT(*) AS cnt FROM users")
    total_users = _cnt()

    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE role = 'admin'")
    total_admins = _cnt()

    # Active users (logged in last 7 days) — use last_login if available, else fallback
    active_users_7d = 0
    try:
        cur.execute("""
            SELECT COUNT(DISTINCT username) AS cnt FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        """)
        active_users_7d = cur.fetchone()['cnt']
    except Exception:
        pass

    # New users this month
    new_users_month = 0
    if users_has_created_at:
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= DATE_FORMAT(NOW(), '%%Y-%%m-01')")
        new_users_month = _cnt()

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

    # Recent users. The live legacy users table does not always have created_at.
    recent_users = []
    try:
        created_at_select = "created_at" if users_has_created_at else "NULL AS created_at"
        user_order = "created_at DESC, id DESC" if users_has_created_at else "id DESC"
        cur.execute("""
            SELECT id, username, email, role, credits, {created_at_select}
            FROM users ORDER BY {user_order} LIMIT 15
        """.format(created_at_select=created_at_select, user_order=user_order))
        recent_users = cur.fetchall()
    except Exception as e:
        logging.error("Error fetching recent users: %s", e)

    # Companies list with more details
    companies = []
    try:
        cur.execute("""
            SELECT c.id, c.company_name, c.company_slug, c.industry, c.company_size,
                   c.subscription_plan AS subscription_status, c.features,
                   c.trial_ends_at AS trial_end_date, c.created_at,
                   cs.enable_white_label,
                   (SELECT COUNT(*) FROM company_users cu WHERE cu.company_id = c.id) AS employee_count,
                   (SELECT COUNT(*) FROM course_orders co WHERE co.company_id = c.id) AS order_count,
                   (SELECT COALESCE(SUM(price), 0) FROM course_orders co WHERE co.company_id = c.id) AS revenue
            FROM companies c
            LEFT JOIN company_settings cs ON cs.company_id = c.id
            ORDER BY c.created_at DESC LIMIT 20
        """)
        companies = cur.fetchall()
        for co in companies:
            feats = co.get('features')
            if isinstance(feats, str):
                try:
                    feats = json.loads(feats)
                except (TypeError, ValueError):
                    feats = {}
            co['custom_branding'] = bool((feats or {}).get('custom_branding'))
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

    return render_template('fm/admin_dashboard.html',
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

    return render_template('fm/admin_credits.html')


@admin_dashboard_bp.route('/users')
def user_list():
    check = require_admin()
    if check:
        return check
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    users_has_created_at = _column_exists(cur, 'users', 'created_at')
    try:
        created_at_select = "u.created_at" if users_has_created_at else "NULL AS created_at"
        user_order = "u.created_at DESC, u.id DESC" if users_has_created_at else "u.id DESC"
        cur.execute("""
            SELECT u.id, u.username, u.email, u.role, u.credits, {created_at_select},
                   cu.company_id, c.company_name, cu.role AS company_role
            FROM users u
            LEFT JOIN company_users cu ON cu.user_id = u.id AND cu.status = 'active'
            LEFT JOIN companies c ON c.id = cu.company_id
            ORDER BY {user_order}
        """.format(created_at_select=created_at_select, user_order=user_order))
        users = cur.fetchall()
    except Exception as e:
        logging.error("Error fetching admin user company data: %s", e)
        cur.execute("""
            SELECT id, username, email, role, credits, NULL AS created_at,
                   NULL AS company_id, NULL AS company_name, NULL AS company_role
            FROM users ORDER BY id DESC
        """)
        users = cur.fetchall()
    cur.close()
    return render_template('fm/admin_users.html', users=users)


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


@admin_dashboard_bp.route('/catalog')
def admin_catalog():
    check = require_admin()
    if check:
        return check
    stats = catalog.catalog_stats()
    categories = catalog.get_categories()[:80]
    vendors = catalog.get_vendors()[:80]
    import_drafts = catalog.list_import_drafts()[:10]
    ai_jobs = catalog.list_ai_category_jobs()[:10]
    return render_template(
        'fm/admin_catalog.html',
        stats=stats,
        categories=categories,
        vendors=vendors,
        import_drafts=import_drafts,
        ai_jobs=ai_jobs,
    )


@admin_dashboard_bp.route('/catalog/import', methods=['POST'])
def admin_catalog_import():
    check = require_admin()
    if check:
        return check
    upload = request.files.get('catalog_csv')
    if not upload or not upload.filename:
        flash("Vaelg en CSV-fil.", "danger")
        return redirect(url_for('admin_dashboard.admin_catalog'))
    try:
        parsed = catalog.parse_catalog_csv(upload)
        draft = catalog.save_import_draft(parsed, filename=upload.filename, uploaded_by=session.get('user', ''))
        flash("CSV importkladde er klar til gennemgang.", "success")
        return redirect(url_for('admin_dashboard.admin_catalog_import_preview', job_id=draft['job_id']))
    except Exception as e:
        current_app.logger.error("Catalog CSV import failed: %s", e)
        flash(f"CSV kunne ikke parses: {e}", "danger")
        return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/import/<job_id>')
def admin_catalog_import_preview(job_id):
    check = require_admin()
    if check:
        return check
    draft = catalog.get_import_draft(job_id)
    if not draft:
        flash("Importkladde ikke fundet.", "warning")
        return redirect(url_for('admin_dashboard.admin_catalog'))
    products = [catalog.normalize_product(product, overrides={}) for product in draft.get('products', [])[:200]]
    return render_template('fm/admin_catalog_import_preview.html', draft=draft, products=products)


@admin_dashboard_bp.route('/catalog/import/<job_id>/confirm', methods=['POST'])
def admin_catalog_import_confirm(job_id):
    check = require_admin()
    if check:
        return check
    draft = catalog.confirm_import_draft(job_id)
    if not draft:
        flash("Importkladde ikke fundet.", "warning")
    else:
        flash("CSV import er bekraeftet og kataloget er opdateret.", "success")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/import/<job_id>/cancel', methods=['POST'])
def admin_catalog_import_cancel(job_id):
    check = require_admin()
    if check:
        return check
    catalog.delete_import_draft(job_id)
    flash("Importkladde slettet.", "info")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/ai-categorize/start', methods=['POST'])
def admin_catalog_ai_start():
    check = require_admin()
    if check:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    job = catalog.create_ai_category_job(created_by=session.get('user', ''))
    return jsonify({'success': True, 'job_id': job['job_id'], 'total': job['total']})


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/batch', methods=['POST'])
def admin_catalog_ai_batch(job_id):
    check = require_admin()
    if check:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    job = catalog.process_ai_category_batch(job_id, batch_size=8)
    if not job:
        return jsonify({'success': False, 'message': 'Job not found'}), 404
    diff = catalog.ai_category_diff(job)
    return jsonify({
        'success': True,
        'job_id': job_id,
        'status': job.get('status'),
        'processed': job.get('processed', 0),
        'total': job.get('total', 0),
        'changed_count': diff['changed_count'],
        'errors': job.get('errors', []),
    })


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>')
def admin_catalog_ai_preview(job_id):
    check = require_admin()
    if check:
        return check
    job = catalog.get_ai_category_job(job_id)
    if not job:
        flash("AI-kategoriseringsjob ikke fundet.", "warning")
        return redirect(url_for('admin_dashboard.admin_catalog'))
    diff = catalog.ai_category_diff(job)
    return render_template('fm/admin_catalog_ai_preview.html', job=job, diff=diff)


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/confirm', methods=['POST'])
def admin_catalog_ai_confirm(job_id):
    check = require_admin()
    if check:
        return check
    job = catalog.confirm_ai_category_job(job_id)
    if not job:
        flash("AI-kategoriseringsjob ikke fundet.", "warning")
    else:
        flash("AI-kategorier bekraeftet og kataloget er opdateret.", "success")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/cancel', methods=['POST'])
def admin_catalog_ai_cancel(job_id):
    check = require_admin()
    if check:
        return check
    catalog.delete_ai_category_job(job_id)
    flash("AI-kategoriseringsjob slettet.", "info")
    return redirect(url_for('admin_dashboard.admin_catalog'))


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
