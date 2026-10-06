"""Futurematch redesign blueprint.

Serves the new Futurematch UI: the AI chat surface, the employee learning home,
and a design showcase that can render any converted page under templates/fm/.
The shared shell lives in templates/fm_base.html; individual pages extend it.
"""
import os
import datetime
from auth_decorators import require_company
from flask import (Blueprint, render_template, session, abort, redirect,
                   url_for, request, flash, current_app, jsonify)

futurematch_bp = Blueprint('futurematch', __name__, template_folder='templates')

_FM_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'fm')


def _fm_pages():
    try:
        return {f[:-5] for f in os.listdir(_FM_DIR) if f.endswith('.html')}
    except OSError:
        return set()


# ── Navigation state: where am I? ───────────────────────────────────────────
# The sidebar (fm_base.html) and the two section sub-navs (fm/_hr_subnav.html,
# fm/_admin_subnav.html) highlight the current location from ONE place, keyed
# on the request endpoint. Templates still may set ``page_id`` /
# ``active_hr_page`` / ``active_admin_page``; an explicit sub-nav key wins, the
# page id is the fallback for pages rendered outside their route (the /ui
# design gallery). One sidebar entry stands for a whole group of sub-nav tabs,
# so the sidebar and the sub-nav always agree on the section you are in.

# HR sub-nav tab per endpoint (keys = fm/_hr_subnav.html `_hp` values).
HR_TAB_BY_ENDPOINT = {
    'hr_dashboard.dashboard': 'dashboard',
    'hr_dashboard.team_cockpit': 'team',
    'customer_success.readiness': 'onboarding',
    'companies.employees': 'employees',
    'companies.add_employee': 'employees',
    'companies.edit_employee': 'employees',
    'bulk_invite.bulk_invite': 'employees',
    'hr_dashboard.employee_details': 'employees',
    'hr_dashboard.employee_goals': 'employees',
    'hr_dashboard.employee_progress': 'employee_progress',
    'hr_dashboard.departments': 'departments',
    'hr_dashboard.pending_approvals': 'approvals',
    'hr_dashboard.company_order_details': 'approvals',
    'hr_dashboard.approval_policies': 'approval_policies',
    'course_assign.assign_course': 'assign_course',
    'mail_delivery.deliveries': 'mail',
    'hr_dashboard.department_budgets': 'budgets',
    'hr_dashboard.billing_overview': 'billing',
    'hr_dashboard.learning_analytics': 'learning_analytics',
    'hr_dashboard.roi_dashboard': 'roi',
    'hr_dashboard.funnel_dashboard': 'funnel',
    'hr_dashboard.retention_dashboard': 'retention',
    'hr_dashboard.benchmarking_view': 'benchmarking',
    'hr_dashboard.skill_gaps_view': 'skill_gaps',
    'hr_ext.engagement': 'engagement',
    'hr_ext.ai_quality': 'ai_quality',
    'hr_ext.training_plan': 'training_plan',
    'hr_dashboard.learning_paths': 'learning_paths',
    'hr_dashboard.learning_path_steps': 'learning_paths',
    'hr_dashboard.learning_path_assignment_review': 'learning_paths',
    'hr_dashboard.bulk_assign_form': 'learning_paths',
    'hr_dashboard.internal_courses': 'internal_courses',
    'hr_dashboard.add_internal_course': 'internal_courses',
    'hr_dashboard.edit_internal_course': 'internal_courses',
    'hr_dashboard.compliance_matrix': 'compliance',
    'hr_ext.procurement': 'procurement',
    'hr_dashboard.supplier_management': 'suppliers',
    'hr_dashboard.supplier_agreements': 'suppliers',
    'hr_dashboard.reports': 'reports',
    'multitenant_reports.reports': 'reports',
}

# HR sub-nav tab -> its group (the first row of fm/_hr_subnav.html). Six groups:
# "Organisation" (people and departments) is a group of its own rather than part of
# "Overblik", so the overview stays three tabs wide and a department head's one
# organisational page (Afdelinger) does not hide behind an HR-manager-only entry.
HR_TAB_GROUP = {
    'dashboard': 'overview', 'team': 'overview', 'onboarding': 'overview',
    'employees': 'organisation', 'departments': 'organisation',
    'approvals': 'orders', 'approval_policies': 'orders', 'assign_course': 'orders', 'mail': 'orders',
    'training_plan': 'training', 'learning_paths': 'training', 'internal_courses': 'training',
    'compliance': 'training', 'skill_gaps': 'training',
    'budgets': 'finance', 'billing': 'finance', 'procurement': 'finance', 'suppliers': 'finance',
    'learning_analytics': 'insight', 'roi': 'insight', 'funnel': 'insight', 'retention': 'insight',
    'benchmarking': 'insight', 'engagement': 'insight', 'ai_quality': 'insight',
    'employee_progress': 'insight', 'reports': 'insight',
}

