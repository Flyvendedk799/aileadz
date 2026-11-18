"""Connected employee/HR talent workspace. All mutations are POST + global CSRF.

No private career exploration is persisted. Every action rechecks active membership,
company, module gates and capabilities. Hiring access is scoped to the opportunity.
"""
import json
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from auth_decorators import login_required
from capabilities import can
import talent_service as service
from competency_model import evaluate

talent_bp = Blueprint('talent', __name__)


def acting_admin():
    """Honor the existing, time-limited platform-admin company workspace."""
    import time
    return (can('platform.admin') and session.get('company_id')
            and session.get('admin_acting_company_id') == session.get('company_id')
            and (session.get('admin_acting_until') or 0) > time.time())


def context(*, management=False):
    cid, user_id = session.get('company_id'), session.get('user_id')
    if not cid or not user_id:
        abort(403)
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        if not (management and acting_admin()):
            service.member(cur, cid, user_id)
    except ValueError:
        cur.close()
        abort(403)
    return cur, cid, user_id


def required(cur, cid, module):
    if not service.flags(cur, cid).get(module + '_enabled'):
        abort(404)


def _profile_schema():
    """Goal visibility lives on the profile's goal table; make sure its columns exist
    before a transaction starts (cached after the first call in a process)."""
    try:
        from app1.user_profile_db import ensure_tables
        ensure_tables()
    except Exception:
        current_app.logger.debug("profile schema step unavailable", exc_info=True)


def hr_access():
    if not can('company.employees'):
        abort(403)


def profile():
    from app1.user_profile_db import get_full_profile
    return get_full_profile(session['user'])


def rows(cur, sql, args):
    cur.execute(sql, args)
    return [service.unpack(r) for r in cur.fetchall()]


@talent_bp.route('/udvikling')
@login_required
def workspace():
    cur = None
    try:
        cur, cid, user_id = context()
        p = profile()
        state = service.employee_state(cur, cid, user_id, p)
        settings = service.flags(cur, cid)
        links = service.goals(cur, cid, user_id)
        orders = rows(cur, 'SELECT order_id,product_title,status FROM course_orders WHERE company_id=%s AND user_id=%s ORDER BY created_at DESC LIMIT 100', (cid,user_id))
        for item in links:
            item['readiness'] = evaluate(state, item['requirements'])
        role_list = service.roles(cur, cid) if settings.get('roles_enabled') else []
        exploration = None
        if request.args.get('role') and settings.get('roles_enabled'):
            selected = service.role(cur, cid, request.args['role'])
            exploration = dict(role=selected, readiness=evaluate(state, selected['requirements'], version=selected['id']))
        apps = rows(cur, 'SELECT a.*,o.title FROM talent_applications a JOIN talent_opportunities o ON o.id=a.opportunity_id AND o.company_id=a.company_id '
                    'WHERE a.company_id=%s AND a.user_id=%s ORDER BY a.created_at DESC', (cid, user_id)) if settings.get('mobility_enabled') else []
        return render_template('fm/talent_workspace.html', state=state, settings=settings, goals=links, profile=p,
                               roles=role_list, exploration=exploration, applications=apps, orders=orders, stages=service.STAGE_LABELS, token=service.uid())
    except ValueError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('talent.workspace'))
    except Exception as exc:
        if getattr(exc, 'code', None):
            raise
        current_app.logger.exception('Talent workspace unavailable')
        return render_template('fm/talent_unavailable.html'), 503
    finally:
        if cur:
            cur.close()


@talent_bp.route('/muligheder')
@login_required
def opportunities():
    cur, cid, user_id = context()
    try:
        required(cur, cid, 'mobility')
        state = service.employee_state(cur, cid, user_id, profile())
        items = rows(cur, "SELECT * FROM talent_opportunities WHERE company_id=%s AND status='published' AND deadline>=CURRENT_DATE ORDER BY deadline,title", (cid,))
        for item in items:
            r = service.role(cur, cid, item['role_version_id'])
            item['readiness'] = evaluate(state, r['requirements'], version=r['id'])
        return render_template('fm/talent_opportunities.html', opportunities=items)
    finally:
        cur.close()


