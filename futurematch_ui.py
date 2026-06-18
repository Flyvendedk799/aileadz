"""Futurematch redesign blueprint.

Serves the new Futurematch UI: the AI chat surface, the employee learning home,
and a design showcase that can render any converted page under templates/fm/.
The shared shell lives in templates/fm_base.html; individual pages extend it.
"""
import os
import datetime
from flask import (Blueprint, render_template, session, abort, redirect,
                   url_for, request, flash, current_app)

futurematch_bp = Blueprint('futurematch', __name__, template_folder='templates')

_FM_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'fm')


def _fm_pages():
    try:
        return {f[:-5] for f in os.listdir(_FM_DIR) if f.endswith('.html')}
    except OSError:
        return set()


@futurematch_bp.route('/chat')
def chat():
    """AI assistant chat surface (standalone shell with chat.js)."""
    return render_template('fm/chat.html')


@futurematch_bp.route('/ai-profiler')
def ai_profiler():
    """AI Profiler — the same chat engine in profile-completion mode."""
    if not session.get('user'):
        flash('Log ind for at bruge AI Profiler.', 'danger')
        return redirect(url_for('auth.login'))
    return render_template('fm/ai_profiler.html')


@futurematch_bp.route('/mind-map')
def mind_map():
    """Mind-Map — visualises the memories/data the AI has stored about the user."""
    if not session.get('user'):
        flash('Log ind for at se din mind-map.', 'danger')
        return redirect(url_for('auth.login'))
    return render_template('fm/mind_map.html')


# How many cards each employee-home section ever renders (cheap, bounded).
_HOME_ACTIVE_LIMIT = 4
_HOME_ORDER_LIMIT = 5
_HOME_REC_LIMIT = 3

# Maps a course_orders.status to a learner-facing "is this still in progress?"
# bucket. Anything in this set is shown under "I gang lige nu".
_HOME_ACTIVE_STATUSES = frozenset({'approved', 'processing', 'confirmed'})


def _home_skill_completeness(profile):
    """Profile-completeness ring data from a get_full_profile() snapshot.

    Consumes the canonical profile_completeness() (user_profile_db) so the ring
    number is identical across the employee home, profile page, profiler ring
    and mind-map — one source of truth, no per-surface drift. Returns
    (pct, sections, has_skills) for backwards compatibility with the template.
    """
    profile = profile or {}
    try:
        from app1.user_profile_db import profile_completeness
        c = profile_completeness(None, profile=profile)
        sections = [{'key': s.get('label'), 'done': s.get('done')} for s in c.get('sections', [])]
        return c.get('pct', 0), sections, len(profile.get('skills') or []) > 0
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
        products = result.get('products') or []
        # If a skill query found nothing, fall back to the default catalog page.
        if q and not products:
            result = catalog_service.search_products(
                filters={}, page=1, per_page=limit, company_id=company_id) or {}
            products = result.get('products') or []
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

    active = []
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
                    raw_status = r.get('status') or 'pending'
                    item = {
                        'order_id': r.get('order_id'),
                        'handle': r.get('product_handle'),
                        'title': r.get('product_title') or 'Ukendt kursus',
                        'status': raw_status,
                        'status_label': _ORDER_STATUS_LABELS.get(raw_status, raw_status),
                        'state': _ORDER_STATE.get(raw_status, 'afventer'),
                        'created_at': r.get('created_at'),
                    }
                    # "I gang lige nu": approved/active and not yet completed.
                    completed = (r.get('completion_status') or '').lower() in ('completed', 'gennemfoert', 'done')
                    if raw_status in _HOME_ACTIVE_STATUSES and not completed and len(active) < _HOME_ACTIVE_LIMIT:
                        active.append(item)
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

    return render_template(
        'fm/employee_home.html',
        active=active,
        orders=orders,
        recommendations=recommendations,
        skills_groups=skills_groups,
        skills_total=len(profile.get('skills') or []),
        completeness_pct=completeness_pct,
        completeness_sections=completeness_sections,
        has_skills=has_skills,
    )