# The groups, in display order. ``targets`` is ((capability, endpoint), ...): the
# group's chip opens the first endpoint whose capability the viewer holds and is
# hidden when none is held. Label and icon are the sidebar entry's (fm_base.html;
# tests/test_site_cohesion.py pins the match).
HR_GROUPS = (
    {'id': 'overview', 'label': 'Overblik', 'icon': 'fa-gauge',
     'targets': (('company.workspace', 'hr_dashboard.dashboard'),)},
    {'id': 'organisation', 'label': 'Organisation', 'icon': 'fa-sitemap',
     'targets': (('company.employees', 'companies.employees'),
                 ('company.workspace', 'hr_dashboard.departments'))},
    {'id': 'orders', 'label': 'Bestillinger', 'icon': 'fa-circle-check',
     'targets': (('company.approvals', 'hr_dashboard.pending_approvals'),
                 ('company.employees', 'course_assign.assign_course'))},
    {'id': 'training', 'label': 'Læring', 'icon': 'fa-list-check',
     'targets': (('company.workspace', 'hr_ext.training_plan'),)},
    {'id': 'finance', 'label': 'Økonomi', 'icon': 'fa-wallet',
     'targets': (('company.workspace', 'hr_dashboard.department_budgets'),)},
    {'id': 'insight', 'label': 'Indsigt', 'icon': 'fa-chart-line',
     'targets': (('company.analytics', 'hr_dashboard.learning_analytics'),)},
)

# HR sub-nav tab -> the sidebar section (``hr.<section>``) that owns it: the tab's
# group, except the two pages that have a sidebar entry of their own.
HR_TAB_SECTION = {
    **HR_TAB_GROUP,
    'onboarding': 'onboarding',
    'mail': 'mail',
}

# Capability that shows a section's sidebar entry (must match the `can(...)` around
# that entry in fm_base.html). When the viewer lacks it, e.g. a department head on
# "Afdelinger", the section's entry is hidden, so "Overblik" lights up instead.
HR_SECTION_CAPABILITY = {
    'overview': 'company.workspace',
    'organisation': 'company.employees',
    'orders': 'company.approvals',
    'training': 'company.workspace',
    'finance': 'company.workspace',
    'insight': 'company.analytics',
    'onboarding': 'company.employees',
    'mail': 'company.employees',
}

# Platform-admin sub-nav tab per endpoint (keys = fm/_admin_subnav.html `_ap`
# values; the sidebar entry is ``admin.<tab>``).
ADMIN_TAB_BY_ENDPOINT = {
    'admin_dashboard.admin_home': 'home',
    'companies.admin_companies_list': 'companies',
    'companies.admin_company_detail': 'companies',
    'admin_dashboard.user_list': 'users',
    'admin_dashboard.credits': 'credits',
    'credits.admin_company_credits': 'credits',
    'admin_dashboard.admin_billing': 'billing',
    'admin_dashboard.admin_order_detail': 'billing',
    'admin_dashboard.admin_catalog': 'catalog',
    'admin_dashboard.admin_catalog_ai_preview': 'catalog',
    'admin_dashboard.admin_catalog_import_preview': 'catalog',
    'catalog_admin.products': 'catalog',
    'catalog_admin.edit_product': 'catalog',
    'admin_reports.catalog_freshness_dashboard': 'freshness',
    'admin_dashboard.admin_vendors': 'vendors',
    'admin_dashboard.admin_agreements': 'agreements',
    'admin_reports.chatbot_dashboard': 'chatbot',
    'admin_reports.conversion_funnel': 'funnel',
    'admin_reports.cohort_retention_dashboard': 'retention',
    'admin_reports.ai_cost_dashboard': 'aicost',
    'admin_dashboard.admin_ai_quality': 'aiquality',
    'admin_dashboard.ai_settings': 'ai',
    'admin_notifications.notifications_dashboard': 'notifications',
    'admin_dashboard.admin_audit_log': 'log',
    'admin_dashboard.admin_system_health': 'health',
    'gdpr.admin_console': 'gdpr',
    'futurematch.showcase_index': 'ui',
    'futurematch.showcase': 'ui',
    'mail_delivery.deliveries': 'mail',
}

# Sidebar entry for everything outside the two sub-nav sections.
SIDEBAR_BY_ENDPOINT = {
    'futurematch.employee_home': 'emphome',
    'futurematch.timeline': 'timeline',
    'futurematch.my_order': 'timeline',
    'futurematch.learning_goals': 'goals',
    'futurematch.chat': 'chat',
    'futurematch.company_chat': 'company_chat',
    'futurematch.ai_profiler': 'chat',
    'futurematch.mind_map': 'mindmap',
    'futurematch.my_cv': 'profile',
    'catalog.catalog_index': 'catalog',
    'catalog.product_detail': 'catalog',
    'catalog.category_index': 'catalog',
    'catalog.category_detail': 'catalog',
    'catalog.vendor_index': 'catalog',
    'catalog.vendor_detail': 'catalog',
    'pages.notifications': 'notifications',
    'pages.profile': 'profile',
    'pages.analytics': 'usage',
    'pages.settings': 'settings',
    'auth.account_2fa': 'settings',
    'hr_dashboard.hr_chatbot': 'hr.assistant',
    'hr_dashboard.chatbot_sessions': 'hr.assistant',
    'hr_dashboard.chatbot_session_detail': 'hr.assistant',
    'settings_hub.index': 'hr.settings',
    'settings_hub.tab': 'hr.settings',
    'companies.settings': 'hr.settings',
    'companies.branding': 'hr.settings',
    'hr_dashboard.chatbot_settings': 'hr.settings',
    'hr_dashboard.widget_creator': 'hr.settings',
    'enterprise_settings.webhooks_page': 'hr.settings',
}

