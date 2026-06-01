from flask import Blueprint, render_template, session, current_app

dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')


def _fetch_dashboard_kpis():
    kpis = {
        'unread_notifications': 0,
        'pending_approvals': None,
        'catalog_tools': None,
        'role_label': 'Bruger',
    }
    role = session.get('role', 'user')
    kpis['role_label'] = 'Administrator' if role == 'admin' else 'Bruger'

    user_id = session.get('user')
    company_id = session.get('company_id')

    try:
        mysql = current_app.mysql
        cur = mysql.connection.cursor()

        if user_id:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = %s AND `read` = 0",
                (user_id,),
            )
            row = cur.fetchone()
            kpis['unread_notifications'] = (row['cnt'] if row else 0) or 0

        if company_id:
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM course_orders
                WHERE company_id = %s AND status = 'pending_approval'
                """,
                (company_id,),
            )
            row = cur.fetchone()
            kpis['pending_approvals'] = (row['cnt'] if row else 0) or 0

            cur.execute(
                "SELECT COUNT(*) AS cnt FROM course_orders WHERE company_id = %s",
                (company_id,),
            )
            row = cur.fetchone()
            kpis['catalog_tools'] = (row['cnt'] if row else 0) or 0

        cur.close()
    except Exception:
        pass

    return kpis


@dashboard_bp.route('/dashboard')
def dashboard():
    return render_template('fm/index.html', kpis=_fetch_dashboard_kpis())
