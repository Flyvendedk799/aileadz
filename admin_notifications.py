from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
import os
from werkzeug.utils import secure_filename

# --- HTML sanitization for admin-supplied notification content (stored-XSS hardening) ---
# Admin broadcast fields are stored as HTML in the `notifications.message` column and may
# later be rendered with |safe. Sanitize on WRITE so every consumer is safe.
# bleach is optional: if it is unavailable we fall back to fully escaping the input
# (markupsafe.escape) — we NEVER store raw, unsanitized admin HTML.
try:
    import bleach as _bleach
    _HAS_BLEACH = True
except Exception as _bleach_exc:  # pragma: no cover - degrade gracefully, never crash boot
    _bleach = None
    _HAS_BLEACH = False

# Always-available escaping fallback.
try:
    from markupsafe import escape as _ms_escape
except Exception:  # pragma: no cover - extremely defensive
    from flask import escape as _ms_escape  # type: ignore

# Allowlist of safe inline-formatting tags for notification body text.
_NOTIF_ALLOWED_TAGS = [
    'a', 'b', 'strong', 'i', 'em', 'u', 'br', 'p', 'span',
    'ul', 'ol', 'li', 'h2', 'h3', 'h4', 'blockquote',
]
_NOTIF_ALLOWED_ATTRS = {
    'a': ['href', 'title', 'target', 'rel'],
    'span': ['style'],
    'p': ['style'],
}
# Only allow safe link protocols; this strips javascript:, data:, vbscript:, etc.
_NOTIF_ALLOWED_PROTOCOLS = ['http', 'https', 'mailto', 'tel']


def _sanitize_notification_html(value):
    """Sanitize a single admin-supplied notification field.

    Returns a string safe to store and later render. Uses bleach when available
    (allowlisted formatting tags, safe link protocols, event handlers / <script>
    stripped). When bleach is missing, fully HTML-escapes the value instead so
    nothing executable is ever persisted.
    """
    if value is None:
        return ''
    text = value if isinstance(value, str) else str(value)
    if _HAS_BLEACH:
        try:
            return _bleach.clean(
                text,
                tags=_NOTIF_ALLOWED_TAGS,
                attributes=_NOTIF_ALLOWED_ATTRS,
                protocols=_NOTIF_ALLOWED_PROTOCOLS,
                strip=True,
            )
        except Exception as exc:  # pragma: no cover - never store raw on failure
            try:
                current_app.logger.warning("bleach sanitize failed, escaping instead: %s", exc)
            except Exception:
                pass
            return str(_ms_escape(text))
    # No bleach available: escape so stored content can never execute.
    return str(_ms_escape(text))


admin_notifications_bp = Blueprint('admin_notifications', __name__, template_folder='templates')

@admin_notifications_bp.route('/notifications', methods=['GET', 'POST'])
def notifications_dashboard():
    # Only allow access if the user is logged in and is an admin.
    if 'user' not in session or session.get('role') != 'admin':
        flash('Adgang nægtet.', 'danger')
        return redirect(url_for('pages.notifications'))
    
    if request.method == 'POST':
        # Sanitize all admin-supplied content on WRITE so stored notifications
        # cannot carry stored-XSS payloads (<script>, onerror=, javascript: URLs)
        # into any consumer that later renders them with |safe.
        title = _sanitize_notification_html(request.form.get('title'))
        subtitle = _sanitize_notification_html(request.form.get('subtitle', ''))
        description = _sanitize_notification_html(request.form.get('description', ''))

        # Combine fields into one formatted HTML message. The wrapping tags are
        # app-controlled/trusted; the interpolated values are already sanitized above.
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
    
    return render_template('fm/notifications_admin.html')