@futurematch_bp.route('/mine-maal')
def learning_goals():
    """Development-goals dashboard for the logged-in user."""
    if not session.get('user'):
        flash('Log ind for at se dine udviklingsmål.', 'danger')
        return redirect(url_for('auth.login'))
    goals = []
    try:
        from app1.user_profile_db import get_learning_goals, ensure_tables
        ensure_tables()
        goals = get_learning_goals(session['user'])
    except Exception as e:
        current_app.logger.warning("learning goals load: %s", e)
    return render_template('fm/learning_goals.html', goals=goals)


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


# Danish status vocabulary — mirrors OrderHandler.order_statuses in app1/order_handler.py
_ORDER_STATUS_LABELS = {
    'pending': 'Afventer betaling',
    'pending_approval': 'Afventer godkendelse',
    'approved': 'Godkendt',
    'rejected': 'Afvist',
    'processing': 'Behandler',
    'confirmed': 'Bekræftet',
    'cancelled': 'Annulleret',
    'completed': 'Gennemført',
}

# Coarse state buckets used by the timeline UI for grouping/colouring.
# Maps a raw course_orders.status to one of:
#   afventer_godkendelse / godkendt / gennemfoert / annulleret / afvist / afventer
_ORDER_STATE = {
    'pending': 'afventer',
    'pending_approval': 'afventer_godkendelse',
    'approved': 'godkendt',
    'processing': 'godkendt',
    'confirmed': 'godkendt',
    'completed': 'gennemfoert',
    'cancelled': 'annulleret',
    'rejected': 'afvist',
}

_TIMELINE_TABLES_SQL = (
    """CREATE TABLE IF NOT EXISTS course_orders (
        id INT AUTO_INCREMENT PRIMARY KEY,
        order_id VARCHAR(50) UNIQUE,
        company_id INT,
        user_id INT,
        username VARCHAR(255),
        product_handle VARCHAR(255),
        product_title VARCHAR(500),
        price DECIMAL(10,2),
        status VARCHAR(30) DEFAULT 'pending',
        completion_status VARCHAR(30),
        completion_date DATETIME,
        completion_deadline DATETIME,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_user (user_id),
        INDEX idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS order_approvals (
        id INT AUTO_INCREMENT PRIMARY KEY,
        order_id VARCHAR(50) NOT NULL,
        company_id INT NOT NULL,
        requester_user_id INT NOT NULL,
        approver_user_id INT,
        status VARCHAR(30) DEFAULT 'pending',
        notes TEXT,
        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        decided_at DATETIME,
        INDEX idx_order (order_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


def _ensure_timeline_tables(conn):
    """Idempotently ensure the two tables the timeline reads exist. Boot-safe."""
    try:
        cur = conn.cursor()
        for ddl in _TIMELINE_TABLES_SQL:
            cur.execute(ddl)
        conn.commit()
        cur.close()
    except Exception as e:  # pragma: no cover - defensive
        current_app.logger.warning("timeline ensure tables: %s", e)


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
                       co.completion_status,
                       oa.status AS approval_status, oa.decided_at AS approval_decided_at
                FROM course_orders co
                LEFT JOIN order_approvals oa ON oa.order_id = co.order_id
                WHERE (co.username = %s OR (co.user_id IS NOT NULL AND co.user_id = %s))
                ORDER BY co.created_at DESC
                """,
                (username, user_id),
            )
            rows = cur.fetchall() or []
            cur.close()

            now = datetime.datetime.now()
            for r in rows:
                raw_status = (r.get('status') or 'pending')
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
                    'title': r.get('product_title') or 'Ukendt kursus',
                    'created_at': r.get('created_at'),
                    'status': raw_status,
                    'status_label': _ORDER_STATUS_LABELS.get(raw_status, raw_status),
                    'state': state,
                    'approval_status': r.get('approval_status'),
                    'approval_label': _ORDER_STATUS_LABELS.get(
                        r.get('approval_status'), r.get('approval_status')
                    ) if r.get('approval_status') else None,
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
        'aktive': sum(1 for i in items if i['state'] == 'godkendt'),
        'gennemfoert': sum(1 for i in items if i['state'] == 'gennemfoert'),
        'overdue': sum(1 for i in items if i['overdue']),
    }

    return render_template(
        'fm/timeline.html',
        items=items,
        summary=summary,
        load_error=load_error,
    )


