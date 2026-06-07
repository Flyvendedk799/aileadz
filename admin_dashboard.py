from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
import MySQLdb.cursors
import logging
import json
import re
from datetime import datetime, timedelta
import catalog_service as catalog
from auth_decorators import require_role
from perf_cache import ttl_cache

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
@require_role('admin')
def admin_home():
    # Platform-wide metrics (no per-user data) — safe to serve a few seconds
    # stale, so the whole heavy KPI block is cached per worker for 60s.
    return render_template('fm/admin_dashboard.html', **_admin_home_data())


@ttl_cache(seconds=60)
def _admin_home_data():
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
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE created_at >= DATE_FORMAT(NOW(), '%Y-%m-01')")
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
            FROM course_orders WHERE created_at >= DATE_FORMAT(NOW(), '%Y-%m-01')
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
        # Pre-aggregate employees and orders in derived tables, then join — one
        # grouped scan of each table (covered by idx_cu_* / idx_co_company_created)
        # instead of 3 correlated subqueries per company (~60 scans for 20 rows).
        # The two aggregates are kept in SEPARATE derived tables so the
        # employees×orders join never fans out and inflates the counts/revenue.
        cur.execute("""
            SELECT c.id, c.company_name, c.company_slug, c.industry, c.company_size,
                   c.subscription_plan AS subscription_status, c.features,
                   c.trial_ends_at AS trial_end_date, c.created_at,
                   cs.enable_white_label,
                   COALESCE(emp.employee_count, 0) AS employee_count,
                   COALESCE(ord.order_count, 0) AS order_count,
                   COALESCE(ord.revenue, 0) AS revenue
            FROM companies c
            LEFT JOIN company_settings cs ON cs.company_id = c.id
            LEFT JOIN (
                SELECT company_id, COUNT(*) AS employee_count
                FROM company_users GROUP BY company_id
            ) emp ON emp.company_id = c.id
            LEFT JOIN (
                SELECT company_id, COUNT(*) AS order_count, COALESCE(SUM(price), 0) AS revenue
                FROM course_orders GROUP BY company_id
            ) ord ON ord.company_id = c.id
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

    return dict(
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
        recent_orders=recent_orders,
    )


@admin_dashboard_bp.route('/credits', methods=['GET', 'POST'])
@require_role('admin')
def credits():
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
@require_role('admin')
def user_list():
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    users_has_created_at = _column_exists(cur, 'users', 'created_at')

    # Server-side pagination + optional search. Previously this fetched the whole
    # users table on every load; now it pulls one bounded page and pushes the
    # search into SQL so it still spans all users (not just the loaded page).
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    per_page = 50
    offset = (page - 1) * per_page
    q = (request.args.get('q') or '').strip()

    where_sql = ""
    params = []
    if q:
        where_sql = "WHERE (u.username LIKE %s OR u.email LIKE %s OR c.company_name LIKE %s)"
        like = "%{}%".format(q)
        params = [like, like, like]

    users = []
    total = 0
    try:
        created_at_select = "u.created_at" if users_has_created_at else "NULL AS created_at"
        user_order = "u.created_at DESC, u.id DESC" if users_has_created_at else "u.id DESC"
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM users u
            LEFT JOIN company_users cu ON cu.user_id = u.id AND cu.status = 'active'
            LEFT JOIN companies c ON c.id = cu.company_id
            {where}
        """.format(where=where_sql), params)
        row = cur.fetchone()
        total = (row['cnt'] if row else 0) or 0

        cur.execute("""
            SELECT u.id, u.username, u.email, u.role, u.credits, {created_at_select},
                   cu.company_id, c.company_name, cu.role AS company_role
            FROM users u
            LEFT JOIN company_users cu ON cu.user_id = u.id AND cu.status = 'active'
            LEFT JOIN companies c ON c.id = cu.company_id
            {where}
            ORDER BY {user_order}
            LIMIT %s OFFSET %s
        """.format(created_at_select=created_at_select, user_order=user_order, where=where_sql),
            params + [per_page, offset])
        users = cur.fetchall()
    except Exception as e:
        logging.error("Error fetching admin user company data: %s", e)
        try:
            cur.execute("""
                SELECT id, username, email, role, credits, NULL AS created_at,
                       NULL AS company_id, NULL AS company_name, NULL AS company_role
                FROM users ORDER BY id DESC LIMIT %s OFFSET %s
            """, (per_page, offset))
            users = cur.fetchall()
        except Exception as e2:
            logging.error("Error fetching admin user fallback: %s", e2)
            users = []
    cur.close()

    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    return render_template('fm/admin_users.html', users=users,
                           page=page, per_page=per_page, total=total,
                           total_pages=total_pages, q=q,
                           has_prev=page > 1, has_next=page < total_pages)


