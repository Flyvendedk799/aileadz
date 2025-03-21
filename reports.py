from flask import Blueprint, render_template, session, redirect, url_for, flash, current_app
import MySQLdb.cursors
from collections import defaultdict
import datetime

reports_bp = Blueprint('reports', __name__, template_folder='templates')

@reports_bp.route('/')
def reports():
    if 'user' not in session:
        flash("Please log in to view reports.", "danger")
        return redirect(url_for('auth.login'))
    username = session.get('user')
    
    # Fetch AI app usage from app_usage table
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT app_name, SUM(usage_count) AS total_usage FROM app_usage WHERE username = %s GROUP BY app_name", (username,))
        app_usage_data = cur.fetchall()
        cur.close()
    except Exception as e:
        current_app.logger.error("Error fetching app usage data: %s", e)
        app_usage_data = []
    
    app_usage_summary = {entry['app_name']: entry['total_usage'] for entry in app_usage_data}
    
    # Fetch credit usage records from credit_usage table
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT timestamp, credits_used, description FROM credit_usage WHERE username = %s ORDER BY timestamp ASC", (username,))
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
    
    # For bar chart: last 7 days
    last7_dates = sorted_dates[-7:] if len(sorted_dates) >= 7 else sorted_dates
    last7_usage = daily_usage_values[-7:] if len(daily_usage_values) >= 7 else daily_usage_values
    
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
    
    # Fetch social metrics (aggregate followers and impressions)
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT SUM(followers) AS total_followers, SUM(impressions) AS total_impressions FROM social_metrics WHERE username = %s", (username,))
        social_data = cur.fetchone()
        cur.close()
        total_followers = social_data['total_followers'] if social_data and social_data['total_followers'] is not None else 0
        total_impressions = social_data['total_impressions'] if social_data and social_data['total_impressions'] is not None else 0
    except Exception as e:
        current_app.logger.error("Error fetching social metrics: %s", e)
        total_followers = 0
        total_impressions = 0

    # Prepare daily details for the table
    daily_details = [{'date': date, 'credits_used': daily_usage[date]} for date in sorted_dates]
    
    return render_template('reports.html',
                           app_usage=app_usage_summary,
                           total_used=total_credits_used,
                           current_credits=current_credits,
                           daily_details=daily_details,
                           chart_labels=sorted_dates,
                           chart_data=daily_usage_values,
                           average_usage=average_daily_usage,
                           peak_usage=peak_usage,
                           peak_day=peak_day,
                           last7_labels=last7_dates,
                           last7_usage=last7_usage,
                           total_followers=total_followers,
                           total_impressions=total_impressions,
                           credit_records=credit_records)