# ── CV / job-ad ingestion (Theme C: empty-profile cold-start) ──
#
# Three steps, all strictly scoped to the logged-in user (session['user']):
#   GET  /profil-upload         -> upload + paste form
#   POST /profil-upload         -> extract + parse -> render proposal for review
#   POST /profil-upload/apply   -> write *approved* items via app1.user_profile_db
#
# The parsed profile is always a PROPOSAL — nothing is written until the user
# confirms specific items on the review step. cv_ingest is fully guarded and
# never raises, so the route degrades to a Danish error/empty state.

# How many of each item type we ever surface for review (defensive cap).
_CV_MAX_ITEMS = 60


def _cv_proposal_to_form_lists(proposal):
    """Shape a cv_ingest proposal into review-ready, index-tagged lists."""
    proposal = proposal or {}
    skills, experience, education, certifications, languages = [], [], [], [], []
    for i, s in enumerate((proposal.get('skills') or [])[:_CV_MAX_ITEMS]):
        name = (s.get('name') or '').strip()
        if not name:
            continue
        skills.append({'idx': i, 'name': name, 'level': (s.get('level') or 'mellem')})
    for i, e in enumerate((proposal.get('experience') or [])[:_CV_MAX_ITEMS]):
        title = (e.get('title') or '').strip()
        company = (e.get('company') or '').strip()
        if not (title or company):
            continue
        experience.append({'idx': i, 'title': title, 'company': company,
                           'years': (e.get('years') or '').strip()})
    for i, ed in enumerate((proposal.get('education') or [])[:_CV_MAX_ITEMS]):
        degree = (ed.get('degree') or '').strip()
        institution = (ed.get('institution') or '').strip()
        if not (degree or institution):
            continue
        education.append({'idx': i, 'degree': degree, 'institution': institution,
                         'year': (ed.get('year') or '').strip()})
    for i, c in enumerate((proposal.get('certifications') or [])[:_CV_MAX_ITEMS]):
        name = (c.get('name') or '').strip()
        if not name:
            continue
        certifications.append({'idx': i, 'name': name, 'issuer': (c.get('issuer') or '').strip(),
                               'issue_date': (c.get('issue_date') or '').strip(),
                               'expiry_date': (c.get('expiry_date') or '').strip()})
    for i, lg in enumerate((proposal.get('languages') or [])[:_CV_MAX_ITEMS]):
        language = (lg.get('language') or '').strip()
        if not language:
            continue
        languages.append({'idx': i, 'language': language,
                          'proficiency': (lg.get('proficiency') or 'mellem')})
    return skills, experience, education, certifications, languages


def _parse_years_to_int(value):
    """Best-effort: pull a 4-digit year (or plain int) from a free-text string."""
    import re
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Prefer an explicit 4-digit year; else any standalone integer.
    m = re.search(r'(19|20)\d{2}', s)
    if m:
        try:
            return int(m.group(0))
        except Exception:
            return None
    m = re.search(r'\d{1,4}', s)
    if m:
        try:
            return int(m.group(0))
        except Exception:
            return None
    return None