@talent_bp.route('/muligheder/<oid>/ansoeg', methods=['GET', 'POST'])
@login_required
def application(oid):
    cur, cid, user_id = context()
    try:
        required(cur, cid, 'mobility')
        opp = service.opportunity(cur, cid, oid)
        if opp['status'] != 'published':
            abort(404)
        p = profile()
        state = service.employee_state(cur, cid, user_id, p)
        allowed = ['headline', 'bio', 'skills', 'experience', 'education', 'certifications', 'completed_courses', 'languages', 'portfolio_links']
        sections = request.form.getlist('sections') if request.method == 'POST' else allowed
        preview = service.application_snapshot(p, state, sections)
        # Preview is re-created from the server profile; never accept client-supplied evidence.
        if request.method == 'POST' and request.form.get('confirm') == 'yes':
            from hashlib import sha256
            digest = sha256(service.dump(preview).encode()).hexdigest()
            if request.form.get('snapshot_digest') != digest:
                raise ValueError('Profilen er ændret. Gennemse ansøgningen igen før du sender.')
            service.apply(cur, cid, user_id, oid, preview)
            current_app.mysql.connection.commit()
            flash('Ansøgningen er sendt. Kun de valgte oplysninger er delt.', 'success')
            return redirect(url_for('talent.workspace'))
        from hashlib import sha256
        digest = sha256(service.dump(preview).encode()).hexdigest()
        return render_template('fm/talent_application.html', opportunity=opp, sections=sections, preview=preview,
                               section_options=allowed, digest=digest)
    except ValueError as exc:
        current_app.mysql.connection.rollback()
        if request.method == 'GET':
            abort(404)
        flash(str(exc), 'warning')
        return redirect(url_for('talent.application', oid=oid))
    finally:
        cur.close()


@talent_bp.route('/hr/talent')
@login_required
def hr():
    hr_access()
    cur, cid, user_id = context(management=True)
    try:
        settings = service.flags(cur, cid)
        role_list = service.roles(cur, cid, published=False) if settings.get('roles_enabled') else []
        people = rows(cur, "SELECT u.id,u.username,cu.department FROM users u JOIN company_users cu ON cu.user_id=u.id WHERE cu.company_id=%s AND cu.status='active' ORDER BY u.username", (cid,))
        goals = service.goals(cur, cid, hr=True)
        reviews = rows(cur, "SELECT r.order_id,o.product_title FROM learning_outcome_reviews r JOIN course_orders o ON o.order_id=r.order_id AND o.company_id=r.company_id WHERE r.company_id=%s AND o.status='completed' AND r.status!='completed' ORDER BY r.created_at LIMIT 100", (cid,))
        opportunities = rows(cur, 'SELECT o.* FROM talent_opportunities o JOIN talent_hiring_members h ON h.company_id=o.company_id AND h.opportunity_id=o.id '
                             'WHERE o.company_id=%s AND h.user_id=%s ORDER BY o.created_at DESC', (cid, user_id)) if settings.get('mobility_enabled') else []
        applications = rows(cur, 'SELECT a.*,o.title FROM talent_applications a JOIN talent_opportunities o ON o.id=a.opportunity_id AND o.company_id=a.company_id '
                            'JOIN talent_hiring_members h ON h.company_id=o.company_id AND h.opportunity_id=o.id '
                            'WHERE a.company_id=%s AND h.user_id=%s ORDER BY a.updated_at DESC', (cid, user_id)) if settings.get('mobility_enabled') else []
        scenarios = rows(cur, 'SELECT * FROM talent_scenarios WHERE company_id=%s ORDER BY created_at DESC', (cid,)) if settings.get('planning_enabled') else []
        for scenario in scenarios:
            people_in_scope = [p for p in people if not scenario['department'] or p['department'] == scenario['department']]
            from kanon import _resolve_k_default
            # Bounded pilot workload; never present a partial cohort as the whole workforce.
            if len(people_in_scope) > 500:
                scenario['coverage'] = None
            else:
                states = service.cohort_states(cur, cid, people_in_scope)
                scenario['coverage'] = service.coverage(states, scenario['requirements'], _resolve_k_default())
            total = 0
            available = True
            import catalog_service
            for action in scenario['actions']:
                product = catalog_service.get_product(action['handle'])
                product = catalog_service.decorate_products_with_discounts([product], company_id=cid)[0] if product else None
                price = (product or {}).get('price_min')
                action['title'] = (product or {}).get('title') or 'Kursus ikke tilgængeligt'
                action['price'] = price
                if price is None:
                    available = False
                else:
                    total += float(price) * action['people']
            scenario['estimated_cost'] = total if available else None
        return render_template('fm/talent_hr.html', settings=settings, roles=role_list, people=people, goals=goals, reviews=reviews,
                               opportunities=opportunities, applications=applications, scenarios=scenarios,
                               stages=service.STAGE_LABELS, transitions=service.STAGES, token=service.uid(),
                               catalog_courses=__import__('catalog_service').search_products(filters={'q': request.args.get('q', '')}, per_page=50, company_id=cid).get('products', []))
    finally:
        cur.close()