# Legacy ``page_id`` values -> sidebar entry (fallback when the endpoint is unknown).
PAGE_ID_ALIASES = {
    'hr': 'hr.overview', 'team': 'hr.overview', 'compliance': 'hr.training',
    'benchmark': 'hr.insight', 'engagement': 'hr.insight', 'ai_quality': 'hr.insight',
    'company': 'hr.insight', 'training_plan': 'hr.training', 'assign_path': 'hr.training',
    'procurement': 'hr.finance', 'creports': 'hr.insight',
    'csettings': 'hr.settings', 'webhooks': 'hr.settings', 'sso': 'hr.settings',
    'analytics': 'usage', 'account-2fa': 'settings',
    'admin': 'admin.home', 'acompanies': 'admin.companies', 'ausers': 'admin.users',
    'acatalog': 'admin.catalog', 'vendors': 'admin.vendors', 'agreements': 'admin.agreements',
    'abot': 'admin.chatbot', 'aicost': 'admin.aicost', 'aiquality': 'admin.aiquality',
    'aiset': 'admin.ai', 'funnel': 'admin.funnel', 'retention': 'admin.retention',
    'freshness': 'admin.freshness', 'notif': 'admin.notifications', 'alog': 'admin.log',
    'syshealth': 'admin.health', 'gdpr': 'admin.gdpr', 'ui': 'admin.ui',
}


def _current_endpoint():
    try:
        from flask import has_request_context
        return (request.endpoint or '') if has_request_context() else ''
    except Exception:
        return ''


@futurematch_bp.app_template_global('nav_state')
def nav_state(page_id='', hr_tab='', admin_tab=''):
    """Active keys for the sidebar and the sub-navs of the current request.

    Returns ``{'side': <sidebar key>, 'hr': <HR tab>, 'admin': <admin tab>}``.
    An explicit ``hr_tab``/``admin_tab`` (the page's own setting) wins over the
    endpoint map; ``page_id`` is the last resort for the sidebar.
    """
    endpoint = _current_endpoint()
    hr = hr_tab or HR_TAB_BY_ENDPOINT.get(endpoint, '')
    admin = admin_tab or ADMIN_TAB_BY_ENDPOINT.get(endpoint, '')
    page = (page_id or '').strip()
    if endpoint in SIDEBAR_BY_ENDPOINT:
        side = SIDEBAR_BY_ENDPOINT[endpoint]
    elif hr:
        section = HR_TAB_SECTION.get(hr, 'overview')
        try:
            import capabilities
            if not capabilities.can(HR_SECTION_CAPABILITY.get(section, 'company.workspace')):
                section = 'overview'
        except Exception:
            section = 'overview'
        side = 'hr.' + section
        if admin and not session.get('company_id'):
            # A platform admin outside any company opens a shared page (the mail
            # console) through the admin navigation, not the company sidebar.
            side = 'admin.' + admin
    elif admin:
        side = 'admin.' + admin
    else:
        side = PAGE_ID_ALIASES.get(page, page)
    return {'side': side, 'hr': hr, 'admin': admin}


@futurematch_bp.app_template_global('hr_tab_group')
def hr_tab_group(tab):
    """The HR group (first row of the HR sub-nav) a tab belongs to, or ''."""
    return HR_TAB_GROUP.get(tab or '', '')


@futurematch_bp.app_template_global('hr_nav_groups')
def hr_nav_groups():
    """The HR groups the viewer may open: ``[{id, label, icon, url}]``.

    ``url`` is the group's first destination the viewer's capabilities allow, so a
    department head's "Organisation" opens Afdelinger and an HR manager's opens
    Medarbejdere; a group with no allowed destination is left out.
    """
    try:
        import capabilities
    except Exception:
        return []
    groups = []
    for group in HR_GROUPS:
        for capability, endpoint in group['targets']:
            if capabilities.can(capability):
                try:
                    groups.append({'id': group['id'], 'label': group['label'],
                                   'icon': group['icon'], 'url': url_for(endpoint)})
                except Exception:
                    pass
                break
    return groups


@futurematch_bp.route('/chat')
def chat():
    """AI assistant chat surface (standalone shell with chat.js)."""
    return render_template('fm/chat.html', chat_cfg=_chat_cfg())


def _company_chat_members(cur, company_id, user_id):
    """Only active members with a real account can exchange messages."""
    cur.execute(
        "SELECT id, user_id, COALESCE(NULLIF(full_name, ''), username, email) AS name, role "
        "FROM company_users WHERE company_id=%s AND user_id IS NOT NULL "
        "AND status='active' ORDER BY id DESC", (company_id,)
    )
    members = list(cur.fetchall())
    me = next((member for member in members if member['user_id'] == user_id), None)
    unique = {}
    for member in members:
        unique.setdefault(member['user_id'], member)
    return me, list(unique.values())