@futurematch_bp.route('/profil-upload', methods=['GET'])
def cv_upload():
    """Step 1: upload/paste form for CV-based profile cold-start."""
    if not session.get('user'):
        flash('Log ind for at uploade dit CV.', 'danger')
        return redirect(url_for('auth.login'))
    return render_template('fm/cv_upload.html', step='upload')


@futurematch_bp.route('/profil-upload', methods=['POST'])
def cv_upload_parse():
    """Step 2: extract text + parse into a reviewable profile PROPOSAL."""
    if not session.get('user'):
        flash('Log ind for at uploade dit CV.', 'danger')
        return redirect(url_for('auth.login'))

    text = ''
    hint = ''
    pasted = (request.form.get('cv_text') or '').strip()

    # File upload takes precedence; fall back to pasted text.
    try:
        import cv_ingest
    except Exception as e:  # pragma: no cover - import guard
        current_app.logger.warning("cv_ingest import: %s", e)
        cv_ingest = None

    upload = request.files.get('cv_file') if request.files else None
    if cv_ingest is not None and upload is not None and getattr(upload, 'filename', ''):
        try:
            text, hint = cv_ingest.extract_text(upload)
        except Exception as e:
            current_app.logger.warning("cv extract: %s", e)
            text, hint = '', ''
        # If the file yielded nothing but the user also pasted text, use that.
        if not (text or '').strip() and pasted:
            text, hint = pasted, ''
    elif pasted:
        text = pasted
    else:
        flash('Vælg en CV-fil eller indsæt teksten fra dit CV.', 'danger')
        return render_template('fm/cv_upload.html', step='upload')

    if not (text or '').strip():
        # Extraction failed — show the Danish hint and keep the user on step 1.
        flash(hint or 'Vi kunne ikke læse noget tekst. Prøv at indsætte teksten fra dit CV.',
              'danger')
        return render_template('fm/cv_upload.html', step='upload',
                               raw_text=pasted)

    proposal = {}
    if cv_ingest is not None:
        try:
            proposal = cv_ingest.parse_profile_from_text(text) or {}
        except Exception as e:
            current_app.logger.warning("cv parse: %s", e)
            proposal = {}

    skills, experience, education, certifications, languages = _cv_proposal_to_form_lists(proposal)
    has_any = bool(skills or experience or education or certifications or languages
                   or (proposal.get('summary') or '').strip())

    if not has_any:
        flash('Vi kunne ikke udlede en profil fra teksten. '
              'Du kan justere teksten og prøve igen.', 'danger')
        return render_template('fm/cv_upload.html', step='upload', raw_text=text)

    return render_template(
        'fm/cv_upload.html',
        step='review',
        summary=(proposal.get('summary') or '').strip(),
        skills=skills,
        experience=experience,
        education=education,
        certifications=certifications,
        languages=languages,
    )