@talent_bp.route('/talent/actions/<action>', methods=['POST'])
@login_required
def action(action):
    f = request.form
    _profile_schema()
    hr_actions = {'settings', 'role', 'publish_role', 'assign_role', 'manager_assessment', 'opportunity', 'publish_opportunity', 'close_opportunity', 'placement', 'scenario', 'publish_scenario'}
    cur, cid, user_id = context(management=action in hr_actions or action == 'transition')
    dest = 'talent.hr' if action in hr_actions or f.get('return_to') == 'hr' and can('company.employees') else 'talent.workspace'
    if action == 'transition' and f.get('return_to') == 'hiring':
        dest = 'talent.hiring'
    try:
        if action in hr_actions:
            hr_access()
        if action == 'settings':
            if not can('company.settings'):
                abort(403)
            service.set_flags(cur, cid, f)
        elif action in ('role', 'publish_role', 'assign_role'):
            required(cur, cid, 'roles')
            if action == 'role':
                service.save_role(cur, cid, f.get('title',''), f.get('requirements',''), f.get('role_id') or None)
            elif action == 'publish_role':
                service.role(cur, cid, f['id'], published=False)
                cur.execute('UPDATE talent_role_versions SET published=1 WHERE company_id=%s AND id=%s', (cid, f['id']))
            else:
                service.assign_role(cur, cid, int(f['user_id']), f['role_version_id'])
        elif action in ('self_assessment', 'manager_assessment'):
            target = int(f['user_id']) if action == 'manager_assessment' else user_id
            service.assess(cur, cid, target, f.get('skill',''), int(f['level']), 'manager' if action == 'manager_assessment' else 'self', user_id, f['token'], f.get('note',''))
        elif action == 'goal':
            service.link_goal(cur, cid, user_id, int(f['goal_id']), f['requirements'], f['review_date'],
                              [dict(kind=f['kind'], title=f['next_step'], order_id=f.get('order_id'))], bool(f.get('shared')))
        elif action == 'share_goal':
            service.share_goal(cur, cid, user_id, f['id'], f.get('shared') == 'yes')
        elif action == 'complete_action':
            cur.execute('SELECT * FROM talent_goal_links WHERE company_id=%s AND user_id=%s AND id=%s FOR UPDATE', (cid, user_id, f['id']))
            row = cur.fetchone()
            if not row:
                abort(404)
            actions = json.loads(row['actions_json'])
            for item in actions:
                if item['id'] == f['action_id'] and item['kind'] in ('practice', 'mentoring'):
                    item['done'] = True
            cur.execute('UPDATE talent_goal_links SET actions_json=%s WHERE company_id=%s AND user_id=%s AND id=%s', (service.dump(actions), cid, user_id, f['id']))
        elif action in ('opportunity', 'publish_opportunity', 'close_opportunity'):
            required(cur, cid, 'mobility')
            if action == 'opportunity':
                service.save_opportunity(cur, cid, user_id, {**f.to_dict(), 'team': f.getlist('team')}, external_owner=bool(acting_admin()))
            else:
                if not service.hiring_access(cur, cid, user_id, f['id']):
                    abort(403)
                opp = service.opportunity(cur, cid, f['id'])
                if action == 'publish_opportunity' and service.date(opp['deadline']) < service.dt.date.today():
                    raise ValueError('Fristen er udløbet.')
                cur.execute('UPDATE talent_opportunities SET status=%s WHERE company_id=%s AND id=%s', ('published' if action == 'publish_opportunity' else 'closed', cid, f['id']))
        elif action == 'transition':
            required(cur, cid, 'mobility')
            service.transition(cur, cid, user_id, f['id'], f['status'], int(f['revision']))
        elif action == 'placement':
            required(cur, cid, 'mobility')
            service.confirm_placement(cur, cid, f['id'])
        elif action in ('scenario', 'publish_scenario'):
            required(cur, cid, 'planning')
            if action == 'scenario':
                actions = []
                if f.get('course_handle'):
                    count = int(f.get('people', '1'))
                    if not 1 <= count <= 100000:
                        raise ValueError('Angiv antal deltagere.')
                    actions.append(dict(handle=f['course_handle'][:255], people=count))
                service.save_scenario(cur, cid, {**f.to_dict(), 'actions': actions})
            else:
                service.publish_scenario(cur, cid, f['id'])
        else:
            abort(404)
        current_app.mysql.connection.commit()
        from security_audit import audit
        audit('talent.' + action, resource_type='talent', resource_id=f.get('id', ''), company_id=cid)
        flash('Ændringen er gemt.', 'success')
    except (ValueError, KeyError) as exc:
        current_app.mysql.connection.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else 'Udfyld alle felter.', 'warning')
    except Exception as exc:
        current_app.mysql.connection.rollback()
        if getattr(exc, 'code', None):
            raise
        current_app.logger.exception('Talent action failed: %s', action)
        flash('Ændringen kunne ikke gemmes. Genindlæs siden og prøv igen.', 'danger')
    finally:
        cur.close()
    return redirect(url_for(dest))


