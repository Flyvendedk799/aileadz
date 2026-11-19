from flask import Blueprint, render_template, session, current_app
import MySQLdb.cursors
from collections import defaultdict
import datetime
import os
from auth_decorators import login_required

reports_bp = Blueprint('reports', __name__, template_folder='templates')


def _usage_window_days(default=365):
    """Bound the per-user credit_usage scan so a heavy-usage account can't pull an
    unbounded history into Python. Generous default (1y); tunable via USAGE_WINDOW_DAYS."""
    try:
        return max(1, int(os.environ.get('USAGE_WINDOW_DAYS', str(default))))
    except (TypeError, ValueError):
        return default

@reports_bp.route('')
@reports_bp.route('/')
@login_required
def reports():
    username = session.get('user')
    
    # Fetch credit usage records from credit_usage table
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT timestamp, credits_used, description FROM credit_usage "
            "WHERE username = %s AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) "
            "ORDER BY timestamp ASC",
            (username, _usage_window_days()),
        )
        credit_records = cur.fetchall()
        cur.close()
    except Exception as e:
        current_app.logger.error("Error fetching credit usage data: %s", e)
        credit_records = []
    
    # Aggregate credit usage by date for chart data
    daily_usage = defaultdict(int)
    for record in credit_records:
        dt = record['timestamp']
        if isinstance(dt, datetime.datetime):
            date_str = dt.strftime('%Y-%m-%d')
        else:
            date_str = str(dt)[:10]
        daily_usage[date_str] += record['credits_used']
    sorted_dates = sorted(daily_usage.keys())
    daily_usage_values = [daily_usage[date] for date in sorted_dates]
    total_credits_used = sum(daily_usage_values)
    
    average_daily_usage = total_credits_used / len(sorted_dates) if sorted_dates else 0
    peak_usage = max(daily_usage_values) if daily_usage_values else 0
    peak_day = sorted_dates[daily_usage_values.index(peak_usage)] if daily_usage_values else ""
    
    # Charts use a gap-free calendar axis: a day without usage is a zero, not a
    # missing point. The trend runs from the first day with usage (inside the
    # window) to today; "last 7 days" means the last 7 calendar days, not the
    # last 7 days that happened to have usage.
    today = datetime.date.today()
    first_day = None
    if sorted_dates:
        try:
            first_day = datetime.datetime.strptime(sorted_dates[0], '%Y-%m-%d').date()
        except ValueError:
            first_day = None
    chart_days = []
    if first_day and first_day <= today:
        chart_days = [(first_day + datetime.timedelta(days=i)).isoformat()
                      for i in range((today - first_day).days + 1)]
    chart_values = [daily_usage.get(d, 0) for d in chart_days]
    last7_dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    last7_usage = [daily_usage.get(d, 0) for d in last7_dates]
    
    # Fetch current credits from users table
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT credits FROM users WHERE username = %s", (username,))
        user_row = cur.fetchone()
        cur.close()
        current_credits = user_row['credits'] if user_row and 'credits' in user_row else 0
    except Exception as e:
        current_app.logger.error("Error fetching user credits: %s", e)
        current_credits = 0
    
    # What the credits went to, from the same ledger rows (the old "app usage" and
    # "social reach" tiles read tables nothing has written to since an earlier product).
    by_purpose = defaultdict(int)
    for record in credit_records:
        label = (record.get('description') or '').strip() or 'Andet'
        by_purpose[label[:60]] += record['credits_used'] or 0
    usage_by_purpose = sorted(by_purpose.items(), key=lambda kv: -kv[1])[:5]

    # Prepare daily details for the table
    daily_details = [{'date': date, 'credits_used': daily_usage[date]} for date in sorted_dates]
    
    return render_template('fm/reports.html',
                           usage_by_purpose=usage_by_purpose,
                           usage_count=len(credit_records),
                           total_used=total_credits_used,
                           current_credits=current_credits,
                           daily_details=daily_details,
                           chart_labels=chart_days,
                           chart_data=chart_values,
                           average_usage=average_daily_usage,
                           peak_usage=peak_usage,
                           peak_day=peak_day,
                           last7_labels=last7_dates,
                           last7_usage=last7_usage,
                           credit_records=credit_records)