@futurematch_bp.route('/profil-upload/apply', methods=['POST'])
def cv_upload_apply():
    """Step 3: write the user-APPROVED items into the profile.

    Only items whose checkbox was ticked are written. Everything is scoped to
    session['user']; no other user's profile can be touched. Guarded end-to-end.
    """
    if not session.get('user'):
        flash('Log ind for at gemme din profil.', 'danger')
        return redirect(url_for('auth.login'))

    username = session['user']
    added_skills = added_exp = added_edu = added_cert = added_lang = 0

    try:
        from app1.user_profile_db import (add_skill, add_experience,
                                          add_education, add_certification,
                                          add_language, ensure_tables)
        try:
            ensure_tables()
        except Exception as e:
            current_app.logger.warning("cv apply ensure_tables: %s", e)

        # Skills — accepted rows arrive as accept_skill = "<idx>" (one per box).
        for idx in request.form.getlist('accept_skill'):
            name = (request.form.get(f'skill_name_{idx}') or '').strip()
            level = (request.form.get(f'skill_level_{idx}') or 'mellem').strip()
            if level not in ('begynder', 'mellem', 'avanceret', 'ekspert'):
                level = 'mellem'
            if name:
                try:
                    add_skill(username, name, level, source='cv_upload')
                    added_skills += 1
                except Exception as e:
                    current_app.logger.warning("cv apply skill: %s", e)

        # Experience.
        for idx in request.form.getlist('accept_experience'):
            title = (request.form.get(f'exp_title_{idx}') or '').strip()
            company = (request.form.get(f'exp_company_{idx}') or '').strip()
            years = request.form.get(f'exp_years_{idx}') or ''
            if not title and not company:
                continue
            try:
                add_experience(username, title or company, company=company,
                               start_year=_parse_years_to_int(years))
                added_exp += 1
            except Exception as e:
                current_app.logger.warning("cv apply experience: %s", e)

        # Education.
        for idx in request.form.getlist('accept_education'):
            degree = (request.form.get(f'edu_degree_{idx}') or '').strip()
            institution = (request.form.get(f'edu_institution_{idx}') or '').strip()
            year = request.form.get(f'edu_year_{idx}') or ''
            if not degree and not institution:
                continue
            try:
                add_education(username, degree or institution, institution=institution,
                              year_completed=_parse_years_to_int(year))
                added_edu += 1
            except Exception as e:
                current_app.logger.warning("cv apply education: %s", e)

        # Certifications.
        for idx in request.form.getlist('accept_certification'):
            name = (request.form.get(f'crt_name_{idx}') or '').strip()
            issuer = (request.form.get(f'crt_issuer_{idx}') or '').strip()
            issue_date = (request.form.get(f'crt_issued_{idx}') or '').strip()
            expiry_date = (request.form.get(f'crt_expiry_{idx}') or '').strip()
            if not name:
                continue
            try:
                add_certification(username, name, issuer=issuer,
                                  issue_date=issue_date or None,
                                  expiry_date=expiry_date or None, source='cv_upload')
                added_cert += 1
            except Exception as e:
                current_app.logger.warning("cv apply certification: %s", e)

        # Languages.
        for idx in request.form.getlist('accept_language'):
            language = (request.form.get(f'lang_name_{idx}') or '').strip()
            proficiency = (request.form.get(f'lang_level_{idx}') or 'mellem').strip()
            if proficiency not in ('begynder', 'mellem', 'flydende', 'modersmaal'):
                proficiency = 'mellem'
            if not language:
                continue
            try:
                add_language(username, language, proficiency=proficiency, source='cv_upload')
                added_lang += 1
            except Exception as e:
                current_app.logger.warning("cv apply language: %s", e)
    except Exception as e:
        current_app.logger.warning("cv apply: %s", e)
        flash('Kunne ikke gemme profilen. Prøv igen.', 'danger')
        return redirect(url_for('futurematch.cv_upload'))

    total = added_skills + added_exp + added_edu + added_cert + added_lang
    if total:
        parts = []
        if added_skills:
            parts.append(f'{added_skills} kompetence' + ('r' if added_skills != 1 else ''))
        if added_exp:
            parts.append(f'{added_exp} erfaring' + ('er' if added_exp != 1 else ''))
        if added_edu:
            parts.append(f'{added_edu} uddannelse' + ('r' if added_edu != 1 else ''))
        if added_cert:
            parts.append(f'{added_cert} certificering' + ('er' if added_cert != 1 else ''))
        if added_lang:
            parts.append(f'{added_lang} sprog')
        flash('Tilføjet til din profil: ' + ', '.join(parts) + '.', 'success')
    else:
        flash('Ingen elementer blev valgt. Din profil er uændret.', 'danger')

    # Land on the profile if that route exists; otherwise back to the uploader.
    try:
        return redirect(url_for('pages.profile'))
    except Exception:
        try:
            return redirect(url_for('futurematch.employee_home'))
        except Exception:
            return redirect(url_for('futurematch.cv_upload'))


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
    return render_template(f'fm/{page}.html')