@futurematch_bp.route('/kollega-chat', methods=['GET', 'POST'])
@require_company
def company_chat():
    """Private one-to-one chat, scoped to the signed-in company and its members."""
    import MySQLdb.cursors
    company_id, user_id = session.get('company_id'), session.get('user_id')
    if not user_id:
        abort(403)
    conn = current_app.mysql.connection
    cur = conn.cursor(MySQLdb.cursors.DictCursor)
    try:
        me, members = _company_chat_members(cur, company_id, user_id)
        if not me:
            abort(403)
        peers = [m for m in members if m['id'] != me['id']]
        if request.method == 'POST':
            data = request.get_json(silent=True) or {}
            try:
                peer_id = int(data.get('recipient_id'))
            except (TypeError, ValueError):
                return jsonify(error='Vælg en kollega.'), 400
            if not any(m['id'] == peer_id for m in peers):
                return jsonify(error='Kollegaen er ikke tilgængelig i virksomheden.'), 403
            raw_body = data.get('body')
            body = raw_body.strip() if isinstance(raw_body, str) else ''
            if not body or len(body) > 5000:
                return jsonify(error='Beskeden skal være mellem 1 og 5000 tegn.'), 400
            cur.execute(
                "INSERT INTO company_chat_messages "
                "(company_id, sender_member_id, recipient_member_id, body) "
                "VALUES (%s, %s, %s, %s)",
                (company_id, me['id'], peer_id, body),
            )
            conn.commit()
            return jsonify(ok=True, id=cur.lastrowid), 201
        peer_id = request.args.get('recipient_id', type=int)
        if request.args.get('format') == 'json':
            if not peer_id or not any(m['id'] == peer_id for m in peers):
                return jsonify(error='Vælg en kollega i virksomheden.'), 403
            cur.execute(
                "SELECT id, sender_member_id, body, created_at FROM company_chat_messages "
                "WHERE company_id=%s AND "
                "((sender_member_id=%s AND recipient_member_id=%s) OR "
                "(sender_member_id=%s AND recipient_member_id=%s)) "
                "ORDER BY id DESC LIMIT 100",
                (company_id, me['id'], peer_id, peer_id, me['id']),
            )
            messages = list(reversed(cur.fetchall()))
            return jsonify(messages=[{
                'id': m['id'], 'mine': m['sender_member_id'] == me['id'],
                'body': m['body'], 'created_at': m['created_at'].isoformat(),
            } for m in messages])
        return render_template('fm/company_chat.html', peers=peers, me=me)
    finally:
        cur.close()


def _chat_cfg():
    """Role + team-order policy for the course cards (N-5.2): "Bestil til team" is
    only offered to company members when the company policy allows team orders."""
    cfg = {'teamOrders': False, 'primaryLabel': 'Anmod om plads'}
    try:
        cid = session.get('company_id')
        if session.get('user') and cid:
            import MySQLdb.cursors
            import team_order_policy as tp
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            try:
                mode = tp.effective_mode(cur, cid, None)
                pol = tp.get_policies(cur, cid)
            finally:
                cur.close()
            # Allowed when the default allows it, or any vendor override does.
            allowed = mode != tp.NOT_ALLOWED or any(m != tp.NOT_ALLOWED for m in pol['vendors'].values())
            cfg['teamOrders'] = bool(allowed)
    except Exception as e:
        current_app.logger.debug("chat cfg: %s", e)
    return cfg


@futurematch_bp.route('/ai-profiler')
def ai_profiler():
    """Legacy URL. The AI Profiler is built into the assistant, so bookmarks,
    old handoff links and the profiler's own conversations (?c=) land in /chat
    with their query string (from / focus / intent / c) intact."""
    return redirect(url_for('futurematch.chat', **request.args.to_dict(flat=True)), code=301)


@futurematch_bp.route('/mind-map')
def mind_map():
    """Mind-Map — visualises the memories/data the AI has stored about the user."""
    if not session.get('user'):
        flash('Log ind for at se din mind-map.', 'danger')
        return redirect(url_for('auth.login'))
    return render_template('fm/mind_map.html')


# How many cards each employee-home section ever renders (cheap, bounded).
_HOME_ORDER_LIMIT = 5
_HOME_REC_LIMIT = 3

import order_lifecycle as _lc  # noqa: E402  (status vocabulary, N-1.1)


def _home_skill_completeness(profile):
    """Profile-completeness ring data from a get_full_profile() snapshot.

    Consumes the canonical profile_completeness() (user_profile_db) and shows
    its depth-aware ``weighted_pct``, the number the profile page, the profiler
    banner, the chat status and the Mind-Map show. Returns
    (pct, sections, has_skills) for backwards compatibility with the template.
    """
    profile = profile or {}
    try:
        from app1.user_profile_db import profile_completeness
        c = profile_completeness(None, profile=profile)
        sections = [{'key': s.get('label'), 'done': s.get('done')} for s in c.get('sections', [])]
        shown = c.get('weighted_pct') if c.get('weighted_pct') is not None else c.get('pct', 0)
        return shown, sections, len(profile.get('skills') or []) > 0
    except Exception:
        # Defensive fallback: never break the home page on a completeness hiccup.
        skills = profile.get('skills') or []
        sections = [
            {'key': 'Profil', 'done': bool(profile.get('headline') or profile.get('bio'))},
            {'key': 'Kompetencer', 'done': len(skills) > 0},
            {'key': 'Erfaring', 'done': len(profile.get('experience') or []) > 0},
            {'key': 'Uddannelse', 'done': len(profile.get('education') or []) > 0},
            {'key': 'Mål', 'done': bool(profile.get('goals'))},
        ]
        done = sum(1 for s in sections if s['done'])
        pct = round(done / len(sections) * 100) if sections else 0
        return pct, sections, bool(skills)