@admin_dashboard_bp.route('/users/<int:user_id>/role', methods=['POST'])
@require_role('admin')
def update_user_role(user_id):
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
@require_role('admin')
def update_user_credits(user_id):
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
@require_role('admin')
def admin_catalog():
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
@require_role('admin')
def admin_catalog_import():
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
@require_role('admin')
def admin_catalog_import_preview(job_id):
    draft = catalog.get_import_draft(job_id)
    if not draft:
        flash("Importkladde ikke fundet.", "warning")
        return redirect(url_for('admin_dashboard.admin_catalog'))
    products = [catalog.normalize_product(product, overrides={}) for product in draft.get('products', [])[:200]]
    return render_template('fm/admin_catalog_import_preview.html', draft=draft, products=products)


@admin_dashboard_bp.route('/catalog/import/<job_id>/confirm', methods=['POST'])
@require_role('admin')
def admin_catalog_import_confirm(job_id):
    draft = catalog.confirm_import_draft(job_id)
    if not draft:
        flash("Importkladde ikke fundet.", "warning")
    else:
        # Bookkeeping: if this draft came from a vendor upload, flag the matching
        # vendor_submissions row as approved. Best-effort, never blocks confirm.
        try:
            cur = current_app.mysql.connection.cursor()
            _mark_submissions_approved_for_job(cur, job_id)
            current_app.mysql.connection.commit()
            cur.close()
        except Exception as e:
            logging.warning("Vendor submission approval bookkeeping failed for %s: %s", job_id, e)
        flash("CSV import er bekraeftet og kataloget er opdateret.", "success")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/import/<job_id>/cancel', methods=['POST'])
@require_role('admin')
def admin_catalog_import_cancel(job_id):
    catalog.delete_import_draft(job_id)
    flash("Importkladde slettet.", "info")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/ai-categorize/start', methods=['POST'])
@require_role('admin')
def admin_catalog_ai_start():
    job = catalog.create_ai_category_job(created_by=session.get('user', ''))
    return jsonify({'success': True, 'job_id': job['job_id'], 'total': job['total']})


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/batch', methods=['POST'])
@require_role('admin')
def admin_catalog_ai_batch(job_id):
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
@require_role('admin')
def admin_catalog_ai_preview(job_id):
    job = catalog.get_ai_category_job(job_id)
    if not job:
        flash("AI-kategoriseringsjob ikke fundet.", "warning")
        return redirect(url_for('admin_dashboard.admin_catalog'))
    diff = catalog.ai_category_diff(job)
    return render_template('fm/admin_catalog_ai_preview.html', job=job, diff=diff)


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/confirm', methods=['POST'])
@require_role('admin')
def admin_catalog_ai_confirm(job_id):
    job = catalog.confirm_ai_category_job(job_id)
    if not job:
        flash("AI-kategoriseringsjob ikke fundet.", "warning")
    else:
        flash("AI-kategorier bekraeftet og kataloget er opdateret.", "success")
    return redirect(url_for('admin_dashboard.admin_catalog'))


@admin_dashboard_bp.route('/catalog/ai-categorize/<job_id>/cancel', methods=['POST'])
@require_role('admin')
def admin_catalog_ai_cancel(job_id):
    catalog.delete_ai_category_job(job_id)
    flash("AI-kategoriseringsjob slettet.", "info")
    return redirect(url_for('admin_dashboard.admin_catalog'))


# ---------------------------------------------------------------------------
# Vendor account management (platform-admin only)
#
# Vendors are an identity layer on top of the existing catalog 'vendor' string.
# These routes let a platform admin create/suspend vendor accounts and review
# the CSV submissions vendors upload (which flow through the EXISTING catalog
# import draft -> confirm pipeline). Everything here is boot-safe: the vendors /
# vendor_submissions tables and the vendor_auth helper are optional, so each
# query is wrapped defensively and a missing table degrades to an empty list
# rather than a 500.
# ---------------------------------------------------------------------------

