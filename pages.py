# pages.py
from flask import Blueprint, render_template, session, current_app, redirect, url_for, flash, request
import MySQLdb.cursors
import os
import json
import datetime
from auth_decorators import login_required

pages_bp = Blueprint('pages', __name__, template_folder='templates')


def _fetch_company_notifications(limit=60):
    """Recipient-scoped company_notifications for the session user.

    Scoping mirrors hr_dashboard.notifications: same company, addressed to this
    user directly OR broadcast (recipient_user_id IS NULL), and either untargeted
    or targeted at this user's company_role. Read-only; returns [] on any failure
    so the page never breaks for users without notifications.
    """
    company_id = session.get('company_id')
    user_id = session.get('user_id')
    company_role = session.get('company_role')
    if not company_id:
        return []
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT cn.id, cn.title, cn.message, cn.is_urgent, cn.is_read,
                   cn.created_at, u.username AS sender_name
            FROM company_notifications cn
            LEFT JOIN users u ON cn.sender_user_id = u.id
            WHERE cn.company_id = %s
              AND (cn.recipient_user_id = %s OR cn.recipient_user_id IS NULL)
              AND (cn.target_roles IS NULL OR JSON_CONTAINS(cn.target_roles, %s))
            ORDER BY cn.is_read ASC, cn.is_urgent DESC, cn.created_at DESC
            LIMIT %s
            """,
            (company_id, user_id, json.dumps(company_role), int(limit)),
        )
        rows = cur.fetchall() or []
        cur.close()
        return list(rows)
    except Exception as e:
        current_app.logger.warning("notifications fetch: %s", e)
        return []


def _group_notifications_by_day(rows):
    """Group rows into Danish day buckets: I dag / I går / Tidligere."""
    today = datetime.date.today()
    yesterday = today - datetime.timedelta(days=1)
    buckets = {'today': [], 'yesterday': [], 'earlier': []}
    for r in rows:
        created = r.get('created_at')
        d = created.date() if isinstance(created, datetime.datetime) else None
        if d == today:
            buckets['today'].append(r)
        elif d == yesterday:
            buckets['yesterday'].append(r)
        else:
            buckets['earlier'].append(r)
    groups = []
    if buckets['today']:
        groups.append({'label': 'I dag', 'items': buckets['today']})
    if buckets['yesterday']:
        groups.append({'label': 'I går', 'items': buckets['yesterday']})
    if buckets['earlier']:
        groups.append({'label': 'Tidligere', 'items': buckets['earlier']})
    return groups

@pages_bp.route('/about')
def about():
    return render_template('fm/about.html')

@pages_bp.route('/contact')
def contact():
    # No dedicated contact page in the Futurematch design; support covers it.
    return render_template('fm/support.html')

@pages_bp.route('/support')
def support():
    return render_template('fm/support.html')

@pages_bp.route('/privacy')
def privacy():
    return render_template('fm/privacy.html')

@pages_bp.route('/terms')
def terms():
    return render_template('fm/terms.html')

@pages_bp.route('/notifications')
@login_required
def notifications():
    rows = _fetch_company_notifications()
    groups = _group_notifications_by_day(rows)
    unread_count = sum(1 for r in rows if not r.get('is_read'))
    return render_template('fm/notifications.html',
                           notification_groups=groups,
                           notifications_total=len(rows),
                           notifications_unread=unread_count)

@pages_bp.route('/analytics')
@login_required
def analytics():
    username = session.get('user')
    
    import datetime
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            _win = max(1, int(os.environ.get('USAGE_WINDOW_DAYS', '365')))
        except (TypeError, ValueError):
            _win = 365
        cur.execute(
            "SELECT timestamp, credits_used, description FROM credit_usage "
            "WHERE username = %s AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) "
            "ORDER BY timestamp ASC",
            (username, _win),
        )
        records = cur.fetchall()
        cur.close()
    except Exception as e:
        current_app.logger.error("Error fetching credit usage data: %s", e)
        records = []
    
    from collections import defaultdict
    daily_usage = defaultdict(int)
    for record in records:
        dt = record['timestamp']
        if isinstance(dt, datetime.datetime):
            date_str = dt.strftime('%Y-%m-%d')
        else:
            date_str = str(dt)[:10]
        daily_usage[date_str] += record['credits_used']
    
    sorted_dates = sorted(daily_usage.keys())
    usage_values = [daily_usage[date] for date in sorted_dates]
    total_credits_used = sum(usage_values)
    
    average_usage = total_credits_used / len(sorted_dates) if sorted_dates else 0
    peak_usage = max(usage_values) if usage_values else 0
    peak_day = sorted_dates[usage_values.index(peak_usage)] if usage_values else ""
    
    # For bar chart: last 7 days usage
    last7_dates = sorted_dates[-7:] if len(sorted_dates) >= 7 else sorted_dates
    last7_usage = usage_values[-7:] if len(usage_values) >= 7 else usage_values
    
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT credits FROM users WHERE username = %s", (username,))
        user_row = cur.fetchone()
        cur.close()
        current_credits = user_row['credits'] if user_row and 'credits' in user_row else 0
    except Exception as e:
        current_app.logger.error("Error fetching user credits: %s", e)
        current_credits = 0
    
    return render_template('fm/analytics.html',
                           labels=sorted_dates,
                           usage=usage_values,
                           total_used=total_credits_used,
                           current_credits=current_credits,
                           transactions=records,
                           average_usage=average_usage,
                           peak_usage=peak_usage,
                           peak_day=peak_day,
                           last7_labels=last7_dates,
                           last7_usage=last7_usage)










@pages_bp.route('/indstillinger', methods=['GET', 'POST'])
@login_required
def settings():
    username = session.get('user')
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT username, email, email_notifications, password FROM users WHERE username = %s", (username,))
        user_data = cur.fetchone()
        cur.close()
        if not user_data:
            flash("Fejl ved hentning af indstillinger.", "danger")
            user_data = {}
    except Exception as e:
        current_app.logger.error("Error fetching user settings: %s", e)
        flash("Fejl ved hentning af indstillinger.", "danger")
        user_data = {}
    
    if request.method == "POST":
        # Process Profile Update
        if "update_profile" in request.form:
            new_email = request.form.get('email')
            email_notifications = 1 if request.form.get('email_notifications') == 'on' else 0
            
            update_fields = []
            update_values = []
            
            if new_email and new_email != user_data.get('email'):
                update_fields.append("email = %s")
                update_values.append(new_email)
            
            update_fields.append("email_notifications = %s")
            update_values.append(email_notifications)
            
            if update_fields:
                update_values.append(username)
                query = "UPDATE users SET " + ", ".join(update_fields) + " WHERE username = %s"
                try:
                    cur = current_app.mysql.connection.cursor()
                    cur.execute(query, tuple(update_values))
                    current_app.mysql.connection.commit()
                    cur.close()
                    flash("Profiloplysninger opdateret!", "success")
                except Exception as e:
                    current_app.logger.error("Error updating profile: %s", e)
                    flash("Fejl ved opdatering af profiloplysninger.", "danger")
                    return redirect(url_for('pages.settings'))
            else:
                flash("Ingen ændringer at opdatere.", "info")
        
        # Process Password Update
        elif "update_password" in request.form:
            current_password = request.form.get('current_password')
            new_password = request.form.get('new_password')
            confirm_password = request.form.get('confirm_password')
            
            if not current_password:
                flash("Angiv venligst dit nuvaerende kodeord.", "danger")
                return redirect(url_for('pages.settings'))
            if new_password != confirm_password:
                flash("Det nye kodeord og bekraeftelse stemmer ikke overens.", "danger")
                return redirect(url_for('pages.settings'))
            from werkzeug.security import check_password_hash, generate_password_hash
            stored_pw = user_data.get('password', '')
            if stored_pw.startswith(('pbkdf2:', 'scrypt:')):
                pw_ok = check_password_hash(stored_pw, current_password)
            else:
                pw_ok = (stored_pw == current_password)
            if not pw_ok:
                flash("Nuvaerende kodeord er forkert.", "danger")
                return redirect(url_for('pages.settings'))
            hashed_new = generate_password_hash(new_password)
            try:
                cur = current_app.mysql.connection.cursor()
                cur.execute("UPDATE users SET password = %s WHERE username = %s", (hashed_new, username))
                current_app.mysql.connection.commit()
                cur.close()
                flash("Kodeord opdateret!", "success")
            except Exception as e:
                current_app.logger.error("Error updating password: %s", e)
                flash("Fejl ved opdatering af kodeord.", "danger")
                return redirect(url_for('pages.settings'))
        
        return redirect(url_for('pages.settings'))
    
    return render_template('fm/settings.html', user=user_data)



@pages_bp.route('/profile')
def profile():
    return render_template('fm/my_profile.html', username=session.get('user'))
