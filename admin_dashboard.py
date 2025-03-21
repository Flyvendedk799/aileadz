from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
import logging

admin_dashboard_bp = Blueprint('admin_dashboard', __name__, template_folder='templates')

@admin_dashboard_bp.route('/credits', methods=['GET', 'POST'])
def credits():
    # Only allow admin users to access this page.
    if 'user' not in session or session.get('role') != 'admin':
        flash("Adgang nægtet.", "danger")
        return redirect(url_for('pages.notifications'))
    
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
            # Increase the target user's credits by the specified amount.
            cur.execute("UPDATE users SET credits = credits + %s WHERE username = %s", (credit_amount, target_user))
            current_app.mysql.connection.commit()
            cur.close()
            flash(f"Kreditter tilføjet til {target_user}!", "success")
        except Exception as e:
            logging.error("Error updating credits: %s", e)
            flash("Fejl ved tildeling af kreditter.", "danger")
        return redirect(url_for('admin_dashboard.credits'))
    
    return render_template('admin_credits.html')