def _home_recommendations(profile, company_id, limit=_HOME_REC_LIMIT):
    """Cheap, non-LLM course recommendations for the learner home.

    Strategy (no model call, page-route safe):
      1. If the user has skills, run a catalog keyword search on the first skill
         name — this reuses catalog_service.search_products (pure in-memory).
      2. Otherwise fall back to the first page of the catalog (popular/recent by
         the catalog's default ordering).
    Company-scoped pricing is applied when company_id is present. Always returns
    a list (possibly empty); never raises.
    """
    profile = profile or {}
    skills = profile.get('skills') or []
    why = 'Populært i kataloget lige nu'
    products = []
    try:
        import catalog_service
        q = ''
        if skills:
            q = (skills[0].get('name') or '').strip()
            if q:
                why = f'Matcher din kompetence: {q}'
        result = catalog_service.search_products(
            filters={'q': q} if q else {},
            page=1, per_page=limit, company_id=company_id,
        ) or {}
        products = catalog_service.exclude_stale(result.get('products') or [])
        # If a skill query found nothing, fall back to the default catalog page.
        if q and not products:
            result = catalog_service.search_products(
                filters={}, page=1, per_page=limit, company_id=company_id) or {}
            products = catalog_service.exclude_stale(result.get('products') or [])
            why = 'Populært i kataloget lige nu'
    except Exception as e:  # pragma: no cover - defensive
        current_app.logger.warning("home recommendations: %s", e)
        return []

    recs = []
    for p in products[:limit]:
        recs.append({
            'handle': p.get('handle'),
            'title': p.get('title') or 'Ukendt kursus',
            'vendor': p.get('vendor') or '',
            'price_min': p.get('price_min'),
            'price_label': p.get('price_label'),
            'format': p.get('format') or '',
            'why': why,
        })
    return recs


