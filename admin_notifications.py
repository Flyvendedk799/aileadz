from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
import os
from werkzeug.utils import secure_filename

admin_notifications_bp = Blueprint('admin_notifications', __name__, template_folder='templates')

@admin_notifications_bp.route('/notifications', methods=['GET', 'POST'])
def notifications_dashboard():
    # Only allow access if the user is logged in and is an admin.
    if 'user' not in session or session.get('role') != 'admin':
        flash('Adgang nægtet.', 'danger')
        return redirect(url_for('pages.notifications'))
    
    if request.method == 'POST':
        title = request.form.get('title')
        subtitle = request.form.get('subtitle', '')
        description = request.form.get('description', '')
        
        # Combine fields into one formatted HTML message
        formatted_message = "<div style='text-align: center;'>";
        formatted_message += f"<h2>{title}</h2>";
        if subtitle:
            formatted_message += f"<h3>{subtitle}</h3>";
        if description:
            formatted_message += f"<p>{description}</p>";
        formatted_message += "</div>";
        
        target = request.form.get('target')  # 'all', 'specific', or 'role'
        specific_username = request.form.get('username') if target == 'specific' else None
        specific_role = request.form.get('role') if target == 'role' else None

        # Handle image file upload
        image_file = request.files.get('image_file')
        image_url = None
        if image_file and image_file.filename != '':
            filename = secure_filename(image_file.filename)
            upload_folder = os.path.join(current_app.root_path, "static", "uploads", "notifications")
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            new_filename = f"{session.get('user')}_{filename}"
            file_path = os.path.join(upload_folder, new_filename)
            try:
                image_file.save(file_path)
                image_url = url_for('static', filename=f"uploads/notifications/{new_filename}")
            except Exception as e:
                current_app.logger.error("Error saving image: %s", e)
                flash("Billed upload mislykkedes.", "danger")
                return redirect(url_for('admin_notifications.notifications_dashboard'))
        
        try:
            mysql = current_app.mysql
            cur = mysql.connection.cursor()
            if target == 'all':
                query = "INSERT INTO notifications (user_id, title, message, image_url) SELECT username, %s, %s, %s FROM users"
                cur.execute(query, (title, formatted_message, image_url))
            elif target == 'specific':
                query = "INSERT INTO notifications (user_id, title, message, image_url) VALUES (%s, %s, %s, %s)"
                cur.execute(query, (specific_username, title, formatted_message, image_url))
            elif target == 'role':
                query = "INSERT INTO notifications (user_id, title, message, image_url) SELECT username, %s, %s, %s FROM users WHERE role = %s"
                cur.execute(query, (title, formatted_message, image_url, specific_role))
            mysql.connection.commit()
            cur.close()
            flash('Notifikation sendt!', 'success')
        except Exception as e:
            current_app.logger.error("Error sending notification: %s", e)
            flash('Fejl under afsendelse: ' + str(e), 'danger')
        return redirect(url_for('admin_notifications.notifications_dashboard'))
    
    return render_template('notifications_admin.html')