@talent_bp.route('/api/talent/competencies')
@login_required
def competency_api():
    """Authenticated, own-company read model; failure is never a fabricated empty profile."""
    from flask import jsonify
    cur = None
    try:
        cur, cid, user_id = context()
        return jsonify(service.employee_state(cur, cid, user_id, profile()))
    except Exception as exc:
        if getattr(exc, 'code', None):
            raise
        current_app.logger.exception('Competency read model unavailable')
        return jsonify(available=False, error='Vurderingerne kunne ikke hentes.'), 503
    finally:
        if cur:
            cur.close()


@talent_bp.route('/muligheder/ansaettelsesteam')
@login_required
def hiring():
    cur, cid, user_id = context(management=True)
    try:
        required(cur, cid, 'mobility')
        applications = rows(cur, 'SELECT a.*,o.title,o.kind FROM talent_applications a JOIN talent_opportunities o ON o.id=a.opportunity_id AND o.company_id=a.company_id '
                            'JOIN talent_hiring_members h ON h.company_id=o.company_id AND h.opportunity_id=o.id '
                            'WHERE a.company_id=%s AND h.user_id=%s ORDER BY a.updated_at DESC', (cid, user_id))
        return render_template('fm/talent_hiring.html', applications=applications, stages=service.STAGE_LABELS, transitions=service.STAGES)
    finally:
        cur.close()