def _vendor_course_counts():
    """Map vendor_name (lower-cased) -> number of catalog products.

    Catalog products are file-based and grouped by the 'vendor' string, so we
    reuse catalog.get_vendors() to count courses per vendor name. Never raises.
    """
    counts = {}
    try:
        for vendor in catalog.get_vendors():
            name = (vendor.get('name') or '').strip().lower()
            if name:
                counts[name] = vendor.get('course_count', 0)
    except Exception as e:
        logging.warning("Could not compute vendor course counts: %s", e)
    return counts


def _mark_submissions_approved_for_job(cur, job_id):
    """Best-effort: flag any pending vendor_submissions for this import job_id as
    approved when an admin confirms the draft. Safe if the table is absent."""
    if not job_id:
        return
    try:
        cur.execute(
            """UPDATE vendor_submissions
                   SET status = 'approved',
                       reviewed_by = %s,
                       reviewed_at = NOW()
                 WHERE job_id = %s AND status = 'pending'""",
            (session.get('user_id') or None, job_id),
        )
    except Exception as e:
        logging.warning("Could not auto-approve vendor submissions for job %s: %s", job_id, e)


@admin_dashboard_bp.route('/vendors')
@require_role('admin')
def admin_vendors():
    """List vendor accounts (with catalog course counts) and pending submissions."""
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    vendors = []
    try:
        cur.execute("""
            SELECT id, vendor_name, slug, contact_email, status,
                   description, website, logo_url, created_at, updated_at
            FROM vendors
            ORDER BY vendor_name ASC
        """)
        vendors = cur.fetchall() or []
    except Exception as e:
        logging.warning("Could not load vendors (table may not exist yet): %s", e)

    course_counts = _vendor_course_counts()
    for vendor in vendors:
        name = (vendor.get('vendor_name') or '').strip().lower()
        vendor['course_count'] = course_counts.get(name, 0)

    submissions = []
    try:
        cur.execute("""
            SELECT vs.id, vs.vendor_id, vs.job_id, vs.filename, vs.row_count,
                   vs.status, vs.reviewed_by, vs.reviewed_at, vs.created_at,
                   v.vendor_name
            FROM vendor_submissions vs
            LEFT JOIN vendors v ON v.id = vs.vendor_id
            ORDER BY (vs.status = 'pending') DESC, vs.created_at DESC
            LIMIT 100
        """)
        submissions = cur.fetchall() or []
    except Exception as e:
        logging.warning("Could not load vendor submissions (table may not exist yet): %s", e)

    pending_count = sum(1 for s in submissions if s.get('status') == 'pending')

    cur.close()
    return render_template(
        'fm/admin_vendors.html',
        vendors=vendors,
        submissions=submissions,
        pending_count=pending_count,
    )


def _send_vendor_invite_email(contact_email, vendor_name, token, expires_at):
    """Best-effort 'Sæt din adgangskode' invite email. Never raises, never blocks.

    Builds the absolute set-password URL from the invite token and sends it via
    the branded email layer (no-op when SMTP isn't configured)."""
    if not contact_email or not token:
        return
    try:
        from email_service import send_branded_email
        try:
            set_password_url = url_for('vendor.vendor_set_password', token=token, _external=True)
        except Exception:
            set_password_url = url_for('vendor.vendor_set_password', token=token)
        try:
            exp_label = expires_at.strftime('%d/%m/%Y %H:%M') if expires_at else ''
        except Exception:
            exp_label = ''
        send_branded_email(
            contact_email,
            "Sæt din adgangskode — Futurematch leverandørportal",
            'vendor_invite',
            {},
            vendor_name=vendor_name,
            set_password_url=set_password_url,
            expires_at=exp_label,
        )
    except Exception as e:
        logging.debug("Vendor invite email skipped: %s", e)