@futurematch_bp.route('/min-laering')
def employee_home():
    """Employee learning home — real, user + company scoped data.

    Read-only; cheap (no LLM). Every section degrades to its empty-state when it
    truly has no data, and a load failure never breaks the page.
    """
    username = session.get('user')
    user_id = session.get('user_id')
    company_id = session.get('company_id')

    orders = []
    profile = {}
    skills_groups = []
    completeness_pct = 0
    completeness_sections = []
    has_skills = False
    recommendations = []

    # ── Orders (active + recent) from course_orders, strictly user-scoped ──
    if username:
        try:
            import MySQLdb.cursors
            from db_compat import refresh_flask_mysql_connection
            mysql = getattr(current_app, 'mysql', None)
            refresh_flask_mysql_connection(mysql)
            conn = mysql.connection if mysql else None
            if conn is not None:
                _ensure_timeline_tables(conn)
                cur = conn.cursor(MySQLdb.cursors.DictCursor)
                cur.execute(
                    """
                    SELECT order_id, product_handle, product_title, price, status,
                           completion_status, completion_deadline, created_at
                    FROM course_orders
                    WHERE (username = %s OR (user_id IS NOT NULL AND user_id = %s))
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (username, user_id),
                )
                rows = cur.fetchall() or []
                cur.close()
                for r in rows:
                    raw_status = _lc.normalize_status(r.get('status'))
                    item = {
                        'order_id': r.get('order_id'),
                        'url': url_for('futurematch.my_order', order_id=r.get('order_id')),
                        'deadline': r.get('completion_deadline'),
                        'handle': r.get('product_handle'),
                        'title': r.get('product_title') or 'Ukendt kursus',
                        'status': raw_status,
                        'status_label': _ORDER_STATUS_LABELS.get(raw_status, raw_status),
                        'state': _ORDER_STATE.get(raw_status, 'afventer'),
                        'created_at': r.get('created_at'),
                    }
                    if len(orders) < _HOME_ORDER_LIMIT:
                        orders.append(item)
        except Exception as e:
            current_app.logger.warning("home orders load: %s", e)

    # ── Profile snapshot → completeness ring + skill groups ──
    if username:
        try:
            from app1.user_profile_db import get_full_profile, ensure_tables
            ensure_tables()
            profile = get_full_profile(username) or {}
        except Exception as e:
            current_app.logger.warning("home profile load: %s", e)
            profile = {}

    completeness_pct, completeness_sections, has_skills = _home_skill_completeness(profile)

    # Group skills by level for the compact competence card (highest first).
    if profile.get('skills'):
        _level_order = ['ekspert', 'avanceret', 'mellem', 'begynder']
        _level_labels = {'begynder': 'Begynder', 'mellem': 'Mellem',
                         'avanceret': 'Avanceret', 'ekspert': 'Ekspert'}
        _by_level = {}
        for s in profile['skills']:
            _by_level.setdefault((s.get('level') or 'mellem'), []).append(s.get('name'))
        for lvl in _level_order:
            names = [n for n in (_by_level.get(lvl) or []) if n]
            if names:
                skills_groups.append({'level': lvl, 'label': _level_labels.get(lvl, lvl),
                                      'names': names})

    # ── Recommendations (cheap catalog fallback; no LLM) ──
    recommendations = _home_recommendations(profile, company_id)

    # ── Goals and deadlines (N-8.5): what the learner is working toward ──
    goals = []
    try:
        from app1.user_profile_db import get_learning_goals
        goals = [g for g in (get_learning_goals(username) or []) if (g.get('status') or 'aktiv') == 'aktiv'][:3]
    except Exception as e:
        current_app.logger.debug("home goals load: %s", e)
    today = datetime.date.today()
    deadlines = []
    for o in orders:
        d = o.get('deadline')
        if d and o['status'] in ('approved', 'booked'):
            try:
                dd = d.date() if hasattr(d, 'date') else d
                days = (dd - today).days
            except Exception:
                continue
            deadlines.append({'title': o['title'], 'url': o['url'], 'date': dd, 'days': days,
                              'overdue': days < 0, 'soon': 0 <= days <= 14})
    deadlines.sort(key=lambda x: x['date'])

    # ── First-run welcome (N-2.2): shown until dismissed or the profile is alive ──
    show_welcome = False
    if username and user_id and not has_skills:
        try:
            wc = current_app.mysql.connection.cursor(MySQLdb_cursors_dict())
            wc.execute("SELECT first_login_completed AS f FROM users WHERE id = %s", (user_id,))
            wr = wc.fetchone()
            wc.close()
            show_welcome = bool(wr) and not int(wr.get('f') or 0)
        except Exception:
            show_welcome = False

    # ── "Tildelt af HR": learning paths HR assigned to me, with due dates ──
    hr_assignments = []
    if username and user_id and company_id:
        try:
            import MySQLdb.cursors
            import learning_path_service
            _c = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            hr_assignments = learning_path_service.assignments_for_learner(_c, user_id, company_id)
            _c.close()
        except Exception as e:
            current_app.logger.warning("home hr assignments: %s", e)

    return render_template(
        'fm/employee_home.html',
        goals=goals,
        deadlines=deadlines[:4],
        show_welcome=show_welcome,
        orders=orders,
        recommendations=recommendations,
        hr_assignments=hr_assignments,
        skills_groups=skills_groups,
        skills_total=len(profile.get('skills') or []),
        completeness_pct=completeness_pct,
        completeness_sections=completeness_sections,
        has_skills=has_skills,
    )


def MySQLdb_cursors_dict():
    import MySQLdb.cursors
    return MySQLdb.cursors.DictCursor


@futurematch_bp.route('/min-laering/velkommen/luk', methods=['POST'])
def dismiss_welcome():
    """Record first_login_completed so the welcome card stops showing."""
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    try:
        cur = current_app.mysql.connection.cursor()
        cur.execute("UPDATE users SET first_login_completed = 1 WHERE id = %s", (session['user_id'],))
        current_app.mysql.connection.commit()
        cur.close()
    except Exception as e:
        current_app.logger.warning("dismiss welcome: %s", e)
    return redirect(url_for('futurematch.employee_home'))


@futurematch_bp.route('/mine-maal')
def learning_goals():
    """Development-goals dashboard for the logged-in user."""
    if not session.get('user'):
        flash('Log ind for at se dine udviklingsmål.', 'danger')
        return redirect(url_for('auth.login'))
    goals = []
    manager_goals = []
    try:
        from app1.user_profile_db import get_learning_goals, ensure_tables
        ensure_tables()
        goals = get_learning_goals(session['user'])
    except Exception as e:
        current_app.logger.warning("learning goals load: %s", e)
    # "Mål fra din leder": ONLY goals HR chose to share (N-3.5 / S-4.4).
    if session.get('company_id') and session.get('user_id'):
        try:
            import MySQLdb.cursors
            import goal_sharing_ui as goal_sharing
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
            manager_goals = goal_sharing.shared_goals_for_learner(
                cur, session['user_id'], session['company_id'], conn=current_app.mysql.connection)
            cur.close()
        except Exception as e:
            current_app.logger.warning("manager goals load: %s", e)
    return render_template('fm/learning_goals.html', goals=goals, manager_goals=manager_goals)


@futurematch_bp.route('/mine-maal/add', methods=['POST'])
def learning_goal_add():
    if not session.get('user'):
        return redirect(url_for('auth.login'))
    title = (request.form.get('title') or '').strip()
    if title:
        try:
            from app1.user_profile_db import add_learning_goal, ensure_tables
            ensure_tables()
            add_learning_goal(session['user'], title, request.form.get('description', ''), request.form.get('target_date'))
            flash('Udviklingsmål oprettet.', 'success')
        except Exception as e:
            current_app.logger.warning("goal add: %s", e)
            flash('Kunne ikke oprette målet.', 'danger')
    return redirect(url_for('futurematch.learning_goals'))


@futurematch_bp.route('/mine-maal/<int:goal_id>/status', methods=['POST'])
def learning_goal_status(goal_id):
    if not session.get('user'):
        return redirect(url_for('auth.login'))
    action = request.form.get('action')
    try:
        from app1.user_profile_db import update_learning_goal, delete_learning_goal, ensure_tables
        ensure_tables()
        if action == 'slet':
            delete_learning_goal(session['user'], goal_id)
        elif action in ('aktiv', 'fuldfoert', 'paa_pause'):
            update_learning_goal(session['user'], goal_id, status=action)
    except Exception as e:
        current_app.logger.warning("goal status: %s", e)
    return redirect(url_for('futurematch.learning_goals'))


# Status vocabulary comes from order_lifecycle (N-1.1): one source for every
# label map in the app. The names below stay for older imports.
_ORDER_STATUS_LABELS = dict(_lc.STATUS_LABELS)
for _legacy, _canon in _lc.LEGACY_ALIASES.items():
    _ORDER_STATUS_LABELS[_legacy] = _lc.STATUS_LABELS[_canon]

# Coarse state buckets used by the timeline UI for grouping/colouring.
_ORDER_STATE = dict(_lc.LEARNER_BUCKETS)
_ASSIGNMENT_APPROVAL_NOTE = _lc.ASSIGNMENT_APPROVAL_NOTE
for _legacy, _canon in _lc.LEGACY_ALIASES.items():
    _ORDER_STATE[_legacy] = _lc.LEARNER_BUCKETS[_canon]


def _ensure_timeline_tables(conn):
    """Kept for callers: course_orders/order_approvals now have ONE definition in
    enterprise_tables (N-3.4), created at boot. Nothing to do here."""
    return None


@futurematch_bp.route('/min-tidslinje')
def timeline():
    """Learner deadline/approval-status timeline for the logged-in user.

    Closes the "hvor er mit kursus?" gap: shows every course the learner has
    ordered with its current status, approval state and completion deadline.
    Strictly scoped to the requesting user (username OR user_id) — never leaks
    other users' orders even within the same company.
    """
    if not session.get('user'):
        flash('Log ind for at se din tidslinje.', 'danger')
        return redirect(url_for('auth.login'))

    username = session.get('user')
    user_id = session.get('user_id')
    items = []
    load_error = False

    try:
        import MySQLdb.cursors
        from db_compat import refresh_flask_mysql_connection
        mysql = getattr(current_app, 'mysql', None)
        refresh_flask_mysql_connection(mysql)
        conn = mysql.connection if mysql else None
        if conn is not None:
            _ensure_timeline_tables(conn)
            cur = conn.cursor(MySQLdb.cursors.DictCursor)
            # Scope strictly to this user. user_id may be NULL on some legacy rows,
            # so also match by username; %s placeholders prevent injection.
            cur.execute(
                """
                SELECT co.order_id, co.product_title, co.price, co.status,
                       co.created_at, co.completion_deadline, co.completion_date,
                       co.completion_status, co.variant_date, co.variant_location,
                       co.request_notes,
                       oa.status AS approval_status, oa.decided_at AS approval_decided_at,
                       oa.notes AS approval_notes,
                       COALESCE(NULLIF(TRIM(acu.full_name), ''), au.username) AS approver_name
                FROM course_orders co
                LEFT JOIN order_approvals oa ON oa.order_id = co.order_id
                LEFT JOIN users au ON au.id = oa.approver_user_id
                LEFT JOIN company_users acu ON acu.user_id = oa.approver_user_id AND acu.company_id = co.company_id
                WHERE (co.username = %s OR (co.user_id IS NOT NULL AND co.user_id = %s))
                ORDER BY co.created_at DESC
                """,
                (username, user_id),
            )
            rows = cur.fetchall() or []
            pending_changes = set()
            order_ids = [r.get('order_id') for r in rows if r.get('order_id')]
            if order_ids:
                # One grouped lookup for every order with an open change request.
                cur.execute(
                    "SELECT DISTINCT order_id FROM course_order_changes "
                    "WHERE status = 'pending' AND order_id IN (%s)" % ",".join(["%s"] * len(order_ids)),
                    tuple(order_ids),
                )
                pending_changes = {c.get('order_id') for c in (cur.fetchall() or [])}
            cur.close()

            now = datetime.datetime.now()
            for r in rows:
                raw_status = _lc.normalize_status(r.get('status'))
                deadline = r.get('completion_deadline')
                completion_date = r.get('completion_date')
                state = _ORDER_STATE.get(raw_status, 'afventer')
                # Overdue only matters while still open (not completed/cancelled/rejected).
                overdue = bool(
                    deadline
                    and state not in ('gennemfoert', 'annulleret', 'afvist')
                    and deadline < now
                )
                price = r.get('price')
                items.append({
                    'order_id': r.get('order_id'),
                    'url': url_for('futurematch.my_order', order_id=r.get('order_id')),
                    'variant_date': r.get('variant_date'),
                    'variant_location': r.get('variant_location'),
                    'title': r.get('product_title') or 'Ukendt kursus',
                    'created_at': r.get('created_at'),
                    'status': raw_status,
                    'status_label': _ORDER_STATUS_LABELS.get(raw_status, raw_status),
                    'state': state,
                    'approval_status': r.get('approval_status'),
                    # The approval decision is only news while the order awaits it;
                    # afterwards the order status already says it.
                    'approval_label': (
                        _lc.approval_label(r.get('approval_status')) or None
                    ) if raw_status == _lc.PENDING_APPROVAL else None,
                    'change_pending': r.get('order_id') in pending_changes,
                    # "Tildelt af {navn} · godkendt": assigned by HR/a manager (approved at assignment).
                    'assigned_by': (r.get('approver_name') if r.get('approval_notes') == _ASSIGNMENT_APPROVAL_NOTE
                                    else ((r.get('request_notes') or '')[len('Tildelt af '):].strip()
                                          if (r.get('request_notes') or '').startswith('Tildelt af ') else None)),
                    'assigned_approved': r.get('approval_notes') == _ASSIGNMENT_APPROVAL_NOTE,
                    'deadline': deadline,
                    'completion_date': completion_date,
                    'overdue': overdue,
                    'price': float(price) if price is not None else None,
                })
    except Exception as e:
        load_error = True
        current_app.logger.warning("timeline load: %s", e)

    # Lightweight summary for the header strip.
    summary = {
        'total': len(items),
        'afventer': sum(1 for i in items if i['state'] in ('afventer', 'afventer_godkendelse')),
        'aktive': sum(1 for i in items if i['state'] in ('godkendt', 'booket')),
        'gennemfoert': sum(1 for i in items if i['state'] == 'gennemfoert'),
        'overdue': sum(1 for i in items if i['overdue']),
    }

    return render_template(
        'fm/timeline.html',
        items=items,
        summary=summary,
        load_error=load_error,
    )


# ── CV on the profile page ──
#
# The CV portal (/profil-upload, a separate 3D page with a no-JS form fallback) was merged
# into the profile page: upload/paste, review and apply happen inline there
# (static/futurematch/assets/profile-cv.js, templates/fm/_cv_import.html) on the same
# /api/cv/* pipeline. The parsed profile is always a PROPOSAL; nothing is written until the
# person confirms items. Only the legacy URL and the generated-CV view live here.

@futurematch_bp.route('/profil-upload')
def cv_upload():
    """Legacy URL (bookmarks, old links, the AI's open_cv_upload action): the CV
    import now lives on the profile page."""
    return redirect(url_for('pages.profile') + '#cv', code=301)


@futurematch_bp.route('/profil/cv')
def my_cv():
    """The Futurematch-generated CV: the person's own profile laid out as a
    printable CV (browser print -> PDF). Read-only, strictly the logged-in user's data."""
    if not session.get('user'):
        flash('Log ind for at se dit CV.', 'danger')
        return redirect(url_for('auth.login'))
    username = session['user']
    profile = {}
    try:
        from app1.user_profile_db import ensure_tables, get_full_profile
        ensure_tables()
        profile = get_full_profile(username) or {}
    except Exception as e:
        current_app.logger.warning("my_cv profile: %s", e)
    has_content = any(profile.get(k) for k in (
        'bio', 'skills', 'experience', 'education', 'certifications',
        'completed_courses', 'languages', 'portfolio_links'))
    return render_template('fm/my_cv.html', p=profile, name=username, has_content=has_content)


def _require_showcase_admin():
    """Guard for the internal design gallery: login + platform-admin only.

    Returns a redirect response if the request should be blocked, else None.
    """
    if not session.get('user'):
        flash('Log ind for at se designgalleriet.', 'danger')
        return redirect(url_for('auth.login'))
    if session.get('role') != 'admin':
        flash('Designgalleriet er kun tilgængeligt for administratorer.', 'danger')
        return redirect(url_for('dashboard.dashboard'))
    return None


# Mock pages that only exist in the design gallery (N-4.1): clearly labelled there.
_GALLERY_ONLY_PAGES = frozenset({
    'admin_chatbot', 'mt_dashboard', 'report_detail', 'profile', 'sso_login', 'widget_chat', 'mt_order_detail',
})


@futurematch_bp.route('/ui')
def showcase_index():
    """Gallery of every Futurematch design page (for review / navigation)."""
    guard = _require_showcase_admin()
    if guard is not None:
        return guard
    pages = sorted(_fm_pages())
    return render_template('fm/_showcase_index.html', pages=pages)


@futurematch_bp.route('/ui/<page>')
def showcase(page):
    """Render any converted Futurematch page by name."""
    guard = _require_showcase_admin()
    if guard is not None:
        return guard
    if page not in _fm_pages() or page.startswith('_'):
        abort(404)
    if page in _GALLERY_ONLY_PAGES:
        flash('Designgalleri: denne side viser eksempeldata og er ikke koblet til rigtige data. '
              'Den rigtige version findes i HR-workspace.', 'warning')
    return render_template(f'fm/{page}.html')


# Learner order detail + completion moment (N-1.2 / N-1.3 / N-1.4).
from learner_orders import register_learner_order_routes  # noqa: E402

register_learner_order_routes(futurematch_bp)
