# pages.py
from flask import Blueprint, render_template, session, current_app, redirect, url_for, flash, request

pages_bp = Blueprint('pages', __name__, template_folder='templates')

@pages_bp.route('/about')
def about():
    return render_template('about.html')

@pages_bp.route('/contact')
def contact():
    return render_template('contact.html')

@pages_bp.route('/analytics')
def analytics():
    if 'user' not in session:
        flash("Please log in to view analytics.", "danger")
        return redirect(url_for('auth.login'))
    username = session.get('user')
    
    import datetime
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT timestamp, credits_used, description FROM credit_usage WHERE username = %s ORDER BY timestamp ASC", (username,))
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
    
    return render_template('analytics.html',
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
def settings():
    if 'user' not in session:
        flash('Please log in to access settings.', 'danger')
        return redirect(url_for('auth.login'))
    username = session.get('user')
    try:
        cur = current_app.mysql.connection.cursor(dictionary=True)
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
                flash("Angiv venligst dit nuværende kodeord.", "danger")
                return redirect(url_for('pages.settings'))
            if new_password != confirm_password:
                flash("Det nye kodeord og bekræftelse stemmer ikke overens.", "danger")
                return redirect(url_for('pages.settings'))
            if user_data.get('password') != current_password:
                flash("Nuværende kodeord er forkert.", "danger")
                return redirect(url_for('pages.settings'))
            try:
                cur = current_app.mysql.connection.cursor()
                cur.execute("UPDATE users SET password = %s WHERE username = %s", (new_password, username))
                current_app.mysql.connection.commit()
                cur.close()
                flash("Kodeord opdateret!", "success")
            except Exception as e:
                current_app.logger.error("Error updating password: %s", e)
                flash("Fejl ved opdatering af kodeord.", "danger")
                return redirect(url_for('pages.settings'))
        
        return redirect(url_for('pages.settings'))
    
    return render_template('indstillinger.html', user=user_data)



@pages_bp.route('/profile')
def profile():
    return render_template('profile.html')