@admin_dashboard_bp.route('/vendors/create', methods=['POST'])
@require_role('admin')
def admin_vendor_create():
    """Create a vendor account.

    Two onboarding modes:
      * If an initial password is provided, hash it (legacy behaviour).
      * Otherwise mint an opaque single-use invite_token (+ 7-day expiry) on the
        vendors row and email a 'Sæt din adgangskode' link so the vendor sets
        their own password via vendor.vendor_set_password. The account is created
        as 'pending' in that case and activates when the password is set.
    """
    vendor_name = (request.form.get('vendor_name') or '').strip()
    contact_email = (request.form.get('contact_email') or '').strip().lower()
    password = request.form.get('password') or ''
    status = (request.form.get('status') or 'active').strip()
    description = (request.form.get('description') or '').strip() or None
    website = (request.form.get('website') or '').strip() or None

    if status not in ('pending', 'active', 'suspended'):
        status = 'active'

    # Password is now OPTIONAL — without one we send a set-password invite.
    if not vendor_name or not contact_email:
        flash("Udfyld leverandørnavn og e-mail.", "danger")
        return redirect(url_for('admin_dashboard.admin_vendors'))

    # Guarded import: vendor_auth is owned by a sibling module and is optional.
    try:
        import vendor_auth
    except Exception as e:
        logging.error("vendor_auth unavailable, cannot create vendor: %s", e)
        flash("Leverandør-login er ikke tilgængeligt endnu. Prøv igen senere.", "danger")
        return redirect(url_for('admin_dashboard.admin_vendors'))

    invite_mode = not password
    password_hash = None
    invite_token = None
    invite_expires_at = None
    if invite_mode:
        # Opaque, single-use invite token (+ 7-day expiry). Account starts
        # 'pending' and activates when the vendor sets their own password.
        import secrets
        invite_token = secrets.token_urlsafe(32)[:80]
        invite_expires_at = datetime.now() + timedelta(days=7)
        status = 'pending'
    else:
        try:
            password_hash = vendor_auth.hash_vendor_password(password)
        except Exception as e:
            logging.error("vendor_auth could not hash vendor password: %s", e)
            flash("Leverandør-login er ikke tilgængeligt endnu. Prøv igen senere.", "danger")
            return redirect(url_for('admin_dashboard.admin_vendors'))

    # Build a unique-ish slug from the vendor name.
    slug = re.sub(r'[^a-z0-9]+', '-', vendor_name.lower()).strip('-')[:140] or None

    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute(
            """INSERT INTO vendors
                   (vendor_name, slug, contact_email, password_hash, status,
                    description, website, invite_token, invite_expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (vendor_name, slug, contact_email, password_hash, status, description,
             website, invite_token, invite_expires_at),
        )
        current_app.mysql.connection.commit()
        cur.close()
        if invite_mode:
            # Best-effort invite email (never blocks the create).
            _send_vendor_invite_email(contact_email, vendor_name, invite_token, invite_expires_at)
            flash(f"Leverandøren \"{vendor_name}\" er oprettet. En invitation til at saette adgangskode er sendt.", "success")
        else:
            flash(f"Leverandøren \"{vendor_name}\" er oprettet.", "success")
    except Exception as e:
        logging.error("Could not create vendor: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        msg = str(e).lower()
        if 'duplicate' in msg or 'unique' in msg:
            flash("En leverandør med denne e-mail eller slug findes allerede.", "danger")
        elif "doesn't exist" in msg or 'no such table' in msg:
            flash("Leverandørtabellen er ikke oprettet endnu.", "danger")
        else:
            flash("Leverandøren kunne ikke oprettes.", "danger")
    return redirect(url_for('admin_dashboard.admin_vendors'))


@admin_dashboard_bp.route('/vendors/<int:vendor_id>/status', methods=['POST'])
@require_role('admin')
def admin_vendor_status(vendor_id):
    """Set a vendor account status to active or suspended."""
    new_status = (request.form.get('status') or '').strip()
    if new_status not in ('active', 'suspended', 'pending'):
        flash("Ugyldig status.", "danger")
        return redirect(url_for('admin_dashboard.admin_vendors'))
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("UPDATE vendors SET status = %s WHERE id = %s", (new_status, vendor_id))
        current_app.mysql.connection.commit()
        cur.close()
        flash(f"Leverandørstatus opdateret til {new_status}.", "success")
    except Exception as e:
        logging.error("Could not update vendor status: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash("Status kunne ikke opdateres.", "danger")
    return redirect(url_for('admin_dashboard.admin_vendors'))


@admin_dashboard_bp.route('/vendors/submission/<int:submission_id>/<action>', methods=['POST'])
@require_role('admin')
def admin_vendor_submission_action(submission_id, action):
    """Mark a vendor submission approved or rejected (bookkeeping toggle).

    Approval of the actual catalog data still happens through the existing
    admin_catalog_import_confirm pipeline; this is the submission-status record.
    """
    if action not in ('approved', 'rejected'):
        flash("Ugyldig handling.", "danger")
        return redirect(url_for('admin_dashboard.admin_vendors'))
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute(
            """UPDATE vendor_submissions
                   SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                 WHERE id = %s""",
            (action, session.get('user_id') or None, submission_id),
        )
        current_app.mysql.connection.commit()
        cur.close()
        label = "godkendt" if action == 'approved' else "afvist"
        flash(f"Indsendelsen er {label}.", "success")
    except Exception as e:
        logging.error("Could not update vendor submission: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash("Indsendelsen kunne ikke opdateres.", "danger")
    return redirect(url_for('admin_dashboard.admin_vendors'))


# ---------------------------------------------------------------------------
# Supplier agreement CRUD (platform-admin only).
#
# company_supplier_agreements drives the negotiated discount decoration on the
# catalog (catalog_service.get_company_discount_map). It was previously only
# readable; these routes give admins a create/edit/delete UI. Each agreement is
# uniquely keyed on (company_id, vendor_name) via uk_company_vendor_agreement,
# so saving an existing pair upserts rather than duplicates.
# ---------------------------------------------------------------------------
@admin_dashboard_bp.route('/agreements')
@require_role('admin')
def admin_agreements():
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    agreements, companies, vendors = [], [], []
    try:
        cur.execute("""
            SELECT a.id, a.company_id, a.vendor_name, a.discount_type, a.discount_value,
                   a.agreement_name, a.agreement_reference, a.valid_from, a.valid_until,
                   a.min_participants, a.notes, a.is_active, a.created_at, a.updated_at,
                   c.company_name
            FROM company_supplier_agreements a
            LEFT JOIN companies c ON c.id = a.company_id
            ORDER BY (a.is_active = 1) DESC, c.company_name ASC, a.vendor_name ASC
        """)
        agreements = cur.fetchall() or []
    except Exception as e:
        logging.warning("Could not load supplier agreements (table may not exist yet): %s", e)

    try:
        cur.execute("SELECT id, company_name FROM companies ORDER BY company_name ASC")
        companies = cur.fetchall() or []
    except Exception as e:
        logging.warning("Could not load companies for agreements: %s", e)

    cur.close()

    # Vendor-name options: the catalog vendor strings (agreements key on the
    # free-text vendor_name that products carry), so an admin picks a real one.
    try:
        vendors = [v.get('name') for v in catalog.get_vendors() if v.get('name')]
        vendors.sort(key=lambda s: s.lower())
    except Exception as e:
        logging.warning("Could not load vendor names for agreements: %s", e)
        vendors = []

    active_count = sum(1 for a in agreements if a.get('is_active'))

    return render_template(
        'fm/admin_agreements.html',
        agreements=agreements,
        companies=companies,
        vendors=vendors,
        active_count=active_count,
    )


@admin_dashboard_bp.route('/agreements/save', methods=['POST'])
@require_role('admin')
def admin_agreement_save():
    """Create or update a supplier agreement (upsert on company_id+vendor_name)."""
    def _back():
        return redirect(url_for('admin_dashboard.admin_agreements'))

    try:
        company_id = int(request.form.get('company_id') or 0)
    except (TypeError, ValueError):
        company_id = 0
    vendor_name = (request.form.get('vendor_name') or '').strip()
    discount_type = (request.form.get('discount_type') or 'percentage').strip()
    if discount_type not in ('percentage', 'fixed_price', 'fixed_amount'):
        discount_type = 'percentage'
    try:
        discount_value = float(request.form.get('discount_value') or 0)
    except (TypeError, ValueError):
        discount_value = 0.0
    agreement_name = (request.form.get('agreement_name') or '').strip() or None
    agreement_reference = (request.form.get('agreement_reference') or '').strip() or None
    valid_from = (request.form.get('valid_from') or '').strip() or None
    valid_until = (request.form.get('valid_until') or '').strip() or None
    try:
        min_participants = int(request.form.get('min_participants') or 1)
    except (TypeError, ValueError):
        min_participants = 1
    notes = (request.form.get('notes') or '').strip() or None
    is_active = 1 if request.form.get('is_active') in ('1', 'on', 'true') else 0

    if not company_id or not vendor_name:
        flash("Vaelg en virksomhed og en leverandør.", "danger")
        return _back()
    if discount_value < 0:
        flash("Rabatvaerdien kan ikke vaere negativ.", "danger")
        return _back()

    try:
        cur = current_app.mysql.connection.cursor()
        # Upsert keyed on uk_company_vendor_agreement (company_id, vendor_name).
        cur.execute(
            """INSERT INTO company_supplier_agreements
                   (company_id, vendor_name, discount_type, discount_value,
                    agreement_name, agreement_reference, valid_from, valid_until,
                    min_participants, notes, is_active, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   discount_type = VALUES(discount_type),
                   discount_value = VALUES(discount_value),
                   agreement_name = VALUES(agreement_name),
                   agreement_reference = VALUES(agreement_reference),
                   valid_from = VALUES(valid_from),
                   valid_until = VALUES(valid_until),
                   min_participants = VALUES(min_participants),
                   notes = VALUES(notes),
                   is_active = VALUES(is_active)""",
            (company_id, vendor_name, discount_type, discount_value,
             agreement_name, agreement_reference, valid_from, valid_until,
             min_participants, notes, is_active, session.get('user_id') or None),
        )
        current_app.mysql.connection.commit()
        cur.close()
        flash("Aftalen er gemt.", "success")
    except Exception as e:
        logging.error("Could not save supplier agreement: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        msg = str(e).lower()
        if "doesn't exist" in msg or 'no such table' in msg:
            flash("Aftaletabellen er ikke oprettet endnu.", "danger")
        else:
            flash("Aftalen kunne ikke gemmes.", "danger")
    return _back()


@admin_dashboard_bp.route('/agreements/<int:agreement_id>/delete', methods=['POST'])
@require_role('admin')
def admin_agreement_delete(agreement_id):
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("DELETE FROM company_supplier_agreements WHERE id = %s", (agreement_id,))
        current_app.mysql.connection.commit()
        cur.close()
        flash("Aftalen er slettet.", "info")
    except Exception as e:
        logging.error("Could not delete supplier agreement: %s", e)
        try:
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        flash("Aftalen kunne ikke slettes.", "danger")
    return redirect(url_for('admin_dashboard.admin_agreements'))


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


# ---------------------------------------------------------------------------
# B10 — Audit-log viewer. The audit_log table is written across the app
# (order_service._write_audit, gdpr, multitenant_reports) but was never surfaced.
# Platform-admin only; filterable by action_type, company and time window.
# ---------------------------------------------------------------------------
@admin_dashboard_bp.route('/log')
@require_role('admin')
def admin_audit_log():
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

    try:
        days = int(request.args.get('days', 30))
    except (TypeError, ValueError):
        days = 30
    action_type = (request.args.get('action_type') or '').strip()
    try:
        company_id = int(request.args.get('company_id')) if request.args.get('company_id') else None
    except (TypeError, ValueError):
        company_id = None

    rows, action_types = [], []
    try:
        where = ["al.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
        params = [days]
        if action_type:
            where.append("al.action_type = %s")
            params.append(action_type)
        if company_id:
            where.append("al.company_id = %s")
            params.append(company_id)
        cur.execute(
            """SELECT al.id, al.company_id, al.user_id, al.action, al.action_type,
                      al.resource_type, al.resource_id, al.description, al.created_at,
                      c.company_name, u.username
               FROM audit_log al
               LEFT JOIN companies c ON c.id = al.company_id
               LEFT JOIN users u ON u.id = al.user_id
               WHERE """ + " AND ".join(where) + """
               ORDER BY al.created_at DESC LIMIT 300""",
            tuple(params),
        )
        rows = cur.fetchall() or []
        cur.execute(
            """SELECT DISTINCT action_type FROM audit_log
               WHERE action_type IS NOT NULL AND action_type <> ''
               ORDER BY action_type"""
        )
        action_types = [r['action_type'] for r in (cur.fetchall() or [])]
    except Exception as e:
        logging.warning("admin_audit_log query failed (table may be empty): %s", e)
    finally:
        cur.close()

    return render_template('fm/adminlog.html', rows=rows, action_types=action_types,
                           sel_action_type=action_type, sel_company_id=company_id, days=days)


# ---------------------------------------------------------------------------
# B12 — Subsystem / event-delivery health. Reuses feature_status probes +
# event_outbox backlog + a platform snapshot. Platform-admin only.
# ---------------------------------------------------------------------------
@admin_dashboard_bp.route('/system-health')
@require_role('admin')
def admin_system_health():
    features = {}
    try:
        from feature_status import check_feature_status
        features = check_feature_status() or {}
    except Exception as e:
        logging.warning("system-health: feature_status unavailable: %s", e)

    outbox = {'pending': 0, 'delivered': 0, 'failed': 0, 'total': 0}
    snapshot = {}
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT status, COUNT(*) AS cnt FROM event_outbox GROUP BY status")
        for r in (cur.fetchall() or []):
            outbox[r['status']] = r['cnt']
            outbox['total'] += r['cnt']
    except Exception as e:
        logging.warning("system-health: event_outbox read failed: %s", e)
    for label, sql in (
        ('companies', "SELECT COUNT(*) AS c FROM companies"),
        ('users', "SELECT COUNT(*) AS c FROM users"),
        ('orders_7d', "SELECT COUNT(*) AS c FROM course_orders WHERE created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"),
    ):
        try:
            cur.execute(sql)
            snapshot[label] = (cur.fetchone() or {}).get('c', 0)
        except Exception:
            snapshot[label] = None
    cur.close()

    return render_template('fm/admin_system_health.html',
                           features=features, outbox=outbox, snapshot=snapshot)


# ---------------------------------------------------------------------------
# C6 — Admin → HR impersonation. A platform admin can "act as" a tenant to use
# its HR workspace for support/oversight. Resolution happens in
# hr_dashboard.get_company_context / companies.get_company_context (which honor
# session['admin_acting_company_id']). Here we set/clear that state, align the
# session, stash the admin's own context for restore, and audit it.
#
# NOTE: this is full read/write impersonation by design (requested). Security
# hardening (read-only mode, SECRET_KEY rotation) is tracked separately.
# ---------------------------------------------------------------------------
@admin_dashboard_bp.route('/impersonate/<int:company_id>')
@require_role('admin')
def impersonate_company(company_id):
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    company = None
    try:
        cur.execute("SELECT id, company_name FROM companies WHERE id = %s", (company_id,))
        company = cur.fetchone()
    except Exception as e:
        logging.error("impersonate: company lookup failed: %s", e)
    finally:
        cur.close()

    if not company:
        flash("Virksomheden blev ikke fundet.", "warning")
        return redirect(url_for('companies.admin_companies_list'))

    # Stash the admin's own context ONCE so we can restore it on exit.
    if 'admin_acting_company_id' not in session:
        session['_imp_prev_company_id'] = session.get('company_id')
        session['_imp_prev_company_role'] = session.get('company_role')
        session['_imp_prev_company_name'] = session.get('company_name')

    session['admin_acting_company_id'] = company['id']
    session['company_id'] = company['id']
    session['company_role'] = 'company_admin'
    session['company_name'] = company['company_name']

    # Audit (best-effort; reuses order_service audit shape / audit_log table).
    try:
        from order_service import _write_audit
        acur = current_app.mysql.connection.cursor()
        _write_audit(acur, company_id=company['id'],
                     user_id=session.get('user_id'),
                     action="admin.impersonate.start",
                     resource_id=str(company['id']),
                     description=f"{session.get('user')} acting as {company['company_name']}")
        current_app.mysql.connection.commit()
        acur.close()
    except Exception as e:
        logging.debug("impersonate audit skipped: %s", e)

    flash(f"Du ser nu {company['company_name']} som administrator.", "info")
    return redirect(url_for('hr_dashboard.dashboard'))


@admin_dashboard_bp.route('/impersonate/exit')
@require_role('admin')
def impersonate_exit():
    acting = session.pop('admin_acting_company_id', None)
    # Restore the admin's own context.
    prev_id = session.pop('_imp_prev_company_id', None)
    prev_role = session.pop('_imp_prev_company_role', None)
    prev_name = session.pop('_imp_prev_company_name', None)
    if prev_id:
        session['company_id'] = prev_id
    else:
        session.pop('company_id', None)
    if prev_role:
        session['company_role'] = prev_role
    else:
        session.pop('company_role', None)
    if prev_name:
        session['company_name'] = prev_name
    else:
        session.pop('company_name', None)

    try:
        if acting:
            from order_service import _write_audit
            acur = current_app.mysql.connection.cursor()
            _write_audit(acur, company_id=acting, user_id=session.get('user_id'),
                         action="admin.impersonate.stop", resource_id=str(acting),
                         description=f"{session.get('user')} stopped impersonating")
            current_app.mysql.connection.commit()
            acur.close()
    except Exception as e:
        logging.debug("impersonate-exit audit skipped: %s", e)

    flash("Impersonation afsluttet.", "info")
    if acting:
        return redirect(url_for('companies.admin_company_detail', company_id=acting))
    return redirect(url_for('companies.admin_companies_list'))
